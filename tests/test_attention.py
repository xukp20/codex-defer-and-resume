from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "defer-and-resume" / "scripts"


class AttentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name)
        self.environment = dict(
            os.environ,
            CODEX_DEFER_RUNTIME=str(self.runtime),
            CODEX_THREAD_ID="attention-tests",
            CODEX_DEFER_KEEPALIVE_SECONDS="0.12",
            CODEX_DEFER_POLL_SECONDS="0.02",
        )
        self.tasks: list[Path] = []

    def tearDown(self) -> None:
        for task in self.tasks:
            if not (task / "result.json").exists():
                self.cli("cancel", "--task-dir", str(task), "--grace-seconds", "0.05")
        self.temporary.cleanup()

    def cli(self, *arguments: str) -> dict:
        response = subprocess.run(
            [sys.executable, str(SCRIPTS / "defer.py"), *arguments],
            env=self.environment, capture_output=True, text=True, check=True, timeout=5,
        )
        return json.loads(response.stdout)

    def start(self, seconds: float = 10, *options: str) -> Path:
        response = self.cli(
            "start", "--name", "attention test", *options, "--",
            sys.executable, "-c", f"import time; time.sleep({seconds!r})",
        )
        task = Path(response["task_dir"])
        self.tasks.append(task)
        return task

    def read(self, task: Path, name: str) -> dict:
        return json.loads((task / name).read_text())

    def hook(self, continuing: bool = False, script: str = "stop_hook.py") -> dict:
        response = subprocess.run(
            [sys.executable, str(SCRIPTS / script)], env=self.environment,
            input=json.dumps({"stop_hook_active": continuing}),
            capture_output=True, text=True, check=True, timeout=5,
        )
        return json.loads(response.stdout)

    def waiting_hook(self, task: Path) -> subprocess.Popen:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "stop_hook.py")],
            env=dict(self.environment, CODEX_DEFER_KEEPALIVE_SECONDS="5"),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        process.stdin.write("{}")
        process.stdin.close()
        process.stdin = None
        self.addCleanup(self.close_hook, process)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.read(task, "wait.json")["state"] == "waiting":
                return process
            self.assertIsNone(process.poll(), "hook exited before waiting")
            time.sleep(0.01)
        self.fail("hook did not enter waiting state")

    def close_hook(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    def wait_result(self, task: Path) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if (task / "result.json").exists():
                return
            time.sleep(0.01)
        self.fail("worker did not complete")

    def test_attention_during_wait_wakes_once_and_preserves_worker(self) -> None:
        task = self.start()
        process = self.waiting_hook(task)
        event = self.cli("attention", "--task-dir", str(task), "--reason", "upstream blocked",
                         "--evidence-path", str(task / "output.log"))
        receipt = Path(event["event_path"]).read_bytes()
        output, error = process.communicate(timeout=2)
        self.assertEqual(process.returncode, 0, error)
        self.assertIn("upstream blocked", json.loads(output)["reason"])
        self.assertIn(event["event_id"], json.loads(output)["reason"])
        self.assertEqual(self.cli("status", "--task-dir", str(task))["state"], "running")
        self.assertFalse((task / "result.json").exists())
        self.assertEqual(self.read(task, "wait.json")["state"], "disarmed")
        self.assertEqual(self.hook(True), {"continue": True})
        self.cli("arm", "--task-dir", str(task))
        response = self.hook()
        self.assertIn("Cache keepalive wake", response["reason"])
        self.assertNotIn("upstream blocked", response["reason"])
        self.assertEqual(Path(event["event_path"]).read_bytes(), receipt)
        self.assertEqual(self.read(task, "wait.json")["consumed_attention_ids"], [event["event_id"]])

    def test_completion_and_multiple_events_share_one_wake(self) -> None:
        task = self.start(0.01)
        self.wait_result(task)
        events = [self.cli("attention", "--task-dir", str(task), "--reason", reason)
                  for reason in ("failure evidence", "decision needed")]
        self.assertNotEqual(events[0]["event_id"], events[1]["event_id"])
        response = self.hook()
        self.assertIn("Deferred work has completed", response["reason"])
        for event in events:
            self.assertIn(event["reason"], response["reason"])
        self.assertCountEqual(self.read(task, "wake.json")["attention_event_ids"],
                              [event["event_id"] for event in events])
        self.assertEqual(self.hook(True), {"continue": True})
        self.cli("arm", "--task-dir", str(task))
        response = self.hook()
        self.assertIn("Deferred work has completed", response["reason"])
        self.assertNotIn("failure evidence", response["reason"])

    def test_attention_disarms_until_explicit_rearm_even_when_result_arrives(self) -> None:
        task = self.start(0.5)
        self.cli("attention", "--task-dir", str(task), "--reason", "blocked")
        self.assertIn("Action required", self.hook()["reason"])
        self.wait_result(task)
        self.assertEqual(self.hook(True), {"continue": True})
        self.cli("arm", "--task-dir", str(task))
        self.assertIn("Deferred work has completed", self.hook()["reason"])

    def test_pending_attention_on_background_task_waits_for_arm(self) -> None:
        task = self.start(10, "--no-hook")
        self.cli("attention", "--task-dir", str(task), "--reason", "pending decision")
        self.assertEqual(self.hook(), {"continue": True})
        self.cli("arm", "--task-dir", str(task))
        self.assertIn("pending decision", self.hook()["reason"])

    def test_review_deadline_survives_keepalives_and_rearm_resets_it(self) -> None:
        task = self.start(10, "--review-after", "0.7")
        deadline = self.read(task, "wait.json")["review_deadline_at"]
        response = self.hook()
        self.assertIn("Cache keepalive wake", response["reason"])
        self.assertIn("already received an actionable subagent", response["reason"])
        for _ in range(10):
            self.assertEqual(self.read(task, "wait.json")["review_deadline_at"], deadline)
            response = self.hook(True)
            if "review deadline reached" in response.get("reason", ""):
                break
        else:
            self.fail("review deadline was reset by keepalive")
        self.assertIn("Action required", response["reason"])
        self.assertTrue(self.read(task, "wake.json")["review_expired"])
        self.assertEqual(self.cli("status", "--task-dir", str(task))["state"], "running")
        self.assertEqual(self.hook(True), {"continue": True})
        self.cli("arm", "--task-dir", str(task))
        renewed = self.read(task, "wait.json")["review_deadline_at"]
        self.assertGreater(datetime.fromisoformat(renewed), datetime.fromisoformat(deadline))

    def test_stale_worker_is_detected_during_active_wait(self) -> None:
        # A worker fixture with no command children avoids orphaning any test work.
        task = self.runtime / self.environment["CODEX_THREAD_ID"] / "stale-worker"
        task.mkdir(parents=True)
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        self.addCleanup(lambda: worker.poll() is None and worker.kill())
        (task / "metadata.json").write_text(json.dumps({"task_id": task.name, "name": task.name}))
        (task / "worker.json").write_text(json.dumps({"pid": worker.pid}))
        (task / "wait.json").write_text(json.dumps({"state": "armed"}))
        process = self.waiting_hook(task)
        worker.terminate()
        worker.wait(timeout=2)
        output, error = process.communicate(timeout=2)
        self.assertEqual(process.returncode, 0, error)
        self.assertIn("exit code 125", json.loads(output)["reason"])
        self.assertEqual(self.read(task, "result.json")["exit_code"], 125)

    def test_malformed_task_does_not_hide_healthy_wake_or_user_disarm(self) -> None:
        bad = self.runtime / self.environment["CODEX_THREAD_ID"] / "000-bad-task"
        bad.mkdir(parents=True)
        (bad / "metadata.json").write_text("{")
        (bad / "worker.json").write_text("{")
        task = self.start()
        self.cli("attention", "--task-dir", str(task), "--reason", "healthy attention")
        (task / "attention" / "bad-event.json").write_text("{")
        self.assertIn("healthy attention", self.hook()["reason"])
        self.cli("arm", "--task-dir", str(task))
        (bad / "metadata.json").write_text("{}")
        self.assertEqual(self.hook(script="user_prompt_hook.py"), {"continue": True})
        self.assertEqual(self.read(task, "wait.json")["disarm_reason"], "user prompt interrupted wait")
        self.assertEqual(self.cli("status", "--task-dir", str(task))["state"], "running")

    def test_cancelled_wait_does_not_emit_pending_attention(self) -> None:
        task = self.start()
        self.cli("attention", "--task-dir", str(task), "--reason", "pending")
        self.cli("cancel", "--task-dir", str(task), "--grace-seconds", "0.05")
        self.assertEqual(self.hook(), {"continue": True})
        self.assertEqual(self.read(task, "result.json")["exit_code"], 130)

    def test_malformed_consumed_ids_do_not_hide_healthy_completion_or_user_disarm(self) -> None:
        bad = self.runtime / self.environment["CODEX_THREAD_ID"] / "000-bad-task"
        bad.mkdir(parents=True)
        (bad / "metadata.json").write_text(json.dumps({"task_id": bad.name, "name": bad.name}))
        (bad / "result.json").write_text(json.dumps({"exit_code": 0}))
        task = self.start(0.01)
        self.wait_result(task)
        for consumed in ("", None, {}, ["valid-id", 1]):
            with self.subTest(consumed_attention_ids=consumed):
                malformed = {"state": "armed", "consumed_attention_ids": consumed}
                (bad / "wait.json").write_text(json.dumps(malformed))
                self.cli("arm", "--task-dir", str(task))
                response = self.hook()
                self.assertIn("Deferred work has completed", response["reason"])
                self.assertNotIn(bad.name, response["reason"])
                self.assertFalse((bad / "wake.json").exists())
                self.cli("arm", "--task-dir", str(task))
                self.assertEqual(self.hook(script="user_prompt_hook.py"), {"continue": True})
                self.assertEqual(self.read(task, "wait.json")["disarm_reason"], "user prompt interrupted wait")
                self.assertEqual(self.read(bad, "wait.json"), malformed)

    def test_review_after_rejects_nonpositive_and_nonfinite_values(self) -> None:
        for value in ("0", "-1", "nan", "inf"):
            response = subprocess.run(
                [sys.executable, str(SCRIPTS / "defer.py"), "start", "--name", "invalid",
                 "--review-after", value, "--", sys.executable, "-c", "pass"],
                env=self.environment, capture_output=True, text=True, timeout=5,
            )
            self.assertNotEqual(response.returncode, 0)
            self.assertIn("--review-after", response.stderr)
        self.assertEqual(list(self.runtime.rglob("metadata.json")), [])


if __name__ == "__main__":
    unittest.main()
