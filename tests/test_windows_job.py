"""A Windows job stops a command's whole process tree and waits until it is gone."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mergetrain.windows_job import CREATE_SUSPENDED, WindowsJob

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


if __name__ == "__main__":
    unittest.main()
