from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mergetrain.daemon import daemon_loop
from mergetrain.store import connect, enqueue_job, list_jobs


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


if __name__ == "__main__":
    unittest.main()
