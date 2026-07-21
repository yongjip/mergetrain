from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mergetrain.daemon import daemon_loop, daemon_tick
from mergetrain.store import claim_all_queued, connect, enqueue_job, get_job, list_jobs


class DaemonTests(unittest.TestCase):
    def test_daemon_once_processes_only_auto_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            manual = enqueue_job(conn, task="manual", branch="manual")
            auto = enqueue_job(conn, task="auto", branch="auto", auto_deploy=True)
            conn.close()

            seen = []

            def process_batch(conn, jobs):  # type: ignore[no-untyped-def]
                seen.extend(job.id for job in jobs)

            daemon_loop(
                db_path=str(db),
                process_batch=process_batch,
                owner="daemon:999999",
                once=True,
                say=lambda _: None,
                install_signal_handlers=False,
            )
            self.assertEqual(seen, [auto.id])
            conn = connect(db)
            try:
                jobs = {job.id: job for job in list_jobs(conn)}
            finally:
                conn.close()
            self.assertEqual(jobs[manual.id].status, "queued")

    def test_tick_pauses_when_claim_parks_orphans_as_needs_reconcile(self) -> None:
        # TOCTOU guard: the pre-claim reconcile check passes, but acquiring
        # the lock requeues a dead owner's orphans and parks a marker-bearing
        # job as needs_reconcile. The same claim must then refuse to deploy.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            orphan = enqueue_job(conn, task="orphan", branch="orphan")
            auto = enqueue_job(conn, task="auto", branch="auto", auto_deploy=True)
            # A dead runner (impossible PID) left an expired lock, an
            # in_progress job carrying its claim token, and a durable
            # pending-deploy marker from a push that may have landed.
            conn.execute(
                """
                INSERT INTO locks (name, owner, acquired_at, heartbeat_at, expires_at, token)
                VALUES ('runner', 'daemon:999999', '2000-01-01T00:00:00Z',
                        '2000-01-01T00:00:00Z', '2000-01-01T00:00:01Z', 'dead-token')
                """
            )
            conn.execute(
                "UPDATE deploy_queue SET status='in_progress', claim_token='dead-token', "
                "pending_deploy_sha='deadbeef' WHERE id = ?",
                (orphan.id,),
            )
            conn.commit()
            conn.close()

            # Call the claim directly: the daemon's pre-claim check has
            # already passed in the TOCTOU scenario, so the guard must live
            # inside the claim transaction itself.
            conn = connect(db)
            try:
                jobs = claim_all_queued(
                    conn, owner="daemon:1", auto_only=True
                )
                self.assertEqual(jobs, [])
                self.assertEqual(get_job(conn, orphan.id).status, "needs_reconcile")
                self.assertEqual(get_job(conn, auto.id).status, "queued")
                lock = conn.execute("SELECT * FROM locks").fetchall()
            finally:
                conn.close()
            # The claim released its own lock instead of deploying past the
            # freshly parked reconcile.
            self.assertEqual(lock, [])

            # And the daemon tick reports the pause rather than "idle".
            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: None,
                owner="daemon:1",
                say=lambda _: None,
            )
            self.assertEqual(outcome, "reconcile_paused")


if __name__ == "__main__":
    unittest.main()
