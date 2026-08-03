#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


POLL_SECONDS = positive_seconds("CODEX_DEFER_POLL_SECONDS", 0.5)
KEEPALIVE_SECONDS = positive_seconds(
    "CODEX_DEFER_KEEPALIVE_SECONDS",
    1500.0,
    fallback_name="CODEX_DEFER_HOOK_MAX_WAIT",
)
WAKE_RETRY_SECONDS = positive_seconds("CODEX_DEFER_WAKE_RETRY_SECONDS", 60.0)
RESULT_STALE_WORKER = 125


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False))
    sys.stdout.flush()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
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


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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


def wake_due_in(task: Path) -> float | None:
    if (task / "ack.json").exists() or not (task / "result.json").exists():
        return None
    wake_path = task / "wake.json"
    if not wake_path.exists():
        return 0.0
    try:
        emitted_at = parse_iso(str(read_json(wake_path)["emitted_at"]))
    except Exception:
        return 0.0
    elapsed = (datetime.now(timezone.utc) - emitted_at).total_seconds()
    return max(0.0, WAKE_RETRY_SECONDS - elapsed)


def active_tasks(thread_dir: Path) -> list[Path]:
    if not thread_dir.is_dir():
        return []
    tasks: list[Path] = []
    for child in sorted(thread_dir.iterdir()):
        if not child.is_dir() or not (child / "metadata.json").exists() or (child / "ack.json").exists():
            continue
        synthesize_stale_result(child)
        tasks.append(child)
    return tasks


def emit_completion(tasks: list[Path]) -> None:
    summaries: list[str] = []
    for task in tasks:
        metadata = read_json(task / "metadata.json")
        result = read_json(task / "result.json")
        wake_path = task / "wake.json"
        previous_attempts = 0
        if wake_path.exists():
            try:
                previous_attempts = int(read_json(wake_path).get("attempt", 0))
            except Exception:
                previous_attempts = 0
        write_json_atomic(
            wake_path,
            {
                "emitted_at": now_iso(),
                "exit_code": result.get("exit_code"),
                "attempt": previous_attempts + 1,
            },
        )
        summaries.append(
            f"{metadata.get('name', task.name)} completed with exit code {result.get('exit_code')}; "
            f"task directory: {task}"
        )
    emit(
        {
            "decision": "block",
            "reason": "Deferred work has completed. Read result.json and only the necessary portion of "
            "output.log, then run defer.py ack. After recording evidence, run defer.py clean.\n"
            + "\n".join(summaries),
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

    thread_dir = runtime_root() / thread
    deadline = time.monotonic() + KEEPALIVE_SECONDS
    while True:
        tasks = active_tasks(thread_dir)
        if not tasks:
            emit({"continue": True})
            return 0

        due = [task for task in tasks if wake_due_in(task) == 0.0]
        if due:
            emit_completion(due)
            return 0

        incomplete = [task for task in tasks if not (task / "result.json").exists()]
        remaining = deadline - time.monotonic()
        if incomplete and remaining <= 0:
            names = [read_json(task / "metadata.json").get("name", task.name) for task in incomplete]
            emit(
                {
                    "decision": "block",
                    "reason": "Cache keepalive wake: deferred work is still running: "
                    + ", ".join(map(str, names))
                    + ". Do not poll, inspect logs, or call status commands. End this turn immediately "
                    + "with the shortest useful response so the Stop Hook can continue waiting. This "
                    + "wake only prevents prolonged model inactivity in the current task.",
                }
            )
            return 0

        retry_delays = [delay for task in tasks if (delay := wake_due_in(task)) is not None and delay > 0]
        sleep_for = POLL_SECONDS
        if incomplete:
            sleep_for = min(sleep_for, max(0.01, remaining))
        if retry_delays:
            sleep_for = min(sleep_for, max(0.01, min(retry_delays)))
        time.sleep(sleep_for)


if __name__ == "__main__":
    raise SystemExit(main())
