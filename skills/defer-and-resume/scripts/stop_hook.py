#!/usr/bin/env python3
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WAIT_ARMED = "armed"
WAIT_WAITING = "waiting"
WAIT_KEEPALIVE_PENDING = "keepalive_pending"
WAIT_COMPLETION_PENDING = "completion_pending"
WAIT_DISARMED = "disarmed"
WAIT_EXPIRED = "expired"
WAIT_CANCELLED = "cancelled"
RESULT_STALE_WORKER = 125


def positive_seconds(name: str, default: float, fallback_name: str | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None and fallback_name:
        raw = os.environ.get(fallback_name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def boolean_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


POLL_SECONDS = positive_seconds("CODEX_DEFER_POLL_SECONDS", 0.5)
KEEPALIVE_SECONDS = positive_seconds(
    "CODEX_DEFER_KEEPALIVE_SECONDS",
    1500.0,
    fallback_name="CODEX_DEFER_HOOK_MAX_WAIT",
)
KEEPALIVE_ENABLED = boolean_env("CODEX_DEFER_KEEPALIVE_ENABLED", True)


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False))
    sys.stdout.flush()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".codex"


def runtime_root() -> Path:
    override = os.environ.get("CODEX_DEFER_RUNTIME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return codex_home() / "runtime" / "defer-and-resume"


def current_thread(hook_input: dict[str, Any]) -> str:
    for candidate in (
        os.environ.get("CODEX_THREAD_ID"),
        hook_input.get("thread_id"),
        hook_input.get("threadId"),
        hook_input.get("session_id"),
        hook_input.get("sessionId"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def stop_hook_active(hook_input: dict[str, Any]) -> bool:
    value = hook_input.get("stop_hook_active", False)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_path(task: Path) -> Path:
    return task / "wait.json"


def read_wait(task: Path) -> dict[str, Any]:
    path = wait_path(task)
    if path.exists():
        value = read_json(path)
        if not isinstance(value.get("state"), str):
            raise ValueError(f"{path} must contain a string state")
        return value
    metadata = read_json(task / "metadata.json")
    value = {
        "version": 1,
        "state": WAIT_ARMED,
        "armed_at": metadata.get("registered_at", now_iso()),
        "updated_at": now_iso(),
    }
    write_json_atomic(path, value)
    return value


def update_wait(task: Path, state: str, **fields: Any) -> dict[str, Any]:
    value = read_wait(task)
    value.update(fields)
    value["version"] = 1
    value["state"] = state
    value["updated_at"] = now_iso()
    write_json_atomic(wait_path(task), value)
    return value


def synthesize_stale_result(task: Path) -> None:
    result_path = task / "result.json"
    worker_path = task / "worker.json"
    if result_path.exists() or not worker_path.exists():
        return
    worker = read_json(worker_path)
    if process_alive(int(worker.get("pid", 0))):
        return
    metadata = read_json(task / "metadata.json")
    write_json_atomic(
        result_path,
        {
            "task_id": metadata["task_id"],
            "name": metadata["name"],
            "started_at": worker.get("started_at"),
            "completed_at": now_iso(),
            "exit_code": RESULT_STALE_WORKER,
            "error": "worker exited without writing result.json",
            "timed_out": False,
            "cancelled": False,
            "log_path": str(task / "output.log"),
        },
    )


def task_dirs(thread_dir: Path) -> list[Path]:
    if not thread_dir.is_dir():
        return []
    tasks: list[Path] = []
    for child in sorted(thread_dir.iterdir()):
        if not child.is_dir() or not (child / "metadata.json").exists():
            continue
        synthesize_stale_result(child)
        tasks.append(child)
    return tasks


def prepare_wait(tasks: list[Path], hook_input: dict[str, Any]) -> list[Path]:
    continuing = stop_hook_active(hook_input)
    prepared: list[Path] = []
    for task in tasks:
        wait = read_wait(task)
        state = wait.get("state")
        if continuing:
            if state == WAIT_COMPLETION_PENDING:
                update_wait(task, WAIT_DISARMED, completed_continuation_at=now_iso())
                continue
            if state == WAIT_KEEPALIVE_PENDING:
                state = WAIT_ARMED
            if state not in {WAIT_ARMED, WAIT_WAITING}:
                continue
        else:
            # A new, non-continuation turn means the previous Hook wait was
            # interrupted. Do not silently re-enter the wait on that turn.
            if state in {WAIT_WAITING, WAIT_KEEPALIVE_PENDING, WAIT_COMPLETION_PENDING}:
                update_wait(task, WAIT_DISARMED, disarmed_at=now_iso(), disarm_reason="wait interrupted")
                continue
            if state != WAIT_ARMED:
                continue

        fields: dict[str, Any] = {
            "hook_pid": os.getpid(),
            "hook_started_at": now_iso(),
            "hook_turn_id": hook_input.get("turn_id"),
        }
        if not wait.get("wait_started_at"):
            fields["wait_started_at"] = now_iso()
        update_wait(task, WAIT_WAITING, **fields)
        prepared.append(task)
    return prepared


def emit_completion(tasks: list[Path]) -> None:
    summaries: list[str] = []
    for task in tasks:
        metadata = read_json(task / "metadata.json")
        result = read_json(task / "result.json")
        wake_path = task / "wake.json"
        emitted_at = now_iso()
        if not wake_path.exists():
            write_json_atomic(
                wake_path,
                {
                    "emitted_at": emitted_at,
                    "emitted": True,
                    "attempt": 1,
                    "exit_code": result.get("exit_code"),
                },
            )
        update_wait(
            task,
            WAIT_DISARMED,
            wake_emitted_at=emitted_at,
            completion_exit_code=result.get("exit_code"),
            disarmed_at=emitted_at,
            disarm_reason="completion wake emitted",
        )
        summaries.append(
            f"{metadata.get('name', task.name)} completed with exit code {result.get('exit_code')}; "
            f"task directory: {task}"
        )
    emit(
        {
            "decision": "block",
            "reason": "Deferred work has completed. Read result.json and only the necessary portion of "
            "output.log, then continue the original task. The completion wake is already disarmed; "
            "cleanup is optional.\n" + "\n".join(summaries),
        }
    )


def emit_keepalive(tasks: list[Path]) -> None:
    names: list[str] = []
    for task in tasks:
        wait = read_wait(task)
        keepalive_count = int(wait.get("keepalive_count", 0)) + 1
        update_wait(task, WAIT_KEEPALIVE_PENDING, keepalive_count=keepalive_count, last_keepalive_at=now_iso())
        names.append(str(read_json(task / "metadata.json").get("name", task.name)))
    emit(
        {
            "decision": "block",
            "reason": "Cache keepalive wake: deferred work is still running: "
            + ", ".join(names)
            + ". Do not poll, inspect logs, or call status commands. End this turn immediately "
            + "with the shortest useful response; the same deferred registration will automatically "
            + "re-enter local waiting on the continuation turn.",
        }
    )


def emit_expired(tasks: list[Path]) -> None:
    names: list[str] = []
    for task in tasks:
        update_wait(task, WAIT_EXPIRED, expired_at=now_iso(), expire_reason="Hook wait window expired")
        names.append(str(read_json(task / "metadata.json").get("name", task.name)))
    emit(
        {
            "continue": True,
            "systemMessage": "Deferred wait window expired for: "
            + ", ".join(names)
            + ". The worker is not cancelled. Run defer.py arm --task-dir <path> to wait again.",
        }
    )


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
        if not isinstance(hook_input, dict):
            hook_input = {}
    except Exception:
        hook_input = {}

    thread = current_thread(hook_input)
    if not thread:
        emit({"continue": True, "systemMessage": "defer-and-resume: missing Codex thread id"})
        return 0

    prepared = prepare_wait(task_dirs(runtime_root() / thread), hook_input)
    if not prepared:
        emit({"continue": True})
        return 0

    deadline = time.monotonic() + KEEPALIVE_SECONDS
    while True:
        tasks = [task for task in prepared if read_wait(task).get("state") == WAIT_WAITING]
        if not tasks:
            emit({"continue": True})
            return 0

        completed = [task for task in tasks if (task / "result.json").exists()]
        if completed:
            emit_completion(completed)
            return 0

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if KEEPALIVE_ENABLED:
                emit_keepalive(tasks)
            else:
                emit_expired(tasks)
            return 0

        time.sleep(min(POLL_SECONDS, max(0.01, remaining)))


if __name__ == "__main__":
    raise SystemExit(main())
