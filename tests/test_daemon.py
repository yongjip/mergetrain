from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from mergetrain.daemon import _grade_batch, daemon_loop, daemon_tick
from mergetrain.errors import QueueError
from mergetrain.models import Job
from mergetrain.store import (
    claim_all_queued,
    connect,
    enqueue_job,
    get_job,
    get_lock,
    list_jobs,
    mark_job,
)


class GradeBatchTests(unittest.TestCase):
    def _jobs(self, *statuses):
        return [Job(id=i, task="t", branch=f"b{i}", status=s) for i, s in enumerate(statuses)]

    def test_all_deployed_is_landed(self) -> None:
        self.assertEqual(_grade_batch(self._jobs("deployed", "deployed"), 2, lambda _: None), "landed:2")

    def test_nothing_deployed_is_no_landing_not_processed(self) -> None:
        # The bug: a sweep where every job blocked reported as a green deploy.
        out = _grade_batch(self._jobs("blocked", "failed"), 2, lambda _: None)
        self.assertEqual(out, "no_landing:2")

    def test_some_deployed_is_partial(self) -> None:
        out = _grade_batch(self._jobs("deployed", "blocked"), 2, lambda _: None)
        self.assertEqual(out, "partial:1/2")

    def test_uninspectable_result_falls_back_to_processed(self) -> None:
        self.assertEqual(_grade_batch(None, 3, lambda _: None), "processed:3")


class DaemonTests(unittest.TestCase):
    def test_batch_exception_cas_releases_claimed_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            job = enqueue_job(
                conn, task="auto", branch="auto", auto_deploy=True
            )
            conn.close()

            with self.assertRaisesRegex(RuntimeError, "boom"):
                daemon_tick(
                    db_path=str(db),
                    process_batch=lambda _conn, _jobs: (_ for _ in ()).throw(
                        RuntimeError("boom")
                    ),
                    owner="daemon:999999",
                    say=lambda _: None,
                )

            conn = connect(db)
            try:
                self.assertEqual(get_job(conn, job.id).status, "queued")
                self.assertIsNone(get_lock(conn))
            finally:
                conn.close()

            def finish(conn, jobs):  # type: ignore[no-untyped-def]
                return [
                    mark_job(
                        conn,
                        item.id,
                        status="failed",
                        note="deterministic test finish",
                        expected_claim_token=item.claim_token,
                    )
                    for item in jobs
                ]

            self.assertEqual(
                daemon_tick(
                    db_path=str(db),
                    process_batch=finish,
                    owner="daemon:999999",
                    say=lambda _: None,
                ),
                "no_landing:1",
            )

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


class ReadOnlyTickTests(unittest.TestCase):
    def test_non_sovereign_tick_never_creates_or_migrates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            # Missing database: the hub path must refuse, not create.
            with self.assertRaises(QueueError):
                daemon_tick(
                    db_path=str(db),
                    process_batch=lambda conn, jobs: None,
                    owner="daemon:1",
                    say=lambda _: None,
                )
            self.assertFalse(db.exists())

            # Old schema stamp: the hub path must report, not migrate. And an
            # idle tick must not rewrite the repo's journal mode either.
            conn = connect(db)
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("PRAGMA user_version = 6")
            conn.commit()
            conn.close()
            with self.assertRaises(QueueError):
                daemon_tick(
                    db_path=str(db),
                    process_batch=lambda conn, jobs: None,
                    owner="daemon:1",
                    say=lambda _: None,
                )
            raw = sqlite3.connect(db)
            try:
                self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(
                    raw.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete"
                )
            finally:
                raw.close()

    def test_sovereign_tick_creates_its_own_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: None,
                owner="daemon:1",
                say=lambda _: None,
                sovereign=True,
            )
            self.assertEqual(outcome, "idle")
            self.assertTrue(db.is_file())

    def test_read_only_connect_survives_uri_special_characters(self) -> None:
        # Characters that are URI-special (so an unescaped sqlite URI would
        # truncate the filename or drop mode=ro) yet legal in a filename. '?'
        # exercises the query-string truncation but is illegal on Windows, so
        # include it only where the OS allows it; '#'/'%' cover the rest
        # everywhere.
        name = "we#dir%41" if os.name == "nt" else "we?rd#dir%41"
        with tempfile.TemporaryDirectory() as td:
            weird = Path(td) / name
            weird.mkdir()
            db = weird / "queue.sqlite"
            conn = connect(db)
            enqueue_job(conn, task="a", branch="a")
            conn.close()
            observer = connect(db, read_only=True)
            try:
                self.assertEqual(len(list_jobs(observer)), 1)
                with self.assertRaises(sqlite3.OperationalError):
                    observer.execute("UPDATE deploy_queue SET note = 'w'")
            finally:
                observer.close()


if __name__ == "__main__":
    unittest.main()
