from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from mergetrain.cli import _results_payload, main, normalize_global_options
from mergetrain.models import Job
from mergetrain.store import connect, enqueue_job, mark_job


class CliTests(unittest.TestCase):
    def test_results_payload_reports_failure_and_partial_outcomes(self) -> None:
        failed = _results_payload([Job(id=1, task="a", branch="a", status="failed")])
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["result"], "failed")
        partial = _results_payload(
            [
                Job(id=1, task="a", branch="a", status="validated"),
                Job(id=2, task="b", branch="b", status="blocked"),
            ]
        )
        self.assertFalse(partial["ok"])
        self.assertEqual(partial["result"], "partial")
        self.assertEqual(partial["counts"], {"blocked": 1, "validated": 1})
        self.assertNotIn("claim_token", partial["jobs"][0])

    def test_json_mode_emits_structured_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                "git:\n  push_refs: []\n", encoding="utf-8"
            )
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "doctor", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "config_error")

    def test_global_option_after_subcommand_is_normalized(self) -> None:
        normalized = normalize_global_options(["doctor", "--json", "--repo", "/tmp/example"])
        self.assertEqual(normalized[:2], ["--repo", "/tmp/example"])
        self.assertIn("doctor", normalized)

    def test_agent_contract_json(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["agent-contract", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertIn("rules", payload)
        self.assertEqual(payload["boundary"]["daemon_processes_only"], "jobs enqueued with --auto")
        self.assertIn("exact validated train", payload["boundary"]["validated_train_deploy"])

    def test_init_write_creates_generic_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "init", "--project", "demo", "--write"])
            self.assertEqual(code, 0)
            self.assertTrue((repo / ".mergetrain.yaml").exists())
            self.assertTrue((repo / "AGENTS.mergetrain.md").exists())

    def test_status_json_exposes_validated_train_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            conn = connect(db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                mark_job(
                    conn,
                    job.id,
                    status="validated",
                    train_id="train-1",
                    train_size=1,
                    validated_at="2026-07-16T00:00:00Z",
                    validation_base_sha="a" * 40,
                    validation_sha="b" * 40,
                    validated_head_sha="c" * 40,
                )
            finally:
                conn.close()
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "--db", str(db), "status", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["validated_trains"][0]["train_id"], "train-1")
            self.assertTrue(payload["validated_trains"][0]["deploy_eligible"])


if __name__ == "__main__":
    unittest.main()
