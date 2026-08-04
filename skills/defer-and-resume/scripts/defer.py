#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


RESULT_STALE_WORKER = 125
RESULT_TIMEOUT = 124
RESULT_CANCELLED = 130

WAIT_ARMED = "armed"
WAIT_WAITING = "waiting"
WAIT_KEEPALIVE_PENDING = "keepalive_pending"
WAIT_COMPLETION_PENDING = "completion_pending"
WAIT_DISARMED = "disarmed"
WAIT_EXPIRED = "expired"
WAIT_CANCELLED = "cancelled"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".codex"


def runtime_root() -> Path:
    override = os.environ.get("CODEX_DEFER_RUNTIME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return codex_home() / "runtime" / "defer-and-resume"


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
        raise ValueError(f"{path} must contain a JSON object")
    return value


def thread_id() -> str:
    value = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not value:
        raise SystemExit("CODEX_THREAD_ID is not available; run this from a Codex task")
    return value


def validate_task_dir(value: str) -> Path:
    task_dir = Path(value).expanduser().resolve()
    root = runtime_root().resolve()
    if root not in task_dir.parents:
        raise SystemExit(f"task directory is outside runtime root: {task_dir}")
    if not (task_dir / "metadata.json").is_file():
        raise SystemExit(f"not a defer-and-resume task directory: {task_dir}")
    return task_dir


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


def create_log(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)


def write_result_if_absent(task_dir: Path, value: dict[str, Any]) -> bool:
    result_path = task_dir / "result.json"
    if result_path.exists():
        return False
    write_json_atomic(result_path, value)
    return True


def wait_path(task_dir: Path) -> Path:
    return task_dir / "wait.json"


def read_wait_state(task_dir: Path) -> dict[str, Any]:
    path = wait_path(task_dir)
    if path.exists():
        value = read_json(path)
        if not isinstance(value.get("state"), str):
            raise ValueError(f"{path} must contain a string state")
        return value
    metadata = read_json(task_dir / "metadata.json")
    value = {
        "version": 1,
        "state": WAIT_ARMED,
        "armed_at": metadata.get("registered_at", now_iso()),
        "updated_at": now_iso(),
    }
    write_json_atomic(path, value)
    return value


def update_wait_state(task_dir: Path, state: str, **fields: Any) -> dict[str, Any]:
    value = read_wait_state(task_dir)
    value.update(fields)
    value["version"] = 1
    value["state"] = state
    value["updated_at"] = now_iso()
    write_json_atomic(wait_path(task_dir), value)
    return value


def stale_worker_result(task_dir: Path) -> dict[str, Any] | None:
    result_path = task_dir / "result.json"
    if result_path.exists():
        return read_json(result_path)
    worker_path = task_dir / "worker.json"
    if not worker_path.exists():
        return None
    worker_info = read_json(worker_path)
    pid = int(worker_info.get("pid", 0))
    if process_alive(pid):
        return None
    metadata = read_json(task_dir / "metadata.json")
    value = {
        "task_id": metadata["task_id"],
        "name": metadata["name"],
        "started_at": worker_info.get("started_at"),
        "completed_at": now_iso(),
        "exit_code": RESULT_STALE_WORKER,
        "error": "worker exited without writing result.json",
        "timed_out": False,
        "cancelled": False,
        "log_path": str(task_dir / "output.log"),
    }
    write_result_if_absent(task_dir, value)
    return read_json(task_dir / "result.json")


def worker(task_dir: Path) -> int:
    metadata = read_json(task_dir / "metadata.json")
    command_payload = json.load(sys.stdin)
    if not isinstance(command_payload, list) or not all(isinstance(item, str) for item in command_payload):
        raise SystemExit("worker command payload must be a JSON string array")
    command = command_payload
    log_path = task_dir / "output.log"
    create_log(log_path)
    started_at = now_iso()
    exit_code = 127
    error: str | None = None
    timed_out = False
    try:
        with log_path.open("ab", buffering=0) as log_file:
            completed = subprocess.run(
                command,
                cwd=metadata["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=metadata.get("timeout_seconds"),
            )
            exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = RESULT_TIMEOUT
        error = f"command exceeded timeout of {metadata.get('timeout_seconds')} seconds"
        with log_path.open("ab", buffering=0) as log_file:
            log_file.write((error + "\n").encode())
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        with log_path.open("ab", buffering=0) as log_file:
            log_file.write((error + "\n").encode())

    write_result_if_absent(
        task_dir,
        {
            "task_id": metadata["task_id"],
            "name": metadata["name"],
            "started_at": started_at,
            "completed_at": now_iso(),
            "exit_code": exit_code,
            "error": error,
            "timed_out": timed_out,
            "cancelled": False,
            "log_path": str(log_path),
        },
    )
    return 0


def start(args: argparse.Namespace) -> int:
    if os.name != "posix":
        raise SystemExit("defer-and-resume currently supports macOS and Linux")
    if not args.command:
        raise SystemExit("a command is required after --")
    command = args.command[1:] if args.command[0] == "--" else args.command
    if not command:
        raise SystemExit("a command is required after --")
    if args.timeout is not None and args.timeout <= 0:
        raise SystemExit("--timeout must be greater than zero")

    current_thread = thread_id()
    task_id = uuid.uuid4().hex
    task_dir = runtime_root() / current_thread / task_id
    task_dir.mkdir(parents=True, mode=0o700)
    os.chmod(task_dir, 0o700)
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise SystemExit(f"working directory does not exist: {cwd}")

    write_json_atomic(
        task_dir / "metadata.json",
        {
            "version": 3,
            "task_id": task_id,
            "thread_id": current_thread,
            "name": args.name,
            "cwd": str(cwd),
            "executable": Path(command[0]).name,
            "argument_count": max(0, len(command) - 1),
            "timeout_seconds": args.timeout,
            "registered_at": now_iso(),
        },
    )
    update_wait_state(task_dir, WAIT_ARMED, armed_at=now_iso())
    create_log(task_dir / "output.log")

    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_worker", str(task_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        text=True,
    )
    if child.stdin is None:
        raise SystemExit("failed to open worker command channel")
    child.stdin.write(json.dumps(command, ensure_ascii=False))
    child.stdin.close()
    write_json_atomic(task_dir / "worker.json", {"pid": child.pid, "started_at": now_iso()})
    print(json.dumps({"task_id": task_id, "task_dir": str(task_dir), "worker_pid": child.pid}, ensure_ascii=False))
    return 0


def task_status(task_dir: Path) -> dict[str, Any]:
    metadata = read_json(task_dir / "metadata.json")
    wait = read_wait_state(task_dir)
    result = stale_worker_result(task_dir)
    worker_info = read_json(task_dir / "worker.json") if (task_dir / "worker.json").exists() else {}
    if (task_dir / "ack.json").exists():
        state = "acknowledged"
    elif result:
        state = "completed"
    elif process_alive(int(worker_info.get("pid", 0))):
        state = "running"
    else:
        state = "starting"
    value: dict[str, Any] = {
        "task_dir": str(task_dir),
        "task_id": metadata.get("task_id"),
        "thread_id": metadata.get("thread_id"),
        "name": metadata.get("name"),
        "state": state,
        "registered_at": metadata.get("registered_at"),
        "worker_pid": worker_info.get("pid"),
        "wait_state": wait.get("state"),
    }
    if result:
        value.update(
            {
                "completed_at": result.get("completed_at"),
                "exit_code": result.get("exit_code"),
                "error": result.get("error"),
                "timed_out": result.get("timed_out", False),
                "cancelled": result.get("cancelled", False),
            }
        )
    return value


def inspect(args: argparse.Namespace) -> int:
    task_dir = validate_task_dir(args.task_dir)
    value: dict[str, Any] = {"task_dir": str(task_dir), "status": task_status(task_dir)}
    for name in ("metadata.json", "worker.json", "wait.json", "result.json", "wake.json", "ack.json"):
        path = task_dir / name
        if path.exists():
            value[name.removesuffix(".json")] = read_json(path)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def status(args: argparse.Namespace) -> int:
    print(json.dumps(task_status(validate_task_dir(args.task_dir)), ensure_ascii=False, indent=2))
    return 0


def list_tasks(args: argparse.Namespace) -> int:
    root = runtime_root()
    task_dirs: list[Path] = []
    if args.all_threads:
        if root.is_dir():
            for thread_dir in root.iterdir():
                if thread_dir.is_dir():
                    task_dirs.extend(child for child in thread_dir.iterdir() if (child / "metadata.json").is_file())
    else:
        thread_dir = root / thread_id()
        if thread_dir.is_dir():
            task_dirs.extend(child for child in thread_dir.iterdir() if (child / "metadata.json").is_file())
    values = [task_status(path) for path in sorted(task_dirs)]
    print(json.dumps(values, ensure_ascii=False, indent=2))
    return 0


def acknowledge(args: argparse.Namespace) -> int:
    task_dir = validate_task_dir(args.task_dir)
    if not (task_dir / "result.json").exists():
        raise SystemExit("cannot acknowledge an incomplete task")
    write_json_atomic(task_dir / "ack.json", {"acknowledged_at": now_iso()})
    print(json.dumps(task_status(task_dir), ensure_ascii=False))
    return 0


def arm(args: argparse.Namespace) -> int:
    task_dir = validate_task_dir(args.task_dir)
    if (task_dir / "ack.json").exists():
        raise SystemExit("cannot arm an acknowledged task; clean it first")
    # A deliberate re-arm starts a new one-shot registration. The previous
    # wake marker belongs to the old registration and must not be reused.
    (task_dir / "wake.json").unlink(missing_ok=True)
    update_wait_state(
        task_dir,
        WAIT_ARMED,
        armed_at=now_iso(),
        disarmed_at=None,
        disarm_reason=None,
        wake_emitted_at=None,
        wake_delivered_at=None,
        completion_exit_code=None,
    )
    print(json.dumps(task_status(task_dir), ensure_ascii=False))
    return 0


def disarm(args: argparse.Namespace) -> int:
    task_dir = validate_task_dir(args.task_dir)
    update_wait_state(task_dir, WAIT_DISARMED, disarmed_at=now_iso(), disarm_reason="disarmed by agent")
    print(json.dumps(task_status(task_dir), ensure_ascii=False))
    return 0


def cancel(args: argparse.Namespace) -> int:
    if args.grace_seconds < 0:
        raise SystemExit("--grace-seconds cannot be negative")
    task_dir = validate_task_dir(args.task_dir)
    if (task_dir / "result.json").exists():
        update_wait_state(task_dir, WAIT_CANCELLED, cancelled_at=now_iso(), cancel_reason="cancelled by agent")
        print(json.dumps(task_status(task_dir), ensure_ascii=False))
        return 0
    metadata = read_json(task_dir / "metadata.json")
    worker_info = read_json(task_dir / "worker.json")
    pid = int(worker_info.get("pid", 0))
    if process_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + args.grace_seconds
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if process_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    write_result_if_absent(
        task_dir,
        {
            "task_id": metadata["task_id"],
            "name": metadata["name"],
            "started_at": worker_info.get("started_at"),
            "completed_at": now_iso(),
            "exit_code": RESULT_CANCELLED,
            "error": "cancelled by agent",
            "timed_out": False,
            "cancelled": True,
            "log_path": str(task_dir / "output.log"),
        },
    )
    update_wait_state(task_dir, WAIT_CANCELLED, cancelled_at=now_iso(), cancel_reason="cancelled by agent")
    print(json.dumps(task_status(task_dir), ensure_ascii=False))
    return 0


def clean(args: argparse.Namespace) -> int:
    task_dir = validate_task_dir(args.task_dir)
    if not args.force and not (task_dir / "result.json").exists():
        raise SystemExit("refusing to clean an incomplete task; wait for completion or use --force")
    shutil.rmtree(task_dir)
    return 0


def gc(args: argparse.Namespace) -> int:
    if args.older_than_hours < 0:
        raise SystemExit("--older-than-hours cannot be negative")
    root = runtime_root()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.older_than_hours)
    removed: list[str] = []
    if root.is_dir():
        for metadata_path in root.glob("*/*/metadata.json"):
            task_dir = metadata_path.parent
            result_path = task_dir / "result.json"
            if not result_path.is_file():
                # Never collect a registration whose worker has not produced
                # a terminal result, even when an old wait file is present.
                continue
            worker_path = task_dir / "worker.json"
            if worker_path.is_file():
                try:
                    if process_alive(int(read_json(worker_path).get("pid", 0))):
                        continue
                except (TypeError, ValueError, OSError, json.JSONDecodeError):
                    continue
            try:
                ack_path = task_dir / "ack.json"
                if ack_path.is_file():
                    timestamp = read_json(ack_path).get("acknowledged_at")
                else:
                    timestamp = read_json(result_path).get("completed_at")
                if not isinstance(timestamp, str):
                    timestamp = read_json(metadata_path).get("registered_at")
                completed_at = parse_iso(str(timestamp))
            except Exception:
                continue
            if completed_at <= cutoff:
                shutil.rmtree(task_dir)
                removed.append(str(task_dir))
        for thread_dir in root.iterdir():
            if thread_dir.is_dir() and not any(thread_dir.iterdir()):
                thread_dir.rmdir()
    print(json.dumps({"removed": removed, "count": len(removed)}, ensure_ascii=False))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--name", required=True)
    start_parser.add_argument("--cwd", default=os.getcwd())
    start_parser.add_argument("--timeout", type=float)
    start_parser.add_argument("command", nargs=argparse.REMAINDER)

    worker_parser = subparsers.add_parser("_worker")
    worker_parser.add_argument("task_dir")

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--task-dir", required=True)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--task-dir", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--all-threads", action="store_true")

    ack_parser = subparsers.add_parser("ack")
    ack_parser.add_argument("--task-dir", required=True)

    arm_parser = subparsers.add_parser("arm")
    arm_parser.add_argument("--task-dir", required=True)

    disarm_parser = subparsers.add_parser("disarm")
    disarm_parser.add_argument("--task-dir", required=True)

    cancel_parser = subparsers.add_parser("cancel")
    cancel_parser.add_argument("--task-dir", required=True)
    cancel_parser.add_argument("--grace-seconds", type=float, default=1.0)

    clean_parser = subparsers.add_parser("clean")
    clean_parser.add_argument("--task-dir", required=True)
    clean_parser.add_argument("--force", action="store_true")

    gc_parser = subparsers.add_parser("gc")
    gc_parser.add_argument("--older-than-hours", type=float, default=168.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.action == "start":
        return start(args)
    if args.action == "_worker":
        return worker(validate_task_dir(args.task_dir))
    if args.action == "inspect":
        return inspect(args)
    if args.action == "status":
        return status(args)
    if args.action == "list":
        return list_tasks(args)
    if args.action == "ack":
        return acknowledge(args)
    if args.action == "arm":
        return arm(args)
    if args.action == "disarm":
        return disarm(args)
    if args.action == "cancel":
        return cancel(args)
    if args.action == "clean":
        return clean(args)
    if args.action == "gc":
        return gc(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
