#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SKILL_NAME = "defer-and-resume"
HOOK_STATUS = "Waiting for deferred work with one-shot cache keepalive"


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def read_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def is_defer_hook(hook: Any) -> bool:
    if not isinstance(hook, dict):
        return False
    command = str(hook.get("command", ""))
    return hook.get("statusMessage") in {
        HOOK_STATUS,
        "Waiting for deferred work with periodic cache keepalive",
    } or (
        "defer-and-resume" in command and command.endswith("stop_hook.py")
    )


def remove_hook(codex_home: Path) -> Path | None:
    hooks_path = codex_home / "hooks.json"
    if not hooks_path.exists():
        return None
    config = read_object(hooks_path)
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return None
    stop_groups = hooks.get("Stop")
    if not isinstance(stop_groups, list):
        return None

    changed = False
    retained_groups: list[Any] = []
    for group in stop_groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            retained_groups.append(group)
            continue
        remaining = [hook for hook in group["hooks"] if not is_defer_hook(hook)]
        if len(remaining) != len(group["hooks"]):
            changed = True
        if remaining:
            updated = dict(group)
            updated["hooks"] = remaining
            retained_groups.append(updated)
    if not changed:
        return None

    backup = hooks_path.with_name(f"hooks.json.backup-{timestamp()}-{uuid.uuid4().hex[:8]}")
    shutil.copy2(hooks_path, backup)
    os.chmod(backup, 0o600)
    if retained_groups:
        hooks["Stop"] = retained_groups
    else:
        hooks.pop("Stop", None)
    write_json_atomic(hooks_path, config)
    return backup


def unacknowledged_tasks(runtime_root: Path) -> list[Path]:
    if not runtime_root.is_dir():
        return []
    return [
        metadata.parent
        for metadata in runtime_root.glob("*/*/metadata.json")
        if not (metadata.parent / "ack.json").exists()
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Uninstall the defer-and-resume Codex Skill and Stop Hook")
    parser.add_argument("--codex-home", type=Path, default=default_codex_home())
    parser.add_argument("--purge-runtime", action="store_true")
    parser.add_argument("--force", action="store_true", help="Abandon unacknowledged deferred tasks")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    codex_home = args.codex_home.expanduser().resolve()
    runtime_root = codex_home / "runtime" / SKILL_NAME
    active = unacknowledged_tasks(runtime_root)
    if active and not args.force:
        names = "\n".join(f"- {path}" for path in active)
        raise SystemExit(
            "Refusing to uninstall while deferred tasks are unacknowledged. "
            "Acknowledge/clean them first or rerun with --force:\n" + names
        )

    hook_backup = remove_hook(codex_home)
    skill_path = codex_home / "skills" / SKILL_NAME
    if skill_path.exists():
        shutil.rmtree(skill_path)
        print(f"Removed Skill: {skill_path}")
    else:
        print(f"Skill was not installed: {skill_path}")

    if args.purge_runtime and runtime_root.exists():
        shutil.rmtree(runtime_root)
        print(f"Removed runtime state: {runtime_root}")
    elif runtime_root.exists():
        print(f"Retained runtime state: {runtime_root}")
    if hook_backup:
        print(f"Removed Stop Hook; previous config backup: {hook_backup}")
    else:
        print("No defer-and-resume Stop Hook was present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
