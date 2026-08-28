"""Direct evidence for correctness-critical state transitions.

These tests close the explicit execution-evidence gaps recorded in
``docs/development.md``. They exercise transaction and remote-state boundaries,
not just the final status label.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Reuse the real bare-remote fixture when this module runs by itself as well as
# under discovery/pytest.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_runner import git, make_demo_repo

from mergetrain import git_runner as git_runner_module
from mergetrain import recovery as recovery_module
from mergetrain.config import load_config
from mergetrain.errors import CancellationRequested, MergetrainError, QueueError
from mergetrain.git_runner import GitRunner
from mergetrain.recovery import force_unlock
from mergetrain.store import (
    acquire_runner_lock,
    cancel_job,
    claim_all_queued,
    connect,
    enqueue_job,
    get_job,
    get_lock,
    list_run_events,
    utc_now,
)

DEAD_OWNER = "ghost:999999"


def _refs(repo: Path, prefix: str) -> str:
    return git(repo, "for-each-ref", "--format=%(refname)", prefix)


def _stage_in_progress(conn, job_id: int, token: str) -> None:  # type: ignore[no-untyped-def]
    conn.execute(
        "UPDATE deploy_queue SET status='in_progress', claim_token=?, started_at=? "
        "WHERE id=?",
        (token, utc_now(), job_id),
    )
    conn.commit()


class DeadOwnerLockEvidenceTests(unittest.TestCase):
    def test_dead_owner_lock_is_cleared_split_and_audited_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                lock = acquire_runner_lock(conn, owner=DEAD_OWNER)
                job = enqueue_job(conn, task="orphan", branch="feature/a")
                _stage_in_progress(conn, job.id, lock.token)

                outcome = force_unlock(config, conn, force=False)
                stored = get_job(conn, job.id)
                events = list_run_events(conn)

                self.assertTrue(outcome.cleared)
                self.assertEqual(outcome.liveness, "dead")
                self.assertEqual(outcome.reason, "dead owner lock cleared")
                self.assertIsNone(get_lock(conn))
                self.assertEqual(stored.status, "queued")
                self.assertEqual(stored.claim_token, "")

                audit = events[-1]
                self.assertEqual(outcome.audit_event_id, audit.id)
                self.assertEqual((audit.phase, audit.state), ("unlock", "cleared"))
                self.assertIn(DEAD_OWNER, audit.message)
                detail = json.loads(audit.detail)
                self.assertEqual(detail["owner"], DEAD_OWNER)
                self.assertEqual(detail["liveness"], "dead")
                self.assertFalse(detail["forced"])
                self.assertNotIn(lock.token, audit.detail)
            finally:
                conn.close()

    def test_force_unlock_does_not_clear_a_lock_replaced_during_remote_probe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            owner = f"runner:{os.getpid()}"
            replacement_owner = f"replacement:{os.getpid()}"
            replacement_token = "replacement-token"
            conn = connect(config.state.db)
            try:
                lock = acquire_runner_lock(conn, owner=owner)
                job = enqueue_job(conn, task="replacement", branch="feature/a")
                _stage_in_progress(conn, job.id, lock.token)

                def replace_during_probe(_config) -> bool:  # type: ignore[no-untyped-def]
                    control = connect(config.state.db)
                    try:
                        control.execute(
                            "UPDATE locks SET owner=?, token=? WHERE name='runner'",
                            (replacement_owner, replacement_token),
                        )
                        control.execute(
                            "UPDATE deploy_queue SET claim_token=?, note=? WHERE id=?",
                            (replacement_token, "owned by replacement", job.id),
                        )
                        control.commit()
                    finally:
                        control.close()
                    return True

                with patch.object(
                    recovery_module,
                    "_remote_reachable",
                    side_effect=replace_during_probe,
                ):
                    outcome = force_unlock(config, conn, force=True)

                current_lock = get_lock(conn)
                stored = get_job(conn, job.id)
                self.assertFalse(outcome.cleared)
                self.assertEqual(outcome.exit_code, 0)
                self.assertIsNone(outcome.audit_event_id)
                self.assertIn("lock changed", outcome.reason)
                self.assertIsNotNone(current_lock)
                self.assertEqual(current_lock.owner, replacement_owner)
                self.assertEqual(current_lock.token, replacement_token)
                self.assertEqual(stored.status, "in_progress")
                self.assertEqual(stored.claim_token, replacement_token)
                self.assertEqual(stored.note, "owned by replacement")
                self.assertFalse(
                    any(event.phase == "unlock" for event in list_run_events(conn))
                )
            finally:
                conn.close()


class AuditPreflightEvidenceTests(unittest.TestCase):
    def _run_preflight_failure(
        self,
        root: Path,
        error: Exception,
    ) -> tuple[Path, Path, str, object, list[object], object]:
        repo, marker = make_demo_repo(root)
        config = load_config(repo=repo)
        remote_main = git(root / "remote.git", "rev-parse", "main")
        conn = connect(config.state.db)
        try:
            job = enqueue_job(conn, task="audit preflight", branch="feature/a")
            runner = GitRunner(config)
            with patch.object(
                runner,
                "_audit_ref_expectation",
                side_effect=error,
            ), patch.object(runner, "_push_with_marker") as push_with_marker:
                runner.process_batch(conn, [job], deploy=True)
            stored = get_job(conn, job.id)
            events = list_run_events(conn, limit=200)
        finally:
            conn.close()
        return repo, marker, remote_main, stored, events, push_with_marker

    def _assert_no_push_evidence(
        self,
        root: Path,
        repo: Path,
        remote_main: str,
        stored,
        push_with_marker,
    ) -> None:  # type: ignore[no-untyped-def]
        self.assertEqual(stored.pending_deploy_sha, "")
        self.assertEqual(stored.push_status, "not_run")
        self.assertEqual(git(root / "remote.git", "rev-parse", "main"), remote_main)
        self.assertEqual(_refs(repo, "refs/mergetrain/pending/"), "")
        self.assertEqual(_refs(root / "remote.git", "refs/mergetrain/deploys/"), "")
        push_with_marker.assert_not_called()

    def test_cancellation_during_audit_preflight_cancels_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker, remote_main, stored, events, push = self._run_preflight_failure(
                root,
                CancellationRequested("cancel during audit lookup"),
            )

            self.assertEqual(stored.status, "canceled")
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            self._assert_no_push_evidence(root, repo, remote_main, stored, push)
            self.assertFalse(
                any(event.message == "Deploy audit preflight failed" for event in events)
            )
            self.assertTrue(
                any(event.message.endswith("canceled") for event in events)
            )

    def test_audit_preflight_failure_blocks_without_marker_or_remote_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker, remote_main, stored, events, push = self._run_preflight_failure(
                root,
                MergetrainError("audit lookup unavailable"),
            )

            self.assertEqual(stored.status, "blocked")
            self.assertIn("audit lookup unavailable", stored.note)
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            self._assert_no_push_evidence(root, repo, remote_main, stored, push)
            failure = next(
                event
                for event in events
                if event.message == "Deploy audit preflight failed"
            )
            self.assertEqual((failure.phase, failure.state), ("pushing", "error"))
            self.assertEqual(failure.detail, "MergetrainError")


class ValidatedReassemblyEvidenceTests(unittest.TestCase):
    def _validate(self, repo: Path, config, conn):  # type: ignore[no-untyped-def]
        job = enqueue_job(
            conn,
            task="validated reassembly",
            branch="feature/a",
            base_sha=git(repo, "rev-parse", "origin/main"),
            head_sha=git(repo, "rev-parse", "feature/a"),
        )
        return GitRunner(config).process_batch(conn, [job], deploy=False)[0]

    def test_merge_conflict_blocks_the_entire_validated_train_before_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                validated = self._validate(repo, config, conn)
                (repo / "a.txt").write_text("main conflict\n", encoding="utf-8")
                git(repo, "add", "a.txt")
                git(repo, "commit", "-m", "conflict with validated feature")
                git(repo, "push", "origin", "main")
                remote_main = git(root / "remote.git", "rev-parse", "main")

                result = GitRunner(config).process_batch(
                    conn,
                    [validated],
                    deploy=True,
                )[0]
            finally:
                conn.close()

            self.assertEqual(result.status, "blocked")
            self.assertIn("validated train could not be reassembled", result.note)
            self.assertEqual(result.pending_deploy_sha, "")
            self.assertEqual(result.push_status, "not_run")
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            self.assertEqual(git(root / "remote.git", "rev-parse", "main"), remote_main)
            self.assertEqual(
                git(root / "remote.git", "show", "main:a.txt"),
                "main conflict",
            )
            self.assertEqual(_refs(repo, "refs/mergetrain/pending/"), "")
            self.assertEqual(_refs(root / "remote.git", "refs/mergetrain/deploys/"), "")

    def test_dirty_reassembly_blocks_the_validated_train_before_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            dirtied = False
            try:
                validated = self._validate(repo, config, conn)
                remote_main = git(root / "remote.git", "rev-parse", "main")
                real_run_command = git_runner_module.run_command

                def dirty_after_merge(command, **kwargs):  # type: ignore[no-untyped-def]
                    nonlocal dirtied
                    completed = real_run_command(command, **kwargs)
                    if (
                        not dirtied
                        and list(command[:3]) == ["git", "merge", "--no-edit"]
                        and completed.returncode == 0
                    ):
                        Path(kwargs["cwd"], "unexpected-gate-input.txt").write_text(
                            "dirty\n",
                            encoding="utf-8",
                        )
                        dirtied = True
                    return completed

                with patch.object(
                    git_runner_module,
                    "run_command",
                    side_effect=dirty_after_merge,
                ):
                    result = GitRunner(config).process_batch(
                        conn,
                        [validated],
                        deploy=True,
                    )[0]
            finally:
                conn.close()

            self.assertTrue(dirtied)
            self.assertEqual(result.status, "blocked")
            self.assertEqual(
                result.note,
                "validated train produced a dirty integration worktree after reassembly",
            )
            self.assertEqual(result.pending_deploy_sha, "")
            self.assertEqual(result.push_status, "not_run")
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            self.assertEqual(git(root / "remote.git", "rev-parse", "main"), remote_main)
            self.assertEqual(_refs(repo, "refs/mergetrain/pending/"), "")
            self.assertEqual(_refs(root / "remote.git", "refs/mergetrain/deploys/"), "")


class CancelTrainRaceEvidenceTests(unittest.TestCase):
    def test_active_train_row_count_change_rolls_back_the_whole_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "queue.sqlite")
            try:
                first = enqueue_job(conn, task="first", branch="first")
                second = enqueue_job(conn, task="second", branch="second")
                claimed = claim_all_queued(conn, owner=f"runner:{os.getpid()}")
                token = claimed[0].claim_token

                # SQLite's IMMEDIATE transaction excludes a concurrent writer,
                # so use RAISE(IGNORE) to deterministically model one counted
                # train member becoming ineligible for the train-wide UPDATE.
                # The row-count guard must reject and roll back the first row's
                # already-applied cancellation as well.
                conn.execute(
                    f"""
                    CREATE TRIGGER skip_changed_train_member_during_cancel
                    BEFORE UPDATE OF cancel_requested_at ON deploy_queue
                    WHEN OLD.id = {second.id} AND NEW.cancel_requested_at != ''
                    BEGIN
                      SELECT RAISE(IGNORE);
                    END
                    """
                )
                conn.commit()

                with self.assertRaisesRegex(
                    QueueError,
                    f"active train changed while canceling job {first.id}",
                ):
                    cancel_job(conn, first.id, note="stop exact train")

                first_after = get_job(conn, first.id)
                second_after = get_job(conn, second.id)
                for item in (first_after, second_after):
                    self.assertEqual(item.status, "in_progress")
                    self.assertEqual(item.claim_token, token)
                    self.assertEqual(item.cancel_requested_at, "")
                self.assertNotEqual(first_after.note, "stop exact train")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
