"""A Windows job stops a command's whole process tree and waits until it is gone."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mergetrain.command_runner import run_command
from mergetrain.windows_job import CREATE_SUSPENDED, WindowsJob, named_job_active

SOURCE = Path(__file__).resolve().parents[1] / "src"
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0


@unittest.skipUnless(os.name == "nt", "Windows job objects")
class WindowsJobTests(unittest.TestCase):
    def test_terminate_returns_once_every_process_has_exited(self) -> None:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        job = WindowsJob.create()
        assert job is not None, "Windows refused to create a job object"
        self.addCleanup(job.close)
        self.addCleanup(job.terminate)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pid_file = root / "grandchild.pid"
            child = (
                "import pathlib, subprocess, sys, time; "
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
                "time.sleep(60)"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", child],
                cwd=root,
                creationflags=CREATE_SUSPENDED,
            )
            self.assertTrue(job.adopt(process.pid), "the child did not join the job")
            deadline = time.monotonic() + 30
            while not (pid_file.exists() and pid_file.stat().st_size):
                self.assertLess(time.monotonic(), deadline, "the child never started its child")
                time.sleep(0.05)
            grandchild_pid = int(pid_file.read_text(encoding="utf-8"))
            grandchild = kernel32.OpenProcess(SYNCHRONIZE, False, grandchild_pid)
            self.assertTrue(grandchild, "could not open the grandchild")
            try:
                self.assertGreaterEqual(job._active_processes(), 2)

                self.assertTrue(job.terminate())

                self.assertEqual(
                    kernel32.WaitForSingleObject(grandchild, 0),
                    WAIT_OBJECT_0,
                    "the grandchild was still running when terminate() returned",
                )
            finally:
                kernel32.CloseHandle(grandchild)
            self.assertEqual(process.wait(timeout=5), 1)

    def test_a_named_job_stays_visible_after_its_runner_is_killed(self) -> None:
        # Windows drops a name with the last handle, so this fails unless the
        # command itself holds a handle to its job (#220).
        name = f"Local\\mergetrain-test-{os.getpid()}-{time.monotonic_ns()}"
        runner_code = (
            "import sys; from mergetrain.command_runner import run_command; "
            "run_command([sys.executable, '-c', 'import time; time.sleep(8)'], "
            "cwd='.', job_name=sys.argv[1])"
        )
        env = {**os.environ, "PYTHONPATH": str(SOURCE)}
        runner = subprocess.Popen([sys.executable, "-c", runner_code, name], env=env)
        self.addCleanup(runner.wait, 10)
        self.addCleanup(runner.kill)
        deadline = time.monotonic() + 30
        while not named_job_active(name):
            self.assertIsNone(runner.poll(), "the runner exited before its command started")
            self.assertLess(time.monotonic(), deadline, "the command never started")
            time.sleep(0.05)

        runner.kill()
        runner.wait(timeout=10)

        self.assertTrue(named_job_active(name), "the job's name died with its runner")
        deadline = time.monotonic() + 30
        while named_job_active(name):
            self.assertLess(time.monotonic(), deadline, "the command never finished")
            time.sleep(0.1)

    def test_a_named_job_stays_findable_while_a_process_outlives_its_command(self) -> None:
        # A local receive-pack can outlive a git push killed on its own and
        # still land the push. It holds no handle to the job, and Windows
        # drops a name with its last handle, so the job has to stay findable
        # for as long as any process of it runs.
        name = f"Local\\mergetrain-test-{os.getpid()}-{time.monotonic_ns()}"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            started = Path(td) / "descendant-started"
            descendant = (
                f"import pathlib, time; pathlib.Path({str(started)!r}).touch(); time.sleep(10)"
            )
            # The command starts the descendant, then dies without a clean exit.
            command = (
                "import os, subprocess, sys; "
                f"subprocess.Popen([sys.executable, '-c', {descendant!r}], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL); os._exit(1)"
            )
            completed = run_command(
                [sys.executable, "-c", command],
                cwd=tempfile.gettempdir(),
                check=False,
                job_name=name,
            )
            self.assertEqual(completed.returncode, 1)
            deadline = time.monotonic() + 30
            while not started.exists():
                self.assertLess(time.monotonic(), deadline, "the descendant never started")
                time.sleep(0.05)

            self.assertTrue(named_job_active(name), "the job's name died with its command")
            deadline = time.monotonic() + 30
            while named_job_active(name):
                self.assertLess(time.monotonic(), deadline, "the descendant never finished")
                time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
