from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mergetrain.cli import _job_result_line, _results_payload, main, normalize_global_options
from mergetrain.models import Job
from mergetrain.reuse import ReuseDecision
from mergetrain.store import (
    claim_next_job,
    connect,
    enqueue_job,
    mark_job,
    record_run_event,
    release_runner_lock,
)


class CliTests(unittest.TestCase):
    def test_results_payload_exposes_exact_reused_validation_sha(self) -> None:
        sha = "a" * 40
        job = Job(
            id=1,
            task="a",
            branch="feature/a",
            status="deployed",
            push_status="succeeded",
            verify_status="succeeded",
            reused_validation_sha=sha,
        )
        payload = _results_payload([job])
        self.assertEqual(payload["reused_validation_shas"], [sha])
        self.assertIn(f"reused={sha}", _job_result_line(payload["jobs"][0]))

    def test_results_payload_reports_post_push_verify_warning(self) -> None:
        job = Job(
            id=1,
            task="a",
            branch="feature/a",
            status="deployed",
            push_status="succeeded",
            verify_status="failed",
        )
        payload = _results_payload([job])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["result"], "warning")
        self.assertEqual(payload["push_counts"], {"succeeded": 1})
        self.assertEqual(payload["verify_counts"], {"failed": 1})
        self.assertEqual(payload["jobs"][0]["status"], "deployed")
        self.assertEqual(
            _job_result_line(payload["jobs"][0]),
            "#1 deployed (push=succeeded, verify=failed): feature/a",
        )

    def test_legacy_version_output_remains_compatible(self) -> None:
        out = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(out):
            main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(out.getvalue(), "mergetrain 0.3.0\n")

    def test_version_json_exposes_runtime_provenance(self) -> None:
        runtime = {
            "distribution_version": "0.1.0",
            "package_path": "/tmp/site-packages/mergetrain",
            "install_mode": "wheel",
            "source_path": None,
            "source_commit": "a" * 40,
            "source_dirty": None,
        }
        out = io.StringIO()
        with patch("mergetrain.cli.runtime_provenance", return_value=runtime), redirect_stdout(out):
            code = main(["version", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["version"], "0.3.0")
        self.assertEqual(payload["runtime"], runtime)

    def test_doctor_json_includes_runtime_provenance(self) -> None:
        runtime = {
            "distribution_version": "0.1.0",
            "package_path": "/tmp/checkout/src/mergetrain",
            "install_mode": "editable",
            "source_path": "/tmp/checkout",
            "source_commit": "b" * 40,
            "source_dirty": False,
        }
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(["--repo", str(repo), "init", "--project", "demo", "--write"]),
                    0,
                )
            subprocess.run(
                ["git", "init", "--initial-branch=main", str(repo)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            out = io.StringIO()
            with patch("mergetrain.cli.runtime_provenance", return_value=runtime), redirect_stdout(out):
                code = main(["--repo", str(repo), "doctor", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["runtime"], runtime)

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
        self.assertIn("disabled by default", payload["boundary"]["validated_gate_reuse"])
        self.assertIn("read-only", payload["boundary"]["progress_observation"])

    def test_configured_agent_contract_uses_integration_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                "terminology:\n  git_operation: integrate\n",
                encoding="utf-8",
            )
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "agent-contract", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["human_vocabulary"]["completed"], "integrated")
            self.assertEqual(payload["human_vocabulary"]["cli_flag"], "--integrate")
            self.assertEqual(payload["human_vocabulary"]["machine_status"], "deployed")
            self.assertEqual(payload["boundary"]["deploy_requires"], "run-next --deploy or run-batch --deploy")

    def test_integration_human_status_preserves_json_machine_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            (repo / ".mergetrain.yaml").write_text(
                "terminology:\n  git_operation: integrate\n",
                encoding="utf-8",
            )
            conn = connect(db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                mark_job(conn, job.id, status="deployed", push_status="succeeded")
            finally:
                conn.close()

            human = io.StringIO()
            with redirect_stdout(human):
                self.assertEqual(
                    main(["--repo", str(repo), "--db", str(db), "status"]),
                    0,
                )
            self.assertIn("integrated", human.getvalue())
            self.assertNotIn(" deployed", human.getvalue())

            machine = io.StringIO()
            with redirect_stdout(machine):
                self.assertEqual(
                    main(["--repo", str(repo), "--db", str(db), "status", "--json"]),
                    0,
                )
            self.assertEqual(json.loads(machine.getvalue())["jobs"][0]["status"], "deployed")

    def test_integrate_preview_lists_exact_atomic_push_refspecs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            (repo / ".mergetrain.yaml").write_text(
                """git:
  remote: upstream
  integration_branch: main
  push_refs:
    - main
    - release
terminology:
  git_operation: integrate
""",
                encoding="utf-8",
            )
            conn = connect(db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                mark_job(
                    conn,
                    job.id,
                    status="validated",
                    train_id="train-1",
                    train_size=1,
                    validated_at="2026-07-19T00:00:00Z",
                    validation_base_sha="a" * 40,
                    validation_sha="b" * 40,
                    validated_head_sha="c" * 40,
                )
            finally:
                conn.close()
            decision = ReuseDecision(
                authorized=False,
                eligible=False,
                action="rerun",
                validation_sha="b" * 40,
                reasons=("reuse not authorized",),
            )
            out = io.StringIO()
            with patch(
                "mergetrain.cli.GitRunner.preview_validated_reuse",
                return_value=decision,
            ), redirect_stdout(out):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "run-batch",
                        "--integrate",
                        "--preview",
                        "--json",
                    ]
                )
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["mode"], "deploy")
            self.assertEqual(payload["terminology"]["completed"], "integrated")
            self.assertEqual(payload["push_plan"]["remote"], "upstream")
            self.assertEqual(
                [item["spec"] for item in payload["push_plan"]["refs"]],
                ["HEAD:main", "HEAD:release"],
            )

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

    def test_inspect_json_exposes_gate_elapsed_and_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            conn = connect(db)
            owner = f"owner:{os.getpid()}"
            try:
                queued = enqueue_job(conn, task="a", branch="feature/a")
                claimed = claim_next_job(conn, owner=owner)
                assert claimed is not None
                record_run_event(
                    conn,
                    claim_token=claimed.claim_token,
                    job_id=queued.id,
                    phase="gating",
                    state="active",
                    message="Running gate 2/3: unit-tests",
                    detail="pytest -q",
                )
                out = io.StringIO()
                with redirect_stdout(out):
                    code = main(
                        [
                            "--repo",
                            str(repo),
                            "--db",
                            str(db),
                            "inspect",
                            str(queued.id),
                            "--json",
                        ]
                    )
                payload = json.loads(out.getvalue())
                self.assertEqual(code, 0)
                self.assertEqual(payload["progress"]["phase"], "gating")
                self.assertEqual(payload["progress"]["gate"]["index"], 2)
                self.assertEqual(payload["progress"]["gate"]["name"], "unit-tests")
                self.assertIsNotNone(payload["progress"]["elapsed_seconds"])
                self.assertTrue(payload["progress"]["heartbeat_at"])
                self.assertEqual(payload["progress"]["lease_liveness"], "alive")
                self.assertNotIn("claim_token", payload["events"][-1])
            finally:
                current = claimed if "claimed" in locals() else None
                if current is not None:
                    mark_job(
                        conn,
                        queued.id,
                        status="canceled",
                        note="test cleanup",
                        expected_claim_token=current.claim_token,
                    )
                    release_runner_lock(conn, owner=owner, token=current.claim_token)
                conn.close()

    def test_inspect_train_has_structured_failure_categories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            conn = connect(db)
            try:
                first = enqueue_job(conn, task="a", branch="feature/a")
                second = enqueue_job(conn, task="b", branch="feature/b")
                for job in (first, second):
                    mark_job(
                        conn,
                        job.id,
                        status="validated",
                        train_id="train-1",
                        train_size=2,
                    )
                mark_job(
                    conn,
                    second.id,
                    status="failed",
                    note="gate command failed",
                )
            finally:
                conn.close()
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "inspect",
                        str(first.id),
                        "--json",
                    ]
                )
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["train"]["outcome"]["severity"], "failure")
            self.assertEqual(
                payload["train"]["outcome"]["failure_categories"],
                ["gate_failed"],
            )

    def test_events_jsonl_resume_and_terminal_frame(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            conn = connect(db)
            owner = f"owner:{os.getpid()}"
            try:
                queued = enqueue_job(conn, task="a", branch="feature/a")
                claimed = claim_next_job(conn, owner=owner)
                assert claimed is not None
                event = record_run_event(
                    conn,
                    claim_token=claimed.claim_token,
                    job_id=queued.id,
                    phase="gating",
                    state="active",
                    message="Running gate 1/1: tests",
                )
                mark_job(
                    conn,
                    queued.id,
                    status="validated",
                    note="ok",
                    expected_claim_token=claimed.claim_token,
                )
                release_runner_lock(conn, owner=owner, token=claimed.claim_token)
            finally:
                conn.close()
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "events",
                        "--job",
                        str(queued.id),
                        "--after",
                        str(event.id - 1),
                        "--follow",
                        "--jsonl",
                    ]
                )
            records = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual(code, 0)
            self.assertEqual(records[0]["id"], event.id)
            self.assertEqual(records[0]["type"], "event")
            self.assertNotIn("claim_token", records[0])
            self.assertEqual(records[-1]["type"], "stream_end")
            self.assertEqual(records[-1]["reason"], "success")

    def test_events_follow_reports_lost_lease_and_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            conn = connect(db)
            try:
                lost = enqueue_job(conn, task="lost", branch="feature/lost")
                mark_job(conn, lost.id, status="in_progress", note="orphan")
                queued = enqueue_job(conn, task="queued", branch="feature/queued")
            finally:
                conn.close()

            lost_out = io.StringIO()
            with redirect_stdout(lost_out):
                lost_code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "events",
                        "--job",
                        str(lost.id),
                        "--follow",
                        "--jsonl",
                    ]
                )
            self.assertEqual(lost_code, 1)
            self.assertEqual(
                json.loads(lost_out.getvalue().splitlines()[-1])["reason"],
                "lost_lease",
            )

            interrupted = io.StringIO()
            with patch("mergetrain.cli.time.sleep", side_effect=KeyboardInterrupt), redirect_stdout(interrupted):
                interrupted_code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "events",
                        "--job",
                        str(queued.id),
                        "--follow",
                        "--jsonl",
                    ]
                )
            self.assertEqual(interrupted_code, 130)
            self.assertEqual(
                json.loads(interrupted.getvalue().splitlines()[-1])["reason"],
                "interrupted",
            )

    def test_logs_tail_reads_only_configured_log_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            logs = repo / ".mergetrain" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "job-1.log"
            log_path.write_text("one\ntwo\nthree\n", encoding="utf-8")
            conn = connect(db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                mark_job(
                    conn,
                    job.id,
                    status="failed",
                    log_path=str(log_path),
                    note="gate failed",
                )
            finally:
                conn.close()
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "logs",
                        str(job.id),
                        "--tail",
                        "2",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(out.getvalue(), "two\nthree\n")


if __name__ == "__main__":
    unittest.main()
