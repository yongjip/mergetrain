from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mergetrain.errors import CancellationRequested, LockHeld, LostLease, QueueError
from mergetrain.store import (
    acquire_runner_lock,
    cancel_job,
    claim_all_queued,
    claim_deploy_batch,
    connect,
    enqueue_job,
    get_job,
    get_lock,
    list_run_events,
    mark_job,
    refresh_runner_lock,
    release_runner_lock,
    terminal_branch_candidates,
    validated_train_summaries,
)


class StoreTests(unittest.TestCase):
    def make_conn(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        conn = connect(Path(td.name) / "queue.sqlite")
        self.addCleanup(conn.close)
        return conn

    def test_legacy_database_migrates_validation_train_columns(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = Path(td.name) / "legacy.sqlite"
        legacy = sqlite3.connect(db)
        legacy.execute(
            """
            CREATE TABLE deploy_queue (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task TEXT NOT NULL,
              branch TEXT NOT NULL,
              worktree_path TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'queued',
              base_sha TEXT NOT NULL DEFAULT '',
              head_sha TEXT NOT NULL DEFAULT '',
              deploy_sha TEXT NOT NULL DEFAULT '',
              requested_at TEXT NOT NULL,
              started_at TEXT NOT NULL DEFAULT '',
              finished_at TEXT NOT NULL DEFAULT '',
              log_path TEXT NOT NULL DEFAULT '',
              note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        legacy.execute(
            """
            INSERT INTO deploy_queue (task, branch, status, requested_at, note)
            VALUES (
              'old', 'feature/old', 'deployed', 'now',
              'post-push verify warning: legacy failure'
            )
            """
        )
        legacy.commit()
        legacy.close()

        conn = connect(db)
        try:
            migrated = get_job(conn, 1)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(deploy_queue)")}
        finally:
            conn.close()
        self.assertEqual(migrated.train_id, "")
        self.assertEqual(migrated.train_size, 0)
        self.assertEqual(migrated.push_status, "succeeded")
        self.assertEqual(migrated.verify_status, "failed")
        self.assertIn("validated_head_sha", columns)
        self.assertIn("claim_token", columns)
        self.assertIn("push_status", columns)
        self.assertIn("verify_status", columns)
        migrated_db = sqlite3.connect(db)
        try:
            self.assertEqual(migrated_db.execute("PRAGMA user_version").fetchone()[0], 4)
            event_columns = {
                row[1] for row in migrated_db.execute("PRAGMA table_info(run_events)")
            }
            self.assertIn("phase", event_columns)
            self.assertIn("heartbeat_at", {
                row[1] for row in migrated_db.execute("PRAGMA table_info(locks)")
            })
        finally:
            migrated_db.close()

    def test_newer_schema_version_is_rejected(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = Path(td.name) / "future.sqlite"
        future = sqlite3.connect(db)
        future.execute("PRAGMA user_version = 999")
        future.close()
        with self.assertRaisesRegex(QueueError, "newer than supported"):
            connect(db)

    def test_duplicate_active_branch_is_blocked_until_terminal(self) -> None:
        conn = self.make_conn()
        first = enqueue_job(conn, task="a", branch="feature/a")
        self.assertEqual(first.id, 1)
        with self.assertRaises(QueueError):
            enqueue_job(conn, task="again", branch="feature/a")
        mark_job(conn, first.id, status="validated")
        with self.assertRaises(QueueError):
            enqueue_job(conn, task="again", branch="feature/a")
        mark_job(conn, first.id, status="deployed")
        second = enqueue_job(conn, task="again", branch="feature/a")
        self.assertEqual(second.id, 2)

    def test_claim_records_structured_event_without_exposing_token(self) -> None:
        conn = self.make_conn()
        enqueue_job(conn, task="a", branch="feature/a")
        claimed = claim_all_queued(conn, owner=f"owner:{os.getpid()}")
        events = list_run_events(conn)
        self.assertEqual(events[-1].phase, "claiming")
        self.assertEqual(events[-1].state, "active")
        self.assertEqual(events[-1].claim_token, claimed[0].claim_token)
        self.assertNotIn("claim_token", events[-1].to_dict())

    def test_validated_train_is_claimed_without_new_queued_jobs(self) -> None:
        conn = self.make_conn()
        first = enqueue_job(conn, task="a", branch="feature/a")
        second = enqueue_job(conn, task="b", branch="feature/b")
        for job, head in [(first, "a" * 40), (second, "b" * 40)]:
            mark_job(
                conn,
                job.id,
                status="validated",
                train_id="train-1",
                train_size=2,
                validated_at="2026-07-16T00:00:00Z",
                validation_base_sha="c" * 40,
                validation_sha="d" * 40,
                validated_head_sha=head,
            )
        queued = enqueue_job(conn, task="later", branch="feature/later")

        summaries = validated_train_summaries(conn)
        self.assertEqual(len(summaries), 1)
        self.assertTrue(summaries[0]["deploy_eligible"])
        claimed = claim_deploy_batch(conn, owner=f"owner:{os.getpid()}")
        self.assertEqual([job.id for job in claimed], [first.id, second.id])
        self.assertEqual({job.status for job in claimed}, {"in_progress"})
        self.assertEqual(len({job.claim_token for job in claimed}), 1)
        self.assertEqual(get_job(conn, queued.id).status, "queued")
        release_runner_lock(
            conn, owner=f"owner:{os.getpid()}", token=claimed[0].claim_token
        )

    def test_multiple_validated_trains_require_explicit_identity(self) -> None:
        conn = self.make_conn()
        for index, train_id in enumerate(["train-1", "train-2"], start=1):
            job = enqueue_job(conn, task=train_id, branch=f"feature/{index}")
            mark_job(
                conn,
                job.id,
                status="validated",
                train_id=train_id,
                train_size=1,
                validated_at="2026-07-16T00:00:00Z",
                validation_base_sha="a" * 40,
                validation_sha=str(index) * 40,
                validated_head_sha=str(index + 2) * 40,
            )
        with self.assertRaisesRegex(QueueError, "multiple validated trains"):
            claim_deploy_batch(conn, owner=f"owner:{os.getpid()}")
        self.assertIsNone(get_lock(conn))
        claimed = claim_deploy_batch(
            conn,
            owner=f"owner:{os.getpid()}",
            train_id="train-2",
        )
        self.assertEqual([job.train_id for job in claimed], ["train-2"])
        release_runner_lock(
            conn, owner=f"owner:{os.getpid()}", token=claimed[0].claim_token
        )

    def test_validated_job_is_not_a_gc_branch_candidate(self) -> None:
        conn = self.make_conn()
        previous = enqueue_job(conn, task="old", branch="feature/validated")
        deployed = enqueue_job(conn, task="b", branch="feature/deployed")
        mark_job(conn, previous.id, status="deployed")
        validated = enqueue_job(conn, task="a", branch="feature/validated")
        mark_job(conn, validated.id, status="validated")
        mark_job(conn, deployed.id, status="deployed")
        candidates = terminal_branch_candidates(conn)
        self.assertEqual([candidate["branch"] for candidate in candidates], ["feature/deployed"])

    def test_canceling_validated_job_cancels_whole_train(self) -> None:
        conn = self.make_conn()
        first = enqueue_job(conn, task="a", branch="feature/a")
        second = enqueue_job(conn, task="b", branch="feature/b")
        for job in [first, second]:
            mark_job(conn, job.id, status="validated", train_id="train-1", train_size=2)
        cancel_job(conn, first.id)
        self.assertEqual(get_job(conn, first.id).status, "canceled")
        self.assertEqual(get_job(conn, second.id).status, "canceled")

    def test_runner_lock_blocks_concurrent_owner(self) -> None:
        conn = self.make_conn()
        lock = acquire_runner_lock(conn, owner=f"user:{os.getpid()}")
        with self.assertRaises(LockHeld):
            acquire_runner_lock(conn, owner="other:999999")
        self.assertIsNotNone(get_lock(conn))
        self.assertNotIn("token", get_lock(conn).to_dict())
        release_runner_lock(conn, owner=f"user:{os.getpid()}", token=lock.token)
        self.assertIsNone(get_lock(conn))

    def test_expired_lease_is_reclaimable_even_if_pid_alive(self) -> None:
        conn = self.make_conn()
        # The current PID is alive, but the lease is already expired and nothing is
        # in flight. A healthy runner would have refreshed its lease; an abandoned
        # lock (or a recycled PID that merely looks alive) must not block forever.
        acquire_runner_lock(conn, owner=f"user:{os.getpid()}", ttl_minutes=-1)
        lock = acquire_runner_lock(conn, owner=f"newrunner:{os.getpid()}", ttl_minutes=30)
        self.assertEqual(lock.owner, f"newrunner:{os.getpid()}")

    def test_expired_lease_with_in_progress_jobs_is_not_stolen(self) -> None:
        conn = self.make_conn()
        # Acquire while nothing is in flight (so no orphan requeue), then leave a job
        # mid-flight. An expired lease with in_progress work is held back for operator
        # investigation rather than auto-reclaimed.
        acquire_runner_lock(conn, owner=f"user:{os.getpid()}", ttl_minutes=-1)
        job = enqueue_job(conn, task="a", branch="a")
        mark_job(conn, job.id, status="in_progress")
        with self.assertRaises(LockHeld):
            acquire_runner_lock(conn, owner=f"newrunner:{os.getpid()}")

    def test_batch_claim_fences_stale_owner_and_requests_cooperative_cancel(self) -> None:
        conn = self.make_conn()
        first = enqueue_job(conn, task="a", branch="a")
        second = enqueue_job(conn, task="b", branch="b")
        owner = f"runner:{os.getpid()}"
        claimed = claim_all_queued(conn, owner=owner, ttl_minutes=-1)
        token = claimed[0].claim_token

        with self.assertRaises(LockHeld):
            acquire_runner_lock(conn, owner=f"other:{os.getpid()}")

        requested = cancel_job(conn, first.id)
        self.assertEqual(requested.status, "in_progress")
        self.assertTrue(requested.cancel_requested_at)
        self.assertTrue(get_job(conn, second.id).cancel_requested_at)
        with self.assertRaises(CancellationRequested):
            refresh_runner_lock(conn, owner=owner, token=token)
        with self.assertRaises(CancellationRequested):
            mark_job(
                conn,
                first.id,
                status="validated",
                expected_claim_token=token,
            )

    def test_replaced_lease_cannot_be_refreshed_or_release_new_owner(self) -> None:
        conn = self.make_conn()
        job = enqueue_job(conn, task="a", branch="a")
        owner = f"runner:{os.getpid()}"
        claimed = claim_all_queued(conn, owner=owner)
        stale_token = claimed[0].claim_token
        conn.execute(
            "UPDATE locks SET owner = ?, token = ? WHERE name = 'runner'",
            (f"replacement:{os.getpid()}", "replacement-token"),
        )
        conn.commit()
        with self.assertRaises(LostLease):
            refresh_runner_lock(conn, owner=owner, token=stale_token)
        self.assertFalse(
            release_runner_lock(conn, owner=owner, token=stale_token)
        )
        self.assertEqual(get_lock(conn).token, "replacement-token")
        self.assertEqual(get_job(conn, job.id).status, "in_progress")

    def test_auto_only_batch_claim_skips_manual_jobs(self) -> None:
        conn = self.make_conn()
        manual = enqueue_job(conn, task="manual", branch="manual")
        auto = enqueue_job(conn, task="auto", branch="auto", auto_deploy=True)
        jobs = claim_all_queued(conn, owner="owner:999999", auto_only=True)
        self.assertEqual([job.id for job in jobs], [auto.id])
        self.assertEqual(jobs[0].status, "in_progress")
        release_runner_lock(conn, owner="owner:999999", token=jobs[0].claim_token)
        self.assertEqual(manual.status, "queued")

    def test_terminal_job_cannot_be_canceled(self) -> None:
        conn = self.make_conn()
        job = enqueue_job(conn, task="a", branch="a")
        mark_job(conn, job.id, status="deployed")
        with self.assertRaises(QueueError):
            cancel_job(conn, job.id)


if __name__ == "__main__":
    unittest.main()
