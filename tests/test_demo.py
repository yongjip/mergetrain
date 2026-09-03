from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mergetrain.cli import main
from mergetrain.config import load_config
from mergetrain.demo import DemoFailure, DemoSandbox, DemoWalkthrough
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
        workflow = (self.repo / ".github" / "workflows" / "demo-gif.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("permissions:\n  contents: read", workflow)
        # The recorder's dependencies are installed by pinned version and
        # verified by checksum, so a moved release cannot change the render.
        self.assertIn('VHS_VERSION: "0.11.0"', workflow)
        self.assertIn('TTYD_VERSION: "1.7.7"', workflow)
        for variable in ("VHS_SHA256", "TTYD_SHA256"):
            self.assertRegex(workflow, rf'{variable}: "[0-9a-f]{{64}}"')
        self.assertIn("sha256sum --check --strict", workflow)
        # The tape names JetBrains Mono; a fallback face would be silent.
        self.assertIn("fonts-jetbrains-mono", workflow)
        self.assertIn("vhs docs/demo.tape", workflow)
        self.assertRegex(
            workflow,
            r"uses: actions/upload-artifact@[0-9a-f]{40}\s+# v7\.0\.1",
        )

    def test_readme_embeds_the_generated_gif(self) -> None:
        readme = (self.repo / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "https://raw.githubusercontent.com/yongjip/mergetrain/main/docs/images/demo.gif",
            readme,
        )


@unittest.skipUnless(shutil.which("git"), "git is required")
class DemoTests(unittest.TestCase):
    def test_full_demo_attributes_the_semantic_pair_and_deploys_survivors(self) -> None:
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
            # The lesson the demo exists for: both branches pass alone and Git
            # merges them cleanly, so only the combined train catches them, and
            # the block names the partner instead of blaming one side.
            self.assertIn('"conflict_with": "2"', out.getvalue())
            self.assertIn(
                "semantic conflict: passes gates alone but fails combined with job 2",
                out.getvalue(),
            )
            self.assertIn("bisect result: #1 \u2194 #2 blocked", out.getvalue())
            self.assertIn("two compatible requests were validated together", out.getvalue())
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
            for branch in (
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
                {"agent/add-retries", "agent/request-logging"},
            )
            remote_config = subprocess.run(
                ["git", f"--git-dir={remote}", "show", "main:app/config.py"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            # faster-timeout was held with its partner, so main keeps the seed
            # value: a blocked pair never lands half of itself.
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
            self.assertIn("ready: health=healthy clean=true", rendered)
            self.assertIn("result: partial", rendered)
            self.assertIn("conflict_with: #2", rendered)
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


class DemoConfigTests(unittest.TestCase):
    """The generated config carries paths through YAML and one shell.

    A quoted path used to be pasted into the YAML template raw, which produced
    an invalid document for PyYAML (`config_error`, so `doctor` failed at step
    two). Every path that needs quoting -- any Windows path, and any path with a
    space on any platform -- hit this, so these cases stay covered off the slow
    full-demo path.
    """

    def _walkthrough(self, dirname: str, *, make_repo: bool = True) -> DemoWalkthrough:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / dirname
        root.mkdir(parents=True)
        sandbox = DemoSandbox(root=root, marker_token="test-token")
        walkthrough = DemoWalkthrough(sandbox, pause=False, delay=0.0)
        if make_repo:
            walkthrough.repo.mkdir(parents=True)
        return walkthrough

    def test_quoted_interpreter_path_stays_a_single_yaml_scalar(self) -> None:
        walkthrough = self._walkthrough("space in name")
        with patch.object(sys, "executable", "/opt/py 3.12/python3"):
            walkthrough._write_demo_config()

        config = load_config(repo=walkthrough.repo)
        self.assertEqual(
            config.gates[0].run,
            '"/opt/py 3.12/python3" -m unittest discover -s tests',
        )
        self.assertIn(f'--git-dir="{walkthrough.remote}"', config.deploy.verify[0].run)

    def test_windows_paths_keep_their_separators(self) -> None:
        walkthrough = self._walkthrough("brief")
        executable = r"C:\tools\Python\3.13\python.exe"
        with (
            patch.object(sys, "executable", executable),
            patch.object(os, "name", "nt"),
        ):
            walkthrough._write_demo_config()

        config = load_config(repo=walkthrough.repo)
        self.assertEqual(
            config.gates[0].run,
            f'"{executable}" -m unittest discover -s tests',
        )

    def test_path_with_a_single_quote_round_trips_through_yaml(self) -> None:
        walkthrough = self._walkthrough("quote")
        with patch.object(sys, "executable", "/opt/o'brien/python3"):
            walkthrough._write_demo_config()

        config = load_config(repo=walkthrough.repo)
        self.assertEqual(
            config.gates[0].run,
            '"/opt/o\'brien/python3" -m unittest discover -s tests',
        )

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_seeded_repository_commits_lf_line_endings(self) -> None:
        # Text-mode writes translate "\n" to os.linesep, so on Windows every
        # demo file was committed with CRLF and the runner's built-in
        # `git diff --check` failed the train on the carriage returns. The
        # committed blob is what the runner reads, so assert on that.
        walkthrough = self._walkthrough("crlf", make_repo=False)
        walkthrough._bootstrap()
        walkthrough._commit_seed()
        walkthrough._make_agent_branch("agent/probe", {"app/probe.py": "PROBE = 1\n"})

        for ref, path in (
            ("main", "app/config.py"),
            ("main", "tests/test_config.py"),
            ("main", ".mergetrain.yaml"),
            ("agent/probe", "app/probe.py"),
        ):
            blob = subprocess.run(
                ["git", "show", f"{ref}:{path}"],
                cwd=walkthrough.repo,
                env=walkthrough.env,
                check=True,
                capture_output=True,
            ).stdout
            self.assertNotIn(b"\r", blob, f"{ref}:{path} was committed with CRLF")

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_generated_commands_run_through_the_platform_shell(self) -> None:
        walkthrough = self._walkthrough("space in name", make_repo=False)
        walkthrough._bootstrap()
        walkthrough._commit_seed()

        config = load_config(repo=walkthrough.repo)
        for command in (config.gates[0].run, config.deploy.verify[0].run):
            completed = subprocess.run(
                command,
                shell=True,
                cwd=walkthrough.repo,
                env=walkthrough.env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"{command}\n{completed.stdout}\n{completed.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
