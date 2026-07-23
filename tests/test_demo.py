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


class DemoAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(__file__).resolve().parents[1]

    def test_tape_records_the_conflict_and_success_outcomes(self) -> None:
        tape = (self.repo / "docs" / "demo.tape").read_text(encoding="utf-8")
        self.assertIn("Output docs/images/demo.gif", tape)
        self.assertIn("Require mergetrain", tape)
        self.assertIn(
            'Type "mergetrain demo --brief --pause --dir /tmp/mt-vhs-171"',
            tape,
        )
        self.assertIn("Wait+Screen@5s /result: partial/", tape)
        self.assertIn("Wait+Screen@5s /result: success/", tape)
        self.assertIn("Wait+Screen@120s /Demo complete:/", tape)

    def test_workflow_pins_the_recorder_and_uploads_the_result(self) -> None:
        workflow = (
            self.repo / ".github" / "workflows" / "demo-gif.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn(
            "charmbracelet/vhs-action@59641cdc7fadf3978db65eb8c6937ea2752f4ec3",
            workflow,
        )
        self.assertIn("version: v0.11.0", workflow)
        self.assertIn("path: docs/demo.tape", workflow)
        self.assertIn("uses: actions/upload-artifact@v7", workflow)

    def test_readme_embeds_the_generated_gif(self) -> None:
        readme = (self.repo / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "https://raw.githubusercontent.com/yongjip/mergetrain/main/"
            "docs/images/demo.gif",
            readme,
        )


@unittest.skipUnless(shutil.which("git"), "git is required")
class DemoTests(unittest.TestCase):
    def test_full_demo_skips_fifo_git_conflict_and_deploys_survivors(self) -> None:
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
            self.assertIn("FIFO result: #1 merged; #2 hit a Git conflict", out.getvalue())
            self.assertIn('"conflict_with": ""', out.getvalue())
            self.assertIn("three compatible requests were validated together", out.getvalue())
            self.assertIn("Sandbox kept at:", out.getvalue())

            repo = sandbox / "repo"
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = {job.branch: job for job in list_jobs(conn, limit=10)}
            finally:
                conn.close()

            conflicted = jobs["agent/longer-timeout"]
            self.assertEqual(conflicted.status, "canceled")
            self.assertEqual(conflicted.conflict_with, "")
            for branch in (
                "agent/faster-timeout",
                "agent/add-retries",
                "agent/request-logging",
            ):
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
                {
                    "agent/faster-timeout",
                    "agent/add-retries",
                    "agent/request-logging",
                },
            )
            remote_config = subprocess.run(
                ["git", f"--git-dir={remote}", "show", "main:app/config.py"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            self.assertEqual(remote_config, "DEFAULT_TIMEOUT = 10\n")
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

    def test_brief_mode_keeps_milestones_and_omits_bulk_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sandbox = Path(td) / "brief"
            out, err = io.StringIO(), io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {"MERGETRAIN_DEMO_STEP_DELAY": "0"},
                    clear=False,
                ),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                code = main(["demo", "--brief", "--dir", str(sandbox)])
            self.assertEqual(code, 0, err.getvalue())
            rendered = out.getvalue()
            self.assertIn("ready: health=true clean=true", rendered)
            self.assertIn("result: partial", rendered)
            self.assertIn("conflict_with: #1 ↔ #2", rendered)
            self.assertIn("outcome: merge_conflict", rendered)
            self.assertIn("result: success", rendered)
            self.assertIn("Demo complete:", rendered)
            self.assertIn("Sandbox removed: $DEMO", rendered)
            self.assertNotIn(str(sandbox), rendered)
            self.assertNotIn('"config": {', rendered)

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
