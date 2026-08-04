from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
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
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/usr/bin/true",
                                    "statusMessage": "unrelated prompt",
                                }
                            ]
                        }
                    ],
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
            stop_hook_entries = [
                hook
                for group in config["hooks"]["Stop"]
                for hook in group.get("hooks", [])
            ]
            user_prompt_hook_entries = [
                hook
                for group in config["hooks"]["UserPromptSubmit"]
                for hook in group.get("hooks", [])
            ]
            defer_stop_hooks = [
                hook for hook in stop_hook_entries if "defer-and-resume" in hook.get("command", "")
            ]
            defer_user_prompt_hooks = [
                hook for hook in user_prompt_hook_entries if "defer-and-resume" in hook.get("command", "")
            ]
            self.assertEqual(len(defer_stop_hooks), 1)
            self.assertEqual(len(defer_user_prompt_hooks), 1)
            self.assertIn(
                "'", defer_stop_hooks[0]["command"], "a path containing spaces must be shell-quoted"
            )
            self.assertIn(
                "'",
                defer_user_prompt_hooks[0]["command"],
                "a path containing spaces must be shell-quoted",
            )
            self.assertTrue(any(hook.get("statusMessage") == "unrelated" for hook in stop_hook_entries))
            self.assertTrue(
                any(hook.get("statusMessage") == "unrelated prompt" for hook in user_prompt_hook_entries)
            )
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
            remaining_user_prompt = [
                hook
                for group in config["hooks"]["UserPromptSubmit"]
                for hook in group.get("hooks", [])
            ]
            self.assertEqual(
                [hook.get("statusMessage") for hook in remaining_user_prompt], ["unrelated prompt"]
            )

    def test_uninstall_refuses_incomplete_runtime_but_accepts_completed_runtime(self) -> None:
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

            (task / "result.json").write_text('{"exit_code": 0}', encoding="utf-8")
            run(sys.executable, str(UNINSTALLER), "--codex-home", str(codex_home))
            self.assertFalse((codex_home / "skills" / "defer-and-resume").exists())

            run(sys.executable, str(INSTALLER), "--codex-home", str(codex_home))
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
        self.user_prompt_hook = (
            self.codex_home / "skills" / "defer-and-resume" / "scripts" / "user_prompt_hook.py"
        )
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

    def hook_call(self, *, continuing: bool = False) -> dict[str, object]:
        completed = run(
            sys.executable,
            str(self.hook),
            env=self.environment,
            input_text=json.dumps({"stop_hook_active": continuing}),
        )
        return json.loads(completed.stdout)

    def user_prompt_hook_call(self) -> dict[str, object]:
        completed = run(
            sys.executable,
            str(self.user_prompt_hook),
            env=self.environment,
            input_text=json.dumps({"session_id": "test-thread"}),
        )
        return json.loads(completed.stdout)

    def acknowledge_and_clean(self, task_dir: Path) -> None:
        run(sys.executable, str(self.defer), "ack", "--task-dir", str(task_dir), env=self.environment)
        run(sys.executable, str(self.defer), "clean", "--task-dir", str(task_dir), env=self.environment)

    def test_keepalive_then_completion(self) -> None:
        task_dir = self.start_task("0.2")
        heartbeat = self.hook_call()
        self.assertEqual(heartbeat["decision"], "block")
        self.assertIn("Cache keepalive wake", str(heartbeat["reason"]))

        completion: dict[str, object] | None = None
        for _ in range(5):
            response = self.hook_call(continuing=True)
            if "Deferred work has completed" in str(response.get("reason")):
                completion = response
                break
        self.assertIsNotNone(completion, "the task should complete within five bounded Hook intervals")
        self.assertEqual(completion["decision"], "block")
        result = json.loads((task_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["exit_code"], 0)
        run(sys.executable, str(self.defer), "clean", "--task-dir", str(task_dir), env=self.environment)
        self.assertFalse(task_dir.exists())

    def test_completion_wake_is_one_shot(self) -> None:
        task_dir = self.start_task("0.01")
        first = self.hook_call()
        self.assertIn("Deferred work has completed", str(first["reason"]))
        first_wake = json.loads((task_dir / "wake.json").read_text(encoding="utf-8"))
        self.assertEqual(first_wake["attempt"], 1)
        self.assertTrue(first_wake["emitted"])
        self.assertNotIn("ack", str(first["reason"]).lower())
        wait = json.loads((task_dir / "wait.json").read_text(encoding="utf-8"))
        self.assertEqual(wait["state"], "disarmed")

        second = self.hook_call(continuing=True)
        self.assertEqual(second, {"continue": True})
        second_wake = json.loads((task_dir / "wake.json").read_text(encoding="utf-8"))
        self.assertEqual(second_wake["attempt"], 1)
        run(sys.executable, str(self.defer), "clean", "--task-dir", str(task_dir), env=self.environment)
        self.assertFalse(task_dir.exists())

    def test_rearm_starts_a_new_completion_registration(self) -> None:
        task_dir = self.start_task("0.01")
        self.assertIn("Deferred work has completed", str(self.hook_call()["reason"]))
        self.assertTrue((task_dir / "wake.json").exists())

        run(sys.executable, str(self.defer), "arm", "--task-dir", str(task_dir), env=self.environment)
        self.assertFalse((task_dir / "wake.json").exists())
        self.assertIn("Deferred work has completed", str(self.hook_call()["reason"]))
        wait = json.loads((task_dir / "wait.json").read_text(encoding="utf-8"))
        self.assertEqual(wait["state"], "disarmed")
        run(sys.executable, str(self.defer), "clean", "--task-dir", str(task_dir), env=self.environment)

    def test_gc_collects_completed_state_without_ack(self) -> None:
        task_dir = self.codex_home / "runtime" / "defer-and-resume" / "test-thread" / "gc-task"
        task_dir.mkdir(parents=True)
        (task_dir / "metadata.json").write_text(
            json.dumps({"registered_at": "2000-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        (task_dir / "result.json").write_text(
            json.dumps({"completed_at": "2000-01-01T00:00:01+00:00", "exit_code": 0}),
            encoding="utf-8",
        )

        run(
            sys.executable,
            str(self.defer),
            "gc",
            "--older-than-hours",
            "0",
            env=self.environment,
        )
        self.assertFalse(task_dir.exists())

    def test_ack_runs_default_gc_and_keeps_current_task(self) -> None:
        old_task = self.codex_home / "runtime" / "defer-and-resume" / "old-thread" / "gc-task"
        old_task.mkdir(parents=True)
        (old_task / "metadata.json").write_text(
            json.dumps({"registered_at": "2000-01-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        (old_task / "result.json").write_text(
            json.dumps({"completed_at": "2000-01-01T00:00:01+00:00", "exit_code": 0}),
            encoding="utf-8",
        )

        current_task = self.start_task("0.01")
        self.assertIn("Deferred work has completed", str(self.hook_call()["reason"]))
        run(sys.executable, str(self.defer), "ack", "--task-dir", str(current_task), env=self.environment)

        self.assertFalse(old_task.exists())
        self.assertTrue(current_task.exists())
        self.assertTrue((current_task / "ack.json").exists())
        run(sys.executable, str(self.defer), "clean", "--task-dir", str(current_task), env=self.environment)

    def test_timeout_exit_code(self) -> None:
        task_dir = self.start_task("0.3", timeout="0.05")
        completion = self.hook_call()
        self.assertIn("exit code 124", str(completion["reason"]))
        result = json.loads((task_dir / "result.json").read_text(encoding="utf-8"))
        self.assertTrue(result["timed_out"])
        self.acknowledge_and_clean(task_dir)

    def test_interrupted_wait_is_disarmed_without_cancelling_worker(self) -> None:
        task_dir = self.start_task("0.5")
        heartbeat = self.hook_call()
        self.assertEqual(heartbeat["decision"], "block")
        interrupted = self.hook_call()
        self.assertEqual(interrupted, {"continue": True})
        wait = json.loads((task_dir / "wait.json").read_text(encoding="utf-8"))
        self.assertEqual(wait["state"], "disarmed")
        run(sys.executable, str(self.defer), "cancel", "--task-dir", str(task_dir), env=self.environment)
        self.acknowledge_and_clean(task_dir)

    def test_user_prompt_disarms_keepalive_without_cancelling_worker(self) -> None:
        task_dir = self.start_task("2")
        heartbeat = self.hook_call()
        self.assertEqual(heartbeat["decision"], "block")
        self.assertIn("Cache keepalive wake", str(heartbeat["reason"]))

        worker_pid = int(json.loads((task_dir / "worker.json").read_text(encoding="utf-8"))["pid"])
        self.assertEqual(self.user_prompt_hook_call(), {"continue": True})
        wait = json.loads((task_dir / "wait.json").read_text(encoding="utf-8"))
        self.assertEqual(wait["state"], "disarmed")
        self.assertEqual(wait["disarm_reason"], "user prompt interrupted wait")
        os.kill(worker_pid, 0)

        run(sys.executable, str(self.defer), "cancel", "--task-dir", str(task_dir), env=self.environment)
        self.acknowledge_and_clean(task_dir)

    def test_user_prompt_recovers_a_killed_stop_hook(self) -> None:
        task_dir = self.start_task("2")
        self.environment["CODEX_DEFER_KEEPALIVE_SECONDS"] = "10"
        hook_process = subprocess.Popen(
            [sys.executable, str(self.hook)],
            cwd=REPOSITORY_ROOT,
            env=self.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert hook_process.stdin is not None
        hook_process.stdin.write(json.dumps({"stop_hook_active": False}))
        hook_process.stdin.close()
        for _ in range(100):
            wait = json.loads((task_dir / "wait.json").read_text(encoding="utf-8"))
            if wait["state"] == "waiting":
                break
            if hook_process.poll() is not None:
                self.fail(f"Stop Hook exited before entering wait: {hook_process.returncode}")
            time.sleep(0.01)
        else:
            self.fail("Stop Hook did not enter waiting state")

        hook_process.kill()
        hook_process.wait(timeout=5)
        assert hook_process.stdout is not None
        assert hook_process.stderr is not None
        hook_process.stdout.close()
        hook_process.stderr.close()
        self.assertEqual(self.user_prompt_hook_call(), {"continue": True})
        wait = json.loads((task_dir / "wait.json").read_text(encoding="utf-8"))
        self.assertEqual(wait["state"], "disarmed")
        self.assertEqual(wait["disarm_reason"], "user prompt interrupted wait")

        run(sys.executable, str(self.defer), "cancel", "--task-dir", str(task_dir), env=self.environment)
        self.acknowledge_and_clean(task_dir)

    def test_keepalive_can_be_disabled_and_rearmed(self) -> None:
        self.environment["CODEX_DEFER_KEEPALIVE_ENABLED"] = "false"
        task_dir = self.start_task("0.4")
        expired = self.hook_call()
        self.assertEqual(expired["continue"], True)
        self.assertIn("wait window expired", str(expired["systemMessage"]))
        wait = json.loads((task_dir / "wait.json").read_text(encoding="utf-8"))
        self.assertEqual(wait["state"], "expired")

        self.environment["CODEX_DEFER_KEEPALIVE_SECONDS"] = "0.5"
        run(sys.executable, str(self.defer), "arm", "--task-dir", str(task_dir), env=self.environment)
        completion = self.hook_call()
        self.assertIn("Deferred work has completed", str(completion["reason"]))
        self.acknowledge_and_clean(task_dir)


class SkillMetadataTests(unittest.TestCase):
    def test_skill_frontmatter_and_interface(self) -> None:
        skill = (SKILL_SOURCE / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\n"))
        self.assertIn("\nname: defer-and-resume\n", skill)
        self.assertIn("\ndescription:", skill)
        interface = (SKILL_SOURCE / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "Defer and Resume"', interface)

    def test_public_text_contains_no_cjk_characters(self) -> None:
        cjk = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
        text_suffixes = {".md", ".py", ".yaml", ".yml", ".json", ".toml", ".txt"}
        violations: list[str] = []
        for path in REPOSITORY_ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or path.suffix not in text_suffixes:
                continue
            if cjk.search(path.read_text(encoding="utf-8")):
                violations.append(str(path.relative_to(REPOSITORY_ROOT)))
        self.assertEqual(violations, [], f"public text must remain English-only: {violations}")


if __name__ == "__main__":
    unittest.main()
