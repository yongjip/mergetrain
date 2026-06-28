from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from mergetrain.errors import LockHeld, QueueError
from mergetrain.store import (
    acquire_runner_lock,
    cancel_job,
    claim_all_queued,
    connect,
    enqueue_job,
    get_lock,
    mark_job,
    release_runner_lock,
)


class StoreTests(unittest.TestCase):
    def make_conn(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return connect(Path(td.name) / "queue.sqlite")

    def test_duplicate_active_branch_is_blocked_until_terminal(self) -> None:
        conn = self.make_conn()
        first = enqueue_job(conn, task="a", branch="feature/a")
        self.assertEqual(first.id, 1)
        with self.assertRaises(QueueError):
            enqueue_job(conn, task="again", branch="feature/a")
        mark_job(conn, first.id, status="validated")
        second = enqueue_job(conn, task="again", branch="feature/a")
        self.assertEqual(second.id, 2)

    def test_runner_lock_blocks_concurrent_owner(self) -> None:
        conn = self.make_conn()
        acquire_runner_lock(conn, owner=f"user:{os.getpid()}")
        with self.assertRaises(LockHeld):
            acquire_runner_lock(conn, owner="other:999999")
        self.assertIsNotNone(get_lock(conn))
        release_runner_lock(conn, owner=f"user:{os.getpid()}")
        self.assertIsNone(get_lock(conn))

    def test_auto_only_batch_claim_skips_manual_jobs(self) -> None:
        conn = self.make_conn()
        manual = enqueue_job(conn, task="manual", branch="manual")
        auto = enqueue_job(conn, task="auto", branch="auto", auto_deploy=True)
        jobs = claim_all_queued(conn, owner="owner:999999", auto_only=True)
        self.assertEqual([job.id for job in jobs], [auto.id])
        release_runner_lock(conn, owner="owner:999999")
        self.assertEqual(manual.status, "queued")

    def test_terminal_job_cannot_be_canceled(self) -> None:
        conn = self.make_conn()
        job = enqueue_job(conn, task="a", branch="a")
        mark_job(conn, job.id, status="validated")
        with self.assertRaises(QueueError):
            cancel_job(conn, job.id)


if __name__ == "__main__":
    unittest.main()
