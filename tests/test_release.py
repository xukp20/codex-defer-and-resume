from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY_ROOT / "install.py"
UNINSTALLER = REPOSITORY_ROOT / "uninstall.py"
SKILL_SOURCE = REPOSITORY_ROOT / "skills" / "defer-and-resume"


def run(
    *arguments: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(arguments),
        cwd=REPOSITORY_ROOT,
        env=env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {arguments}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


class InstallerTests(unittest.TestCase):
    def test_malformed_hook_config_does_not_replace_existing_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary) / "codex"
            installed_skill = codex_home / "skills" / "defer-and-resume"
            installed_skill.mkdir(parents=True)
            marker = installed_skill / "marker.txt"
            marker.write_text("keep", encoding="utf-8")
            (codex_home / "hooks.json").write_text('{"hooks": []}', encoding="utf-8")

            completed = run(
                sys.executable,
                str(INSTALLER),
                "--codex-home",
                str(codex_home),
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertTrue(marker.is_file())

    def test_install_is_idempotent_and_uninstall_preserves_other_hooks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex defer test ") as temporary:
            codex_home = Path(temporary) / "codex home"
            codex_home.mkdir(parents=True)
            existing = {
                "description": "existing config",
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/usr/bin/true",
                                    "statusMessage": "unrelated",
                                }
                            ]
                        }
                    ]
                },
            }
            (codex_home / "hooks.json").write_text(json.dumps(existing), encoding="utf-8")

            run(sys.executable, str(INSTALLER), "--codex-home", str(codex_home))
            run(sys.executable, str(INSTALLER), "--codex-home", str(codex_home))

            installed_skill = codex_home / "skills" / "defer-and-resume"
            self.assertTrue((installed_skill / "SKILL.md").is_file())
            self.assertFalse(any(installed_skill.rglob("__pycache__")))
            self.assertFalse(any(installed_skill.rglob("*.pyc")))
            config = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            hook_entries = [
                hook
                for group in config["hooks"]["Stop"]
                for hook in group.get("hooks", [])
            ]
            defer_hooks = [hook for hook in hook_entries if "defer-and-resume" in hook.get("command", "")]
            self.assertEqual(len(defer_hooks), 1)
            self.assertIn("'", defer_hooks[0]["command"], "a path containing spaces must be shell-quoted")
            self.assertTrue(any(hook.get("statusMessage") == "unrelated" for hook in hook_entries))
            self.assertTrue(list(codex_home.glob("hooks.json.backup-*")))
            self.assertTrue(list((codex_home / "backups" / "defer-and-resume").iterdir()))

            run(sys.executable, str(UNINSTALLER), "--codex-home", str(codex_home))
            self.assertFalse(installed_skill.exists())
            config = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
            remaining = [
                hook
                for group in config["hooks"]["Stop"]
                for hook in group.get("hooks", [])
            ]
            self.assertEqual([hook.get("statusMessage") for hook in remaining], ["unrelated"])

    def test_uninstall_refuses_unacknowledged_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            codex_home = Path(temporary) / "codex"
            run(sys.executable, str(INSTALLER), "--codex-home", str(codex_home))
            task = codex_home / "runtime" / "defer-and-resume" / "thread" / "task"
            task.mkdir(parents=True)
            (task / "metadata.json").write_text("{}", encoding="utf-8")

            refused = run(
                sys.executable,
                str(UNINSTALLER),
                "--codex-home",
                str(codex_home),
                check=False,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("Refusing to uninstall", refused.stderr)
            self.assertTrue((codex_home / "skills" / "defer-and-resume").exists())

            run(
                sys.executable,
                str(UNINSTALLER),
                "--codex-home",
                str(codex_home),
                "--force",
                "--purge-runtime",
            )
            self.assertFalse((codex_home / "runtime" / "defer-and-resume").exists())


class DeferredRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.codex_home = Path(self.temporary.name) / "codex"
        run(sys.executable, str(INSTALLER), "--codex-home", str(self.codex_home))
        self.defer = self.codex_home / "skills" / "defer-and-resume" / "scripts" / "defer.py"
        self.hook = self.codex_home / "skills" / "defer-and-resume" / "scripts" / "stop_hook.py"
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "CODEX_HOME": str(self.codex_home),
                "CODEX_THREAD_ID": "test-thread",
                "CODEX_DEFER_KEEPALIVE_SECONDS": "0.12",
                "CODEX_DEFER_POLL_SECONDS": "0.02",
                "CODEX_DEFER_WAKE_RETRY_SECONDS": "0.08",
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start_task(self, seconds: str, timeout: str = "3") -> Path:
        started = run(
            sys.executable,
            str(self.defer),
            "start",
            "--name",
            "test task",
            "--cwd",
            str(REPOSITORY_ROOT),
            "--timeout",
            timeout,
            "--",
            "/bin/sleep",
            seconds,
            env=self.environment,
        )
        return Path(json.loads(started.stdout)["task_dir"])

    def hook_call(self) -> dict[str, object]:
        completed = run(
            sys.executable,
            str(self.hook),
            env=self.environment,
            input_text="{}",
        )
        return json.loads(completed.stdout)

    def acknowledge_and_clean(self, task_dir: Path) -> None:
        run(sys.executable, str(self.defer), "ack", "--task-dir", str(task_dir), env=self.environment)
        run(sys.executable, str(self.defer), "clean", "--task-dir", str(task_dir), env=self.environment)

    def test_keepalive_then_completion(self) -> None:
        task_dir = self.start_task("0.2")
        heartbeat = self.hook_call()
        self.assertEqual(heartbeat["decision"], "block")
        self.assertIn("缓存保活唤醒", str(heartbeat["reason"]))

        completion = self.hook_call()
        self.assertEqual(completion["decision"], "block")
        self.assertIn("后台任务已经完成", str(completion["reason"]))
        result = json.loads((task_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["exit_code"], 0)
        self.acknowledge_and_clean(task_dir)

    def test_unacknowledged_completion_is_retried(self) -> None:
        task_dir = self.start_task("0.01")
        first = self.hook_call()
        self.assertIn("后台任务已经完成", str(first["reason"]))
        first_wake = json.loads((task_dir / "wake.json").read_text(encoding="utf-8"))
        self.assertEqual(first_wake["attempt"], 1)

        second = self.hook_call()
        self.assertIn("后台任务已经完成", str(second["reason"]))
        second_wake = json.loads((task_dir / "wake.json").read_text(encoding="utf-8"))
        self.assertEqual(second_wake["attempt"], 2)
        self.acknowledge_and_clean(task_dir)

    def test_timeout_exit_code(self) -> None:
        task_dir = self.start_task("0.3", timeout="0.05")
        completion = self.hook_call()
        self.assertIn("退出码 124", str(completion["reason"]))
        result = json.loads((task_dir / "result.json").read_text(encoding="utf-8"))
        self.assertTrue(result["timed_out"])
        self.acknowledge_and_clean(task_dir)


class SkillMetadataTests(unittest.TestCase):
    def test_skill_frontmatter_and_interface(self) -> None:
        skill = (SKILL_SOURCE / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\n"))
        self.assertIn("\nname: defer-and-resume\n", skill)
        self.assertIn("\ndescription:", skill)
        interface = (SKILL_SOURCE / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "Defer and Resume"', interface)


if __name__ == "__main__":
    unittest.main()
