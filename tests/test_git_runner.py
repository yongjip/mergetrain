from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


def py_path(path: Path | str) -> str:
    """A filesystem path safe to embed inside a Python string literal.

    Windows paths contain backslashes, which a ``python -c "... '{path}' ..."``
    gate would read as escape sequences (``C:\\Users`` -> ``\\U...``). Forward
    slashes are valid in the literal and pathlib accepts them on every OS.
    """

    return str(path).replace("\\", "/")


def _clear_readonly(func, path, _exc):
    # Git marks loose objects and pack files read-only; Windows refuses to
    # delete a read-only file, so rmtree of a repo raises WinError 5. Clear the
    # bit and retry — the POSIX default already tolerates this.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree(path: Path | str) -> None:
    """``shutil.rmtree`` that also removes read-only files (Windows git repos)."""

    kwargs = {"onexc": _clear_readonly}
    if sys.version_info < (3, 12):
        kwargs = {"onerror": lambda f, p, _e: _clear_readonly(f, p, None)}
    shutil.rmtree(path, **kwargs)

from mergetrain.cli import main
from mergetrain.config import load_config
from mergetrain.errors import CommandFailed, redact_secrets
from mergetrain.git_runner import GitRunner, _dashboard_command, run_shell
from mergetrain.store import (
    cancel_job,
    claim_all_queued,
    claim_deploy_batch,
    connect,
    enqueue_job,
    get_job,
    get_lock,
    list_run_events,
    release_runner_lock,
)


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed\n{completed.stdout}\n{completed.stderr}")
    return completed.stdout.strip()


def make_demo_repo(
    root: Path,
    *,
    gate_command: str = "",
    verify_command: str | None = None,
    reuse_enabled: bool = False,
    reuse_max_age_minutes: int = 60,
    reuse_on_mismatch: str = "rerun",
    always_rerun_on_deploy: bool = False,
    fingerprint_command: str | None = None,
) -> tuple[Path, Path]:
    """Create a remote+clone with a ``feature/a`` branch and return (repo, marker).

    The gate appends to ``marker`` once per gate run so tests can assert the train
    gate executed exactly once over the assembled batch.
    """
    repo = root / "repo"
    remote = root / "remote.git"
    git(root, "init", "--bare", str(remote))
    git(root, "clone", str(remote), str(repo))
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test User")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "app.txt")
    git(repo, "commit", "-m", "base")
    git(repo, "branch", "-M", "main")
    git(repo, "push", "-u", "origin", "main")
    git(repo, "switch", "-c", "feature/a")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-m", "a")
    git(repo, "switch", "main")
    marker = root / "gate-count.txt"
    gate_command = gate_command or (
        f"{sys.executable} -c \"from pathlib import Path; p=Path('{py_path(marker)}'); "
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\""
    )
    verify_config = "  verify: []"
    if verify_command is not None:
        verify_config = f"""  verify:
    - name: live-check
      run: {verify_command}"""
    fingerprint_config = "    fingerprints: []"
    if fingerprint_command is not None:
        fingerprint_config = f"""    fingerprints:
      - name: toolchain
        run: {fingerprint_command}"""
    always_rerun_config = (
        "\n    always_rerun_on_deploy: true" if always_rerun_on_deploy else ""
    )
    config_text = f"""project:
  name: demo
state:
  db: {root / 'queue.sqlite'}
  logs: {root / 'logs'}
  worktree_root: {root / 'worktrees'}
git:
  remote: origin
  integration_branch: main
  push_refs:
    - main
queue:
  lock_ttl_minutes: 1
  daemon_interval_seconds: 1
  heartbeat_interval_seconds: 1
  command_timeout_seconds: 30
gates:
  - name: marker
    run: {gate_command}{always_rerun_config}
deploy:
{verify_config}
  reuse:
    enabled: {str(reuse_enabled).lower()}
    max_age_minutes: {reuse_max_age_minutes}
    on_mismatch: {reuse_on_mismatch}
{fingerprint_config}
"""
    (repo / ".mergetrain.yaml").write_text(config_text, encoding="utf-8")
    return repo, marker


