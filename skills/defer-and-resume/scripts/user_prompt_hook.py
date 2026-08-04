#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from typing import Any

from stop_hook import (
    WAIT_ARMED,
    WAIT_COMPLETION_PENDING,
    WAIT_DISARMED,
    WAIT_KEEPALIVE_PENDING,
    WAIT_WAITING,
    current_thread,
    emit,
    now_iso,
    read_wait,
    runtime_root,
    task_dirs,
    update_wait,
)


ACTIVE_WAIT_STATES = {
    WAIT_ARMED,
    WAIT_WAITING,
    WAIT_KEEPALIVE_PENDING,
    WAIT_COMPLETION_PENDING,
}


def read_hook_input() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def disarm_user_waits(thread: str) -> None:
    try:
        tasks = task_dirs(runtime_root() / thread)
    except Exception:
        # A lifecycle hook must never block a user prompt because one old or
        # partially-written task record is malformed.
        return

    for task in tasks:
        try:
            wait = read_wait(task)
            if wait.get("state") not in ACTIVE_WAIT_STATES:
                continue
            update_wait(
                task,
                WAIT_DISARMED,
                disarmed_at=now_iso(),
                disarm_reason="user prompt interrupted wait",
            )
        except (OSError, ValueError, json.JSONDecodeError):
            continue


def main() -> int:
    hook_input = read_hook_input()
    thread = current_thread(hook_input)
    if thread:
        disarm_user_waits(thread)
    emit({"continue": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
