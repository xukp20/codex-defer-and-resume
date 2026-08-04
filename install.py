#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SKILL_NAME = "defer-and-resume"
HOOK_STATUS = "Waiting for deferred work with one-shot cache keepalive"
USER_PROMPT_HOOK_STATUS = "Disarming deferred waits on user input"


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
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        USER_PROMPT_HOOK_STATUS,
    } or (
        "defer-and-resume" in command
        and command.endswith(("stop_hook.py", "user_prompt_hook.py"))
    )


def remove_existing_hook_groups(stop_groups: Any) -> list[Any]:
    if not isinstance(stop_groups, list):
        return []
    retained: list[Any] = []
    for group in stop_groups:
        if not isinstance(group, dict):
            retained.append(group)
            continue
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            retained.append(group)
            continue
        remaining = [hook for hook in hooks if not is_defer_hook(hook)]
        if remaining:
            updated = dict(group)
            updated["hooks"] = remaining
            retained.append(updated)
    return retained


def install_skill(source: Path, destination: Path, backup_root: Path) -> Path | None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = destination.parent / f".{SKILL_NAME}.install-{uuid.uuid4().hex}"
    shutil.copytree(
        source,
        staging,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    backup: Path | None = None
    if destination.exists():
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backup_root / f"{SKILL_NAME}-{timestamp()}-{uuid.uuid4().hex[:8]}"
        destination.replace(backup)
    staging.replace(destination)
    return backup


def install_hook(
    codex_home: Path,
    stop_script_path: Path,
    user_prompt_script_path: Path,
) -> Path | None:
    hooks_path = codex_home / "hooks.json"
    config = read_object(hooks_path)
    backup: Path | None = None
    if hooks_path.exists():
        backup = hooks_path.with_name(f"hooks.json.backup-{timestamp()}-{uuid.uuid4().hex[:8]}")
        shutil.copy2(hooks_path, backup)
        os.chmod(backup, 0o600)

    hooks = config.get("hooks")
    if hooks is None:
        hooks = {}
        config["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise SystemExit(f"{hooks_path}: 'hooks' must be a JSON object")

    stop_groups = remove_existing_hook_groups(hooks.get("Stop", []))
    user_prompt_groups = remove_existing_hook_groups(hooks.get("UserPromptSubmit", []))
    stop_command = f"/usr/bin/env python3 {shlex.quote(str(stop_script_path))}"
    stop_groups.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": stop_command,
                    "async": False,
                    "timeout": 3700,
                    "statusMessage": HOOK_STATUS,
                }
            ]
        }
    )
    user_prompt_command = f"/usr/bin/env python3 {shlex.quote(str(user_prompt_script_path))}"
    user_prompt_groups.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": user_prompt_command,
                    "async": False,
                    "timeout": 10,
                    "statusMessage": USER_PROMPT_HOOK_STATUS,
                }
            ]
        }
    )
    hooks["Stop"] = stop_groups
    hooks["UserPromptSubmit"] = user_prompt_groups
    config.setdefault("description", "Codex lifecycle hooks")
    write_json_atomic(hooks_path, config)
    return backup


def preflight_hook_config(codex_home: Path) -> None:
    hooks_path = codex_home / "hooks.json"
    config = read_object(hooks_path)
    hooks = config.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise SystemExit(f"{hooks_path}: 'hooks' must be a JSON object")
    if isinstance(hooks, dict):
        for event_name in ("Stop", "UserPromptSubmit"):
            event_groups = hooks.get(event_name)
            if event_groups is not None and not isinstance(event_groups, list):
                raise SystemExit(f"{hooks_path}: 'hooks.{event_name}' must be a JSON array")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install the defer-and-resume Codex Skill and lifecycle Hooks"
    )
    parser.add_argument("--codex-home", type=Path, default=default_codex_home())
    return parser.parse_args()


def main() -> int:
    if os.name != "posix":
        raise SystemExit("defer-and-resume currently supports macOS and Linux")
    args = parse_args()
    codex_home = args.codex_home.expanduser().resolve()
    repository_root = Path(__file__).resolve().parent
    source_skill = repository_root / "skills" / SKILL_NAME
    if not (source_skill / "SKILL.md").is_file():
        raise SystemExit(f"Skill source is missing: {source_skill}")

    preflight_hook_config(codex_home)
    destination = codex_home / "skills" / SKILL_NAME
    backup_root = codex_home / "backups" / SKILL_NAME
    skill_backup = install_skill(source_skill, destination, backup_root)
    scripts = destination / "scripts"
    hook_backup = install_hook(
        codex_home,
        scripts / "stop_hook.py",
        scripts / "user_prompt_hook.py",
    )

    print(f"Installed Skill: {destination}")
    print(f"Configured Stop and UserPromptSubmit Hooks: {codex_home / 'hooks.json'}")
    if skill_backup:
        print(f"Previous Skill backup: {skill_backup}")
    if hook_backup:
        print(f"Previous Hook backup: {hook_backup}")
    print("Start a new Codex task to use the installed Skill.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
