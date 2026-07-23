from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mergetrain.cli import main
from mergetrain.config import load_config
from mergetrain.demo import DemoFailure, DemoWalkthrough
from mergetrain.store import connect, list_jobs


@unittest.skipUnless(shutil.which("git"), "git is required")
class DemoTests(unittest.TestCase):
    def test_full_demo_isolated_conflict_and_deploys_only_survivors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            invoking_repo = root / "invoking"
            invoking_repo.mkdir()
            subprocess.run(
                ["git", "init", "-q", str(invoking_repo)],
                check=True,
                capture_output=True,
            )
            sentinel = invoking_repo / "do-not-touch.txt"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            user_config = root / "user.gitconfig"
            user_config.write_text("[user]\n\tname = Real User\n", encoding="utf-8")
            sandbox = root / "walkthrough"
            out, err = io.StringIO(), io.StringIO()

            previous_cwd = Path.cwd()
            try:
                os.chdir(invoking_repo)
                with (
                    patch.dict(
                        os.environ,
                        {"GIT_CONFIG_GLOBAL": str(user_config)},
                        clear=False,
                    ),
                    redirect_stdout(out),
                    redirect_stderr(err),
                ):
                    code = main(["demo", "--dir", str(sandbox), "--keep"])
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(code, 0, err.getvalue())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
            self.assertEqual(
                user_config.read_text(encoding="utf-8"),
                "[user]\n\tname = Real User\n",
            )
            self.assertIn("result: partial", out.getvalue())
            self.assertIn("result: success", out.getvalue())
            self.assertIn("Train gate failed; bisecting 4 jobs", out.getvalue())
            self.assertIn('"conflict_with": "2"', out.getvalue())
            self.assertIn("conflict_with: #1 ↔ #2", out.getvalue())
            self.assertIn("Sandbox kept at:", out.getvalue())

            repo = sandbox / "repo"
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = {job.branch: job for job in list_jobs(conn, limit=10)}
            finally:
                conn.close()

            left = jobs["agent/faster-timeout"]
            right = jobs["agent/health-check"]
            self.assertEqual(left.status, "canceled")
            self.assertEqual(right.status, "canceled")
            for branch in ("agent/add-retries", "agent/request-logging"):
                self.assertEqual(jobs[branch].status, "deployed")
                self.assertEqual(jobs[branch].push_status, "succeeded")
                self.assertEqual(jobs[branch].verify_status, "succeeded")

            remote = sandbox / "remote.git"
            subjects = subprocess.run(
                [
                    "git",
                    f"--git-dir={remote}",
                    "log",
                    "--format=%s",
                    "main",
                ],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.splitlines()
            agent_subjects = {subject for subject in subjects if subject.startswith("agent/")}
            self.assertEqual(
                agent_subjects,
                {"agent/add-retries", "agent/request-logging"},
            )
            remote_config = subprocess.run(
                ["git", f"--git-dir={remote}", "show", "main:app/config.py"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            self.assertEqual(remote_config, "DEFAULT_TIMEOUT = 30\n")
            for path in ("app/retries.py", "app/request_logging.py"):
                subprocess.run(
                    ["git", f"--git-dir={remote}", "cat-file", "-e", f"main:{path}"],
                    check=True,
                    capture_output=True,
                )

    def test_success_cleanup_removes_only_marked_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sandbox = Path(td) / "cleanup"
            with patch.object(DemoWalkthrough, "run", return_value=None):
                code = main(["demo", "--dir", str(sandbox)])
            self.assertEqual(code, 0)
            self.assertFalse(sandbox.exists())

    def test_failure_keeps_sandbox_and_prints_recovery_hints(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sandbox = Path(td) / "failure"
            err = io.StringIO()
            with (
                patch.object(
                    DemoWalkthrough,
                    "run",
                    side_effect=DemoFailure("synthetic failure"),
                ),
                redirect_stderr(err),
            ):
                code = main(["demo", "--dir", str(sandbox)])
            self.assertEqual(code, 1)
            self.assertTrue(sandbox.is_dir())
            self.assertTrue((sandbox / ".mergetrain-demo-marker").is_file())
            self.assertIn("sandbox kept", err.getvalue().lower())
            self.assertIn("status --json", err.getvalue())
            self.assertIn("dashboard --preview", err.getvalue())

    def test_nonempty_requested_directory_is_never_modified(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sandbox = Path(td) / "occupied"
            sandbox.mkdir()
            sentinel = sandbox / "sentinel.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                code = main(["demo", "--dir", str(sandbox)])
            self.assertEqual(code, 1)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")
            self.assertIn("must not exist or must be empty", err.getvalue())


if __name__ == "__main__":
    unittest.main()
