"""Job is read from and written to deploy_queue by field name."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from mergetrain.models import Job
from mergetrain.store import connect


class JobColumnTests(unittest.TestCase):
    def _columns(self) -> set[str]:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "queue.sqlite")
            try:
                rows = conn.execute("PRAGMA table_info(deploy_queue)").fetchall()
            finally:
                conn.close()
        return {str(row["name"]) for row in rows}

    def test_every_job_field_is_a_queue_column(self) -> None:
        missing = {field.name for field in fields(Job)} - self._columns()
        self.assertEqual(missing, set())

    def test_empty_optional_columns_read_as_their_defaults(self) -> None:
        row: dict[str, object] = {field.name: None for field in fields(Job)}
        row.update(id=3, task="t", branch="b", status="blocked")
        job = Job.from_row(row)
        self.assertEqual(job.push_status, "not_run")
        self.assertEqual(job.verify_status, "not_run")
        self.assertEqual(job.train_size, 0)
        self.assertIs(job.auto_deploy, False)
        self.assertEqual(job.note, "")

    def test_a_missing_status_never_reads_as_queued(self) -> None:
        row: dict[str, object] = {field.name: None for field in fields(Job)}
        row.update(id=3, task="t", branch="b", status="")
        self.assertEqual(Job.from_row(row).status, "")

    def test_a_real_row_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "queue.sqlite")
            try:
                conn.execute(
                    "INSERT INTO deploy_queue (task, branch, status, requested_at, "
                    "auto_deploy, train_size) VALUES ('t', 'b', 'queued', 'now', 1, 2)"
                )
                row = conn.execute("SELECT * FROM deploy_queue").fetchone()
            finally:
                conn.close()
        job = Job.from_row(row)
        self.assertIsInstance(row, sqlite3.Row)
        self.assertEqual((job.task, job.status, job.train_size), ("t", "queued", 2))
        self.assertIs(job.auto_deploy, True)

    def test_to_dict_hides_internal_bookkeeping(self) -> None:
        job = Job(
            id=1,
            task="t",
            branch="b",
            claim_token="secret",
            deployment_id="d",
            approval_destination_sha="a",
            note="n",
        )
        data = job.to_dict()
        for name in ("claim_token", "deployment_id", "approval_destination_sha"):
            self.assertNotIn(name, data)
        self.assertEqual((data["note"], data["note_truncated"]), ("n", False))


if __name__ == "__main__":
    unittest.main()