class GitRunnerTests(unittest.TestCase):
    def test_unchanged_validated_train_reuses_gates_and_still_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            verify_marker = root / "verify.txt"
            verify = f'{sys.executable} -c "from pathlib import Path; Path(\'{py_path(verify_marker)}\').write_text(\'verified\')"'
            repo, marker = make_demo_repo(root, verify_command=verify)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                validated = runner.process_batch(conn, [job], deploy=False)[0]
                deployed = runner.process_batch(
                    conn,
                    [validated],
                    deploy=True,
                    reuse_validated=True,
                )[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.status, "deployed")
            self.assertEqual(deployed.deploy_sha, validated.validation_sha)
            self.assertEqual(deployed.reused_validation_sha, validated.validation_sha)
            self.assertTrue(validated.validation_tree_sha)
            self.assertTrue(validated.validation_gate_policy_sha)
            self.assertTrue(validated.validation_environment_sha)
            self.assertTrue(validated.validation_train_sha)
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            self.assertEqual(verify_marker.read_text(encoding="utf-8"), "verified")
            reused = [event for event in events if event.state == "reused"]
            self.assertEqual(
                [event.message for event in reused],
                ["Reused gate 1/2: diff-check", "Reused gate 2/2: marker"],
            )

    def test_reuse_preview_json_names_exact_validation_sha_without_claiming(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
            finally:
                conn.close()
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(config.state.db),
                        "run-batch",
                        "--deploy",
                        "--train-id",
                        validated.train_id,
                        "--reuse-validated",
                        "--preview",
                        "--json",
                    ]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["preview"])
            self.assertTrue(payload["reuse"]["eligible"])
            self.assertEqual(
                payload["reuse"]["reused_validation_sha"],
                validated.validation_sha,
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:a.txt")

    def test_config_authorization_reuses_but_required_gate_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(
                root,
                reuse_enabled=True,
                always_rerun_on_deploy=True,
            )
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                validated = runner.process_batch(conn, [job], deploy=False)[0]
                deployed = runner.process_batch(conn, [validated], deploy=True)[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.reused_validation_sha, validated.validation_sha)
            self.assertEqual(marker.read_text(encoding="utf-8"), "xx")
            self.assertIn("Reused gate 1/2: diff-check", [event.message for event in events])
            self.assertIn("Running gate 2/2: marker", [event.message for event in events])

    def test_environment_fingerprint_change_falls_back_to_full_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fingerprint = root / "toolchain.txt"
            fingerprint.write_text("tool-a\n", encoding="utf-8")
            repo, marker = make_demo_repo(
                root,
                reuse_enabled=True,
                # `cat` is not a Windows command; read the file portably.
                fingerprint_command=(
                    f"{sys.executable} -c \"from pathlib import Path; "
                    f"print(Path('{py_path(fingerprint)}').read_text())\""
                ),
            )
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                validated = runner.process_batch(conn, [job], deploy=False)[0]
                fingerprint.write_text("tool-b\n", encoding="utf-8")
                deployed = runner.process_batch(conn, [validated], deploy=True)[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.reused_validation_sha, "")
            self.assertEqual(marker.read_text(encoding="utf-8"), "xx")
            fallback = next(
                event
                for event in events
                if event.message == "Validated gates were not reused; rerunning all gates"
            )
            self.assertIn("environment or toolchain fingerprint changed", fallback.detail)

    def test_push_failure_is_not_reported_as_deployed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                failure = CommandFailed(
                    ["git", "push"], 1, stderr="remote rejected the update"
                )
                with patch.object(runner, "push_verified_head", side_effect=failure):
                    result = runner.process_one(conn, job, deploy=True)
            finally:
                conn.close()
            # A non-rejection push failure is AMBIGUOUS (the remote may have
            # accepted it), so it parks needs_reconcile — never a terminal
            # 'failed' that a later deploy would silently push over (guarantee #4).
            # (Marker preservation with a real claim is covered end-to-end in
            # test_reconcile.test_ambiguous_push_parks_needs_reconcile_*.)
            self.assertEqual(result.status, "needs_reconcile")
            self.assertEqual(result.push_status, "failed")
            self.assertEqual(result.verify_status, "not_run")

    def test_real_push_rejection_still_blocks_not_reconciles(self) -> None:
        # Benign check: a genuine PushRejected (protected branch / permission —
        # the remote definitely did NOT accept the push) must still finalize
        # 'blocked', NOT needs_reconcile, so the ambiguous-push fix never
        # mislabels a real rejection as an ambiguous outcome.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                rejection = CommandFailed(
                    ["git", "push"], 1,
                    stderr="! [remote rejected] main -> main (protected branch hook declined)",
                )
                with patch.object(runner, "push_verified_head", side_effect=rejection):
                    result = runner.process_one(conn, job, deploy=True)
            finally:
                conn.close()
            self.assertEqual(result.status, "blocked")

    def test_a_gate_that_mutates_the_worktree_blocks_the_deploy(self) -> None:
        # Guarantee #1: gates are verification, not mutation. A gate that dirties
        # (or commits to) the integration worktree after the deploy sha is
        # recorded blocks the deploy — a tree differing from the tested sha is
        # never shipped, and the push is never reached.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root, gate_command="echo x > gate-dirty.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                result = GitRunner(config).process_batch(conn, [job], deploy=True)[0]
                pending = git(
                    repo, "for-each-ref", "--format=%(refname)", "refs/mergetrain/pending/"
                )
            finally:
                conn.close()
            self.assertEqual(result.status, "blocked")
            self.assertIn("tree", result.note.lower())
            self.assertEqual(pending, "")  # never reached the push / marker
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:a.txt")
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:a.txt")

    def test_unexpected_post_push_error_preserves_deployed_truth(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            verify = f'{sys.executable} -c "import sys; sys.exit(0)"'
            repo, _marker = make_demo_repo(root, verify_command=verify)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                with patch.object(
                    runner,
                    "_run_verify_hooks",
                    side_effect=RuntimeError("verification crashed"),
                ):
                    result = runner.process_one(conn, job, deploy=True)
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(result.status, "deployed")
            self.assertEqual(result.push_status, "succeeded")
            self.assertEqual(result.verify_status, "failed")
            self.assertIn("post-push completion warning", result.note)
            self.assertEqual(events[-1].phase, "complete")
            self.assertEqual(events[-1].state, "warning")
            self.assertEqual(git(root / "remote.git", "show", "main:a.txt"), "a")

    def test_single_deploy_records_verify_success_and_failure(self) -> None:
        for returncode, expected_verify, expected_event_state in [
            (0, "succeeded", "success"),
            (7, "failed", "warning"),
        ]:
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                verify = f'{sys.executable} -c "import sys; sys.exit({returncode})"'
                repo, _marker = make_demo_repo(root, verify_command=verify)
                config = load_config(repo=repo)
                conn = connect(config.state.db)
                try:
                    job = enqueue_job(conn, task="a", branch="feature/a")
                    result = GitRunner(config).process_one(conn, job, deploy=True)
                    events = list_run_events(conn)
                finally:
                    conn.close()
                self.assertEqual(result.status, "deployed")
                self.assertEqual(result.push_status, "succeeded")
                self.assertEqual(result.verify_status, expected_verify)
                self.assertEqual(events[-1].phase, "complete")
                self.assertEqual(events[-1].state, expected_event_state)
                if returncode:
                    self.assertIn("verification needs attention", events[-1].message)

    def test_batch_deploy_records_verify_success_and_failure(self) -> None:
        for returncode, expected_verify, expected_event_state in [
            (0, "succeeded", "success"),
            (9, "failed", "warning"),
        ]:
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                verify = f'{sys.executable} -c "import sys; sys.exit({returncode})"'
                repo, _marker = make_demo_repo(root, verify_command=verify)
                config = load_config(repo=repo)
                conn = connect(config.state.db)
                try:
                    job = enqueue_job(conn, task="a", branch="feature/a")
                    result = GitRunner(config).process_batch(conn, [job], deploy=True)[0]
                    events = list_run_events(conn)
                finally:
                    conn.close()
                self.assertEqual(result.status, "deployed")
                self.assertEqual(result.push_status, "succeeded")
                self.assertEqual(result.verify_status, expected_verify)
                self.assertEqual(events[-1].phase, "complete")
                self.assertEqual(events[-1].state, expected_event_state)

    def test_deploy_clears_pending_marker_and_pin_ref(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                result = GitRunner(config).process_batch(conn, [job], deploy=True)[0]
            finally:
                conn.close()
            self.assertEqual(result.status, "deployed")
            # The write-ahead marker is cleared once the deploy is finalized,
            self.assertEqual(result.pending_deploy_sha, "")
            # and a clean deploy leaves no pin ref behind.
            pending = git(repo, "for-each-ref", "--format=%(refname)", "refs/mergetrain/pending/")
            self.assertEqual(pending, "")

    def test_dashboard_command_masks_obvious_secret_values(self) -> None:
        rendered = _dashboard_command(
            "TEST_TOKEN=fixture-value run-check --password fixture-password"
        )
        self.assertEqual(
            rendered,
            "TEST_TOKEN=[redacted] run-check --password [redacted]",
        )

    def test_command_failed_str_redacts_inline_secrets(self) -> None:
        # redact_secrets is the single masking primitive; CommandFailed.__str__
        # runs through it so the persisted job note never carries an inline
        # credential, matching what the dashboard already masks live.
        self.assertEqual(
            redact_secrets("deploy API_TOKEN=sk-abc123 --password hunter2"),
            "deploy API_TOKEN=[redacted] --password [redacted]",
        )
        exc = CommandFailed(["run-check", "--token", "sk-secret-xyz"], 1, stderr="boom")
        rendered = str(exc)
        self.assertNotIn("sk-secret-xyz", rendered)
        self.assertIn("--token [redacted]", rendered)
        self.assertIn("boom", rendered)

    def test_failed_gate_note_redacts_inline_command_secret(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # The secret is inline in the gate command itself (not just its
            # output), so it lands in CommandFailed.command -> the job note.
            gate = (
                f'{sys.executable} -c "import sys; sys.exit(5)" '
                "--token sk-inline-secret-value"
            )
            repo, _marker = make_demo_repo(root, gate_command=gate)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                result = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
            finally:
                conn.close()
            self.assertEqual(result.status, "failed")
            self.assertIn("[redacted]", result.note)
            self.assertNotIn("sk-inline-secret-value", result.note)

    def test_command_output_is_kept_out_of_structured_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            secret = "fixture-secret-output"
            gate = (
                f'{sys.executable} -c "import sys; '
                "import os; print(os.environ['FIXTURE_EVENT_SECRET'], "
                "file=sys.stderr); sys.exit(5)\""
            )
            repo, _marker = make_demo_repo(root, gate_command=gate)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                with patch.dict(
                    os.environ, {"FIXTURE_EVENT_SECRET": secret}, clear=False
                ):
                    result = GitRunner(config).process_batch(
                        conn, [job], deploy=False
                    )[0]
                events = list_run_events(conn, limit=200)
            finally:
                conn.close()
            self.assertIn(secret, result.note)
            serialized_events = json.dumps(
                [event.to_dict() for event in events], ensure_ascii=False
            )
            self.assertNotIn(secret, serialized_events)
            self.assertIn("exit_code=5", serialized_events)

    def test_managed_command_timeout_terminates_process_group(self) -> None:
        # ignore_cleanup_errors: this test kills a subprocess mid-run; on
        # Windows the OS may still hold the killed process's cwd/pipe handles
        # when TemporaryDirectory tears down (WinError 32). Production worktree
        # cleanup is best-effort + gc for the same reason, so tolerate it here.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            started = time.monotonic()
            with self.assertRaises(CommandFailed) as raised:
                run_shell(
                    f'{sys.executable} -c "import time; time.sleep(10)"',
                    cwd=td,
                    env=os.environ.copy(),
                    log=io.StringIO(),
                    timeout_seconds=0.2,
                    pulse_interval_seconds=0.1,
                )
            self.assertEqual(raised.exception.returncode, 124)
            self.assertLess(time.monotonic() - started, 3)

    def test_batch_merges_jobs_and_runs_gate_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                results = GitRunner(config).process_batch(conn, [job], deploy=False)
                stored = get_job(conn, job.id)
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual([result.status for result in results], ["validated"])
            self.assertEqual(stored.status, "validated")
            self.assertTrue(stored.train_id)
            self.assertEqual(stored.train_size, 1)
            self.assertTrue(stored.validated_at)
            self.assertTrue(stored.validation_base_sha)
            self.assertEqual(stored.validation_sha, stored.deploy_sha)
            self.assertEqual(stored.validated_head_sha, git(repo, "rev-parse", "feature/a"))
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            self.assertIn("Merged feature/a", [event.message for event in events])
            self.assertIn("Running gate 2/2: marker", [event.message for event in events])
            running_gate = next(event for event in events if event.message == "Running gate 2/2: marker")
            self.assertEqual(running_gate.detail, config.gates[0].run)

    def test_validated_batch_deploys_after_integration_ref_moves(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(
                    conn,
                    task="a",
                    branch="feature/a",
                    base_sha=git(repo, "rev-parse", "origin/main"),
                    head_sha=git(repo, "rev-parse", "feature/a"),
                )
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                (repo / "base-moved.txt").write_text("moved\n", encoding="utf-8")
                git(repo, "add", "base-moved.txt")
                git(repo, "commit", "-m", "move integration")
                git(repo, "push", "origin", "main")
                deployed = GitRunner(config).process_batch(
                    conn,
                    [validated],
                    deploy=True,
                    reuse_validated=True,
                )[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.status, "deployed")
            self.assertEqual(deployed.push_status, "succeeded")
            self.assertEqual(deployed.verify_status, "not_configured")
            self.assertEqual(deployed.reused_validation_sha, "")
            self.assertNotEqual(deployed.validation_base_sha, deployed.deploy_sha)
            self.assertEqual(git(root / "remote.git", "show", "main:a.txt"), "a")
            self.assertEqual(git(root / "remote.git", "show", "main:base-moved.txt"), "moved")
            self.assertEqual(marker.read_text(encoding="utf-8"), "xx")
            fallback = next(
                event
                for event in events
                if event.message == "Validated gates were not reused; rerunning all gates"
            )
            self.assertIn("integration ref moved", fallback.detail)

    def test_changed_branch_head_blocks_validated_train(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                git(repo, "switch", "feature/a")
                (repo / "changed.txt").write_text("changed\n", encoding="utf-8")
                git(repo, "add", "changed.txt")
                git(repo, "commit", "-m", "change after validation")
                git(repo, "switch", "main")
                result = GitRunner(config).process_batch(
                    conn,
                    [validated],
                    deploy=True,
                    reuse_validated=True,
                )[0]
            finally:
                conn.close()
            self.assertEqual(result.status, "blocked")
            self.assertIn("HEAD changed since validation", result.note)
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:a.txt")

    def test_changed_gate_policy_falls_back_to_full_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first_marker = root / "first-gate.txt"
            second_marker = root / "second-gate.txt"
            first_gate = f'{sys.executable} -c "from pathlib import Path; Path(\'{py_path(first_marker)}\').write_text(\'x\')"'
            second_gate = f'{sys.executable} -c "from pathlib import Path; Path(\'{py_path(second_marker)}\').write_text(\'y\')"'
            repo, _marker = make_demo_repo(root, gate_command=first_gate)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                config.config_path.write_text(
                    config.config_path.read_text(encoding="utf-8").replace(
                        first_gate, second_gate
                    ),
                    encoding="utf-8",
                )
                changed_config = load_config(repo=repo)
                deployed = GitRunner(changed_config).process_batch(
                    conn,
                    [validated],
                    deploy=True,
                    reuse_validated=True,
                )[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.reused_validation_sha, "")
            self.assertEqual(first_marker.read_text(encoding="utf-8"), "x")
            self.assertEqual(second_marker.read_text(encoding="utf-8"), "y")
            fallback = next(
                event
                for event in events
                if event.message == "Validated gates were not reused; rerunning all gates"
            )
            self.assertIn("gate or fingerprint policy changed", fallback.detail)

    def test_missing_validation_commit_falls_back_to_full_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                conn.execute(
                    "UPDATE deploy_queue SET validation_sha = ? WHERE id = ?",
                    ("f" * 40, validated.id),
                )
                conn.commit()
                validated = get_job(conn, validated.id)
                deployed = GitRunner(config).process_batch(
                    conn,
                    [validated],
                    deploy=True,
                    reuse_validated=True,
                )[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.reused_validation_sha, "")
            self.assertEqual(marker.read_text(encoding="utf-8"), "xx")
            fallback = next(
                event
                for event in events
                if event.message == "Validated gates were not reused; rerunning all gates"
            )
            self.assertIn("validation commit is missing", fallback.detail)

    def test_stale_validation_falls_back_to_full_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root, reuse_max_age_minutes=1)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                conn.execute(
                    "UPDATE deploy_queue SET validated_at = ? WHERE id = ?",
                    ("2000-01-01T00:00:00Z", validated.id),
                )
                conn.commit()
                validated = get_job(conn, validated.id)
                deployed = GitRunner(config).process_batch(
                    conn,
                    [validated],
                    deploy=True,
                    reuse_validated=True,
                )[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.reused_validation_sha, "")
            self.assertEqual(marker.read_text(encoding="utf-8"), "xx")
            fallback = next(
                event
                for event in events
                if event.message == "Validated gates were not reused; rerunning all gates"
            )
            self.assertIn("older than the configured reuse age", fallback.detail)

    def test_mismatch_policy_can_fail_closed_without_rerunning_or_pushing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(
                root,
                reuse_max_age_minutes=1,
                reuse_on_mismatch="fail",
            )
            config = load_config(repo=repo)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            token = ""
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                conn.execute(
                    "UPDATE deploy_queue SET validated_at = ? WHERE id = ?",
                    ("2000-01-01T00:00:00Z", validated.id),
                )
                conn.commit()
                claimed = claim_deploy_batch(
                    conn,
                    owner=owner,
                    train_id=validated.train_id,
                )
                token = claimed[0].claim_token
                result = GitRunner(config).process_batch(
                    conn,
                    claimed,
                    deploy=True,
                    owner=owner,
                    reuse_validated=True,
                )[0]
            finally:
                if token:
                    release_runner_lock(conn, owner=owner, token=token)
                conn.close()
            self.assertEqual(result.status, "blocked")
            self.assertIn("failed closed", result.note)
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:a.txt")

    def test_batch_refreshes_lease_while_holding_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            try:
                enqueue_job(conn, task="a", branch="feature/a")
                claimed = claim_all_queued(conn, owner=owner, ttl_minutes=-1)
                before = get_lock(conn)
                results = GitRunner(config).process_batch(
                    conn, claimed, deploy=False, owner=owner, ttl_minutes=30
                )
                after = get_lock(conn)
            finally:
                conn.close()
            self.assertEqual([result.status for result in results], ["validated"])
            self.assertIsNotNone(before)
            self.assertIsNotNone(after)
            # Lease advanced from expired (past) to valid (~30 min ahead).
            self.assertGreater(after.expires_at, before.expires_at)

    def test_long_gate_heartbeats_and_cooperatively_cancels(self) -> None:
        # ignore_cleanup_errors: cancelling mid-gate kills the gate subprocess
        # and tears down its integration worktree; on Windows the OS may still
        # hold those handles at TemporaryDirectory cleanup (WinError 32), the
        # same best-effort situation production handles via gc.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            root = Path(td)
            gate = f'{sys.executable} -c "import time; time.sleep(10)"'
            repo, _marker = make_demo_repo(root, gate_command=gate)
            config = load_config(repo=repo)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            job = enqueue_job(conn, task="a", branch="feature/a")
            claimed = claim_all_queued(conn, owner=owner, ttl_minutes=1)
            token = claimed[0].claim_token
            conn.close()

            results: list = []
            errors: list[Exception] = []

            def run() -> None:
                worker_conn = connect(config.state.db)
                try:
                    results.extend(
                        GitRunner(config).process_batch(
                            worker_conn,
                            claimed,
                            deploy=False,
                            owner=owner,
                            ttl_minutes=1,
                        )
                    )
                except Exception as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
                finally:
                    worker_conn.close()

            worker = threading.Thread(target=run)
            worker.start()
            time.sleep(0.5)
            control = connect(config.state.db)
            active = get_job(control, job.id)
            self.assertTrue(active.log_path)
            self.assertTrue(Path(active.log_path).is_file())
            control.execute(
                "UPDATE locks SET expires_at = '2000-01-01T00:00:00Z' WHERE token = ?",
                (token,),
            )
            control.commit()
            time.sleep(1.5)
            self.assertGreater(get_lock(control).expires_at, "2000-01-01T00:00:00Z")
            cancel_job(control, job.id)
            control.close()
            worker.join(timeout=6)

            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual([result.status for result in results], ["canceled"])
            cleanup = connect(config.state.db)
            try:
                release_runner_lock(cleanup, owner=owner, token=token)
            finally:
                cleanup.close()


def add_branch(repo: Path, name: str, filename: str) -> None:
    git(repo, "switch", "-c", name, "main")
    (repo / filename).write_text(f"{name}\n", encoding="utf-8")
    git(repo, "add", filename)
    git(repo, "commit", "-m", name)
    git(repo, "switch", "main")


class BisectIsolationTests(unittest.TestCase):
    def test_bisect_isolates_single_bad_job_and_revalidates_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate = (
                f"{sys.executable} -c \"import sys, pathlib; "
                "sys.exit(1 if pathlib.Path('bad.txt').exists() else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            add_branch(repo, "agent/bad", "bad.txt")
            add_branch(repo, "agent/b", "b.txt")
            add_branch(repo, "agent/c", "c.txt")
            add_branch(repo, "agent/d", "d.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [enqueue_job(conn, task="a", branch="feature/a")]
                jobs.extend(
                    enqueue_job(conn, task=name, branch=f"agent/{name}")
                    for name in ("bad", "b", "c", "d")
                )
                results = GitRunner(config).process_batch(conn, jobs, deploy=False)
                stored = {job.id: get_job(conn, job.id) for job in jobs}
                events = list_run_events(conn)
            finally:
                conn.close()
            by_branch = {job.branch: stored[job.id] for job in jobs}
            self.assertEqual(by_branch["agent/bad"].status, "failed")
            self.assertIn("bisect isolation", by_branch["agent/bad"].note)
            self.assertEqual(by_branch["agent/bad"].conflict_with, "")
            for branch in ("feature/a", "agent/b", "agent/c", "agent/d"):
                self.assertEqual(by_branch[branch].status, "validated", branch)
                self.assertEqual(by_branch[branch].conflict_with, "")
                self.assertEqual(by_branch[branch].train_size, 4, branch)
            self.assertEqual(len(results), 5)
            messages = [event.message for event in events]
            self.assertIn("Train gate failed; bisecting 5 jobs", messages)
            self.assertIn("Bisect isolation complete: 4 job(s) rejoin the train", messages)

    def test_bisect_reports_semantic_conflict_pair_with_conflict_with(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate = (
                f"{sys.executable} -c \"import sys, pathlib; "
                "sys.exit(1 if (pathlib.Path('left.txt').exists() "
                "and pathlib.Path('right.txt').exists()) else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            add_branch(repo, "agent/left", "left.txt")
            add_branch(repo, "agent/right", "right.txt")
            add_branch(repo, "agent/ok1", "ok1.txt")
            add_branch(repo, "agent/ok2", "ok2.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [
                    enqueue_job(conn, task=name, branch=f"agent/{name}")
                    for name in ("left", "right", "ok1", "ok2")
                ]
                results = GitRunner(config).process_batch(conn, jobs, deploy=False)
                stored = {job.branch: get_job(conn, job.id) for job in jobs}
                ids = {job.branch: job.id for job in jobs}
            finally:
                conn.close()
            left, right = stored["agent/left"], stored["agent/right"]
            self.assertEqual(left.status, "blocked")
            self.assertEqual(right.status, "blocked")
            self.assertEqual(left.conflict_with, str(ids["agent/right"]))
            self.assertEqual(right.conflict_with, str(ids["agent/left"]))
            self.assertIn("semantic conflict", left.note)
            self.assertIn("agent/right", left.note)
            self.assertIn("agent/left", right.note)
            for branch in ("agent/ok1", "agent/ok2"):
                self.assertEqual(stored[branch].status, "validated", branch)
                self.assertEqual(stored[branch].conflict_with, "")
            self.assertEqual(len(results), 4)

    def test_bisect_reports_three_way_conflict_and_frees_innocent_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate = (
                f"{sys.executable} -c \"import sys, pathlib; "
                "sys.exit(1 if (pathlib.Path('t1.txt').exists() "
                "and pathlib.Path('t3.txt').exists() "
                "and pathlib.Path('t5.txt').exists()) else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            for name in ("t1", "t2", "t3", "t4", "t5"):
                add_branch(repo, f"agent/{name}", f"{name}.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [
                    enqueue_job(conn, task=name, branch=f"agent/{name}")
                    for name in ("t1", "t2", "t3", "t4", "t5")
                ]
                GitRunner(config).process_batch(conn, jobs, deploy=False)
                stored = {job.branch: get_job(conn, job.id) for job in jobs}
                ids = {job.branch: job.id for job in jobs}
            finally:
                conn.close()
            conflicted = ("agent/t1", "agent/t3", "agent/t5")
            for branch in conflicted:
                self.assertEqual(stored[branch].status, "blocked", branch)
                partners = {
                    int(part) for part in stored[branch].conflict_with.split(",")
                }
                expected = {ids[other] for other in conflicted if other != branch}
                self.assertEqual(partners, expected, branch)
                self.assertIn("semantic conflict", stored[branch].note)
            for branch in ("agent/t2", "agent/t4"):
                self.assertEqual(stored[branch].status, "validated", branch)
                self.assertEqual(stored[branch].conflict_with, "")

    def test_bisect_masked_failure_does_not_blame_innocent_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # bad fails alone but is masked by fix; the real conflict is x+y.
            gate = (
                f"{sys.executable} -c \"import sys, pathlib; e=pathlib.Path; "
                "sys.exit(1 if ((e('bad.txt').exists() and not e('fix.txt').exists()) "
                "or (e('x.txt').exists() and e('y.txt').exists())) else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            for name in ("x", "l2", "bad", "fix", "y"):
                add_branch(repo, f"agent/{name}", f"{name}.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [
                    enqueue_job(conn, task=name, branch=f"agent/{name}")
                    for name in ("x", "l2", "bad", "fix", "y")
                ]
                GitRunner(config).process_batch(conn, jobs, deploy=False)
                stored = {job.branch: get_job(conn, job.id) for job in jobs}
                ids = {job.branch: job.id for job in jobs}
            finally:
                conn.close()
            self.assertEqual(stored["agent/x"].status, "blocked")
            self.assertEqual(stored["agent/y"].status, "blocked")
            self.assertEqual(stored["agent/x"].conflict_with, str(ids["agent/y"]))
            self.assertEqual(stored["agent/y"].conflict_with, str(ids["agent/x"]))
            # bad is masked by fix in the surviving combination, which
            # genuinely passes — nobody gets falsely blamed.
            for branch in ("agent/l2", "agent/bad", "agent/fix"):
                self.assertEqual(stored[branch].status, "validated", branch)
                self.assertEqual(stored[branch].conflict_with, "")

    def test_bisect_falls_back_to_linear_when_failure_does_not_reproduce(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            counter = root / "count.txt"
            gate = (
                f"{sys.executable} -c \"import pathlib, sys; "
                f"p = pathlib.Path('{py_path(counter)}'); "
                "n = (int(p.read_text()) + 1) if p.exists() else 1; "
                "p.write_text(str(n)); sys.exit(1 if n == 1 else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            for name in ("b", "c", "d"):
                add_branch(repo, f"agent/{name}", f"{name}.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [enqueue_job(conn, task="a", branch="feature/a")]
                jobs.extend(
                    enqueue_job(conn, task=name, branch=f"agent/{name}")
                    for name in ("b", "c", "d")
                )
                GitRunner(config).process_batch(conn, jobs, deploy=False)
                stored = [get_job(conn, job.id) for job in jobs]
                events = list_run_events(conn, limit=200)
            finally:
                conn.close()
            self.assertEqual([job.status for job in stored], ["validated"] * 4)
            self.assertEqual([job.conflict_with for job in stored], [""] * 4)
            messages = [event.message for event in events]
            self.assertIn("Bisect inconclusive; isolating jobs one-by-one", messages)

    def test_small_train_keeps_linear_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate = (
                f"{sys.executable} -c \"import sys, pathlib; "
                "sys.exit(1 if pathlib.Path('bad.txt').exists() else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            add_branch(repo, "agent/bad", "bad.txt")
            add_branch(repo, "agent/b", "b.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [enqueue_job(conn, task="a", branch="feature/a")]
                jobs.append(enqueue_job(conn, task="bad", branch="agent/bad"))
                jobs.append(enqueue_job(conn, task="b", branch="agent/b"))
                GitRunner(config).process_batch(conn, jobs, deploy=False)
                stored = {job.branch: get_job(conn, job.id) for job in jobs}
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(stored["agent/bad"].status, "failed")
            self.assertEqual(stored["feature/a"].status, "validated")
            self.assertEqual(stored["agent/b"].status, "validated")
            messages = [event.message for event in events]
            self.assertIn("Train gate failed; isolating jobs", messages)
            self.assertNotIn("Train gate failed; bisecting 3 jobs", messages)


class PushRejectionTests(unittest.TestCase):
    def test_classifier_distinguishes_permission_from_other_failures(self) -> None:
        from mergetrain.git_runner import is_push_rejection

        self.assertTrue(is_push_rejection("remote: error: GH006 Protected branch update failed"))
        self.assertTrue(is_push_rejection("! [remote rejected] main -> main (protected branch hook declined)"))
        self.assertTrue(is_push_rejection("remote: Changes must be made through a pull request."))
        self.assertFalse(is_push_rejection("! [rejected] main -> main (non-fast-forward)"))
        self.assertFalse(is_push_rejection("fatal: could not read from remote repository"))

    def test_inspect_categorizes_a_push_blocked_job_as_push_rejected(self) -> None:
        from mergetrain.models import Job
        from mergetrain.observability import job_outcome

        job = Job(
            id=1, task="a", branch="feature/a", status="blocked",
            push_status="failed",
            note="remote rejected the push (protected branch, required pull request)",
        )
        self.assertEqual(job_outcome(job)["category"], "push_rejected")

    def test_protected_branch_push_lands_blocked_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            # A pre-receive hook that rejects with a protected-branch message,
            # so the real push path exercises the rejection classifier.
            hook = root / "remote.git" / "hooks" / "pre-receive"
            hook.write_text(
                "#!/bin/sh\necho 'remote: error: GH006 Protected branch update failed' 1>&2\nexit 1\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                result = GitRunner(config).process_batch(conn, [job], deploy=True)[0]
            finally:
                conn.close()
            # Not `failed` (which means "bad code, rebase") — this is a repo
            # policy issue the operator must fix, so the job is blocked.
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.push_status, "failed")
            self.assertIn("rejected the push", result.note)


class GcWorktreeGuardTests(unittest.TestCase):
    def test_gc_never_removes_a_live_runners_worktree(self) -> None:
        # Blocker: gc --apply force-removed the worktree a running deploy was
        # merging/gating inside. A live runner's worktree must be protected.
        from mergetrain.git_runner import apply_gc, find_worktree_gc_candidates

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            wt_root = config.state.worktree_root
            wt_root.mkdir(parents=True, exist_ok=True)
            live = wt_root / f"{config.project.name}-mergetrain-1-abc"
            orphan = wt_root / f"{config.project.name}-mergetrain-2-def"
            live.mkdir()
            orphan.mkdir()

            # The live worktree is reported as protected in the candidate list...
            cands = find_worktree_gc_candidates(config, protect=[str(live)])
            protected = [c for c in cands if c.get("protected")]
            self.assertEqual([c["path"] for c in protected], [str(live)])

            # ...and apply never removes it, while the orphan is swept.
            apply_gc(config, protect=[str(live)])
            self.assertTrue(live.is_dir(), "live runner worktree was destroyed")
            self.assertFalse(orphan.exists(), "orphan worktree should be gc'd")

    def test_gc_rechecks_a_runner_that_started_after_the_snapshot(self) -> None:
        # #84 defect 5: the protect list is a snapshot taken before apply_gc
        # runs. A runner that acquires the lock AFTER it is built is absent from
        # protect — but a per-deletion recheck of the live lock still spares its
        # worktree, while a genuine orphan is still swept.
        from mergetrain.git_runner import apply_gc

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            wt_root = config.state.worktree_root
            wt_root.mkdir(parents=True, exist_ok=True)
            started_late = wt_root / f"{config.project.name}-mergetrain-9-late"
            orphan = wt_root / f"{config.project.name}-mergetrain-2-def"
            started_late.mkdir()
            orphan.mkdir()

            # protect is empty (the snapshot predates the runner), but the live
            # lock now points at started_late.
            result = apply_gc(
                config,
                protect=[],
                live_worktree_now=lambda: str(started_late),
            )
            self.assertTrue(
                started_late.is_dir(),
                "a runner that started after the snapshot was destroyed",
            )
            self.assertFalse(orphan.exists(), "the genuine orphan should still be gc'd")
            self.assertNotIn(
                str(started_late),
                [c["path"] for c in result["removed_worktrees"]],
            )


class MergeConflictTests(unittest.TestCase):
    """Real git-level merge conflicts during assembly (the BisectIsolation
    suite only fakes semantic conflicts via gate exit codes)."""

    def test_real_merge_conflict_blocks_the_job_and_pushes_nothing(self) -> None:
        from mergetrain.observability import job_outcome

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            # the branch edits app.txt line 1...
            git(repo, "switch", "-c", "agent/x", "main")
            (repo / "app.txt").write_text("x-change\n", encoding="utf-8")
            git(repo, "add", "app.txt")
            git(repo, "commit", "-m", "x")
            # ...and the integration ref moves to a conflicting state on the same line
            git(repo, "switch", "main")
            (repo / "app.txt").write_text("main-change\n", encoding="utf-8")
            git(repo, "add", "app.txt")
            git(repo, "commit", "-m", "move main")
            git(repo, "push", "origin", "main")

            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="x", branch="agent/x")
                result = GitRunner(config).process_batch(conn, [job], deploy=True)[0]
                stored = get_job(conn, job.id)
                pending = git(
                    repo, "for-each-ref", "--format=%(refname)", "refs/mergetrain/pending/"
                )
            finally:
                conn.close()

            self.assertEqual(stored.status, "blocked")
            self.assertIn("conflict", stored.note.lower())
            self.assertEqual(job_outcome(stored)["category"], "merge_conflict")
            self.assertEqual(result.push_status, "not_run")
            # nothing shipped: the remote still holds the integration change (not
            # the branch's), no write-ahead marker was written, no gate ran.
            self.assertEqual(git(root / "remote.git", "show", "main:app.txt"), "main-change")
            self.assertEqual(pending, "")
            self.assertFalse(marker.exists())

    def test_merge_conflict_isolates_one_job_and_siblings_still_deploy(self) -> None:
        from mergetrain.observability import job_outcome

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            # two branches, each editing the SAME line of app.txt differently
            for name, content in (("agent/x", "x-change\n"), ("agent/y", "y-change\n")):
                git(repo, "switch", "-c", name, "main")
                (repo / "app.txt").write_text(content, encoding="utf-8")
                git(repo, "add", "app.txt")
                git(repo, "commit", "-m", name)
                git(repo, "switch", "main")

            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [
                    enqueue_job(conn, task="x", branch="agent/x"),
                    enqueue_job(conn, task="y", branch="agent/y"),
                ]
                GitRunner(config).process_batch(conn, jobs, deploy=True)
                stored = {job.branch: get_job(conn, job.id) for job in jobs}
            finally:
                conn.close()

            # assembly merges in list order: agent/x lands, agent/y conflicts and
            # is isolated — the innocent sibling still gates once and deploys.
            self.assertEqual(stored["agent/x"].status, "deployed")
            self.assertEqual(stored["agent/x"].push_status, "succeeded")
            self.assertEqual(stored["agent/y"].status, "blocked")
            self.assertEqual(job_outcome(stored["agent/y"])["category"], "merge_conflict")
            # only the sibling's change reached the remote
            self.assertEqual(git(root / "remote.git", "show", "main:app.txt"), "x-change")


class JobOutcomeCategoryTests(unittest.TestCase):
    def test_gate_named_push_is_not_mislabeled_push_failed(self) -> None:
        from mergetrain.models import Job
        from mergetrain.observability import job_outcome

        # A gate failure whose note merely contains "push" (e.g. a gate called
        # "no-force-push") ran before any push — push_status stays not_run, so it
        # must categorize as gate_failed, not push_failed, which would steer
        # remediation toward branch-protection instead of the failing gate.
        gate = Job(
            id=1, task="a", branch="agent/a", status="failed",
            push_status="not_run", note="gate 'no-force-push' failed: exit 1",
        )
        self.assertEqual(job_outcome(gate)["category"], "gate_failed")

        # A genuine push failure is still push_failed — via the structured field.
        pushed = Job(
            id=2, task="a", branch="agent/b", status="failed",
            push_status="failed", note="remote rejected the update",
        )
        self.assertEqual(job_outcome(pushed)["category"], "push_failed")


if __name__ == "__main__":
    unittest.main()
