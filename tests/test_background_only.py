import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "defer-and-resume" / "scripts"


class BackgroundOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, CODEX_DEFER_RUNTIME=self.temp.name, CODEX_THREAD_ID='test-background-only')

    def tearDown(self):
        # Cancel unfinished workers before removing their isolated task state.
        for p in Path(self.temp.name).rglob('metadata.json'):
            task = p.parent
            if not (task / 'result.json').exists():
                self.cli('cancel', '--task-dir', str(task))
        self.temp.cleanup()

    def cli(self, *args):
        r = subprocess.run([sys.executable, str(SCRIPTS / 'defer.py'), *args], capture_output=True, text=True, env=self.env, timeout=5, check=True)
        return json.loads(r.stdout)

    def start(self, code, no_hook=True, timeout=None):
        args = ['start', '--name', 'isolated-test']
        if no_hook:
            args.append('--no-hook')
        if timeout is not None:
            args.extend(['--timeout', str(timeout)])
        args.extend(['--', sys.executable, '-c', code])
        return Path(self.cli(*args)['task_dir'])

    def result(self, task):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (task / 'result.json').exists():
                return json.loads((task / 'result.json').read_text())
            time.sleep(0.02)
        self.fail('worker did not finish')

    def hook(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / 'stop_hook.py')], input=json.dumps({'thread_id': self.env['CODEX_THREAD_ID']}), capture_output=True, text=True, env=self.env, timeout=2, check=True)
        return json.loads(r.stdout)

    def test_no_hook_returns_while_worker_runs_and_stays_silent_after_completion(self):
        task = self.start("import time; time.sleep(0.6); print('finished')")
        self.assertFalse((task / 'result.json').exists())
        self.assertEqual(self.hook(), {'continue': True})
        self.assertEqual(self.cli('status', '--task-dir', str(task))['wait_state'], 'disarmed')
        self.assertEqual(self.result(task)['exit_code'], 0)
        self.assertIn('finished', (task / 'output.log').read_text())
        self.assertEqual(self.hook(), {'continue': True})
        self.assertFalse((task / 'wake.json').exists())

    def test_missing_wait_file_does_not_arm_background_job_and_explicit_arm_works(self):
        task = self.start("print('done')")
        self.result(task)
        (task / 'wait.json').unlink()
        self.assertEqual(self.hook(), {'continue': True})
        self.assertEqual(json.loads((task / 'wait.json').read_text())['state'], 'disarmed')
        (task / 'wait.json').unlink()
        self.assertEqual(self.cli('status', '--task-dir', str(task))['wait_state'], 'disarmed')
        self.assertEqual(self.cli('arm', '--task-dir', str(task))['wait_state'], 'armed')
        response = self.hook()
        self.assertIn('Deferred work has completed', response['reason'])
        self.assertTrue((task / 'wake.json').exists())

    def test_default_start_preserves_completion_wake(self):
        task = self.start("print('default')", no_hook=False)
        self.result(task)
        self.assertEqual(self.cli('status', '--task-dir', str(task))['wait_state'], 'armed')
        self.assertIn('Deferred work has completed', self.hook()['reason'])

    def test_background_timeout_and_cancel_preserve_result_handling(self):
        task = self.start('import time; time.sleep(10)', timeout=0.1)
        self.assertEqual(self.result(task)['exit_code'], 124)
        self.assertEqual(self.hook(), {'continue': True})
        task = self.start('import time; time.sleep(10)')
        self.cli('cancel', '--task-dir', str(task))
        self.assertEqual(self.result(task)['exit_code'], 130)
        self.assertEqual(self.hook(), {'continue': True})


if __name__ == '__main__':
    unittest.main()
