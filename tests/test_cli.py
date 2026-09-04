from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mergetrain import __version__
from mergetrain.cli import (
    _job_result_line,
    _results_payload,
    _run_exit_code,
    main,
    normalize_global_options,
)
from mergetrain.commands.setup import render_agent_contract
from mergetrain.config import load_config, render_default_config
from mergetrain.contract import CONTRACT_VERSION
from mergetrain.deploy_plan import (
    deploy_destination_sha,
    deploy_execution_policy_sha,
)
from mergetrain.errors import CommandFailed
from mergetrain.models import Job
from mergetrain.reuse import ReuseDecision
from mergetrain.store import (
    claim_next_job,
    connect,
    enqueue_job,
    get_job,
    list_jobs,
    mark_job,
    record_run_event,
    release_runner_lock,
)


class CliTests(unittest.TestCase):
    def test_history_rejects_invalid_since_with_typed_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(
                    [
                        "--repo",
                        td,
                        "history",
                        "--since",
                        "yesterday",
                        "--json",
                    ]
                )
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(payload["error"]["code"], "queue_error")
            self.assertIn("ISO-8601", payload["error"]["message"])

    def test_stats_text_mode_renders_the_payload_it_is_given(self) -> None:
        # Text mode is the only reader of stats_payload that is not covered by
        # the JSON contract fingerprint, so a payload key rename can (and did,
        # in 0.9.0/0.9.1) crash it with a KeyError while every other test and
        # the fingerprint gate stay green. Seed a real finished train so the
        # duration/gate branches actually execute rather than short-circuiting
        # on empty data.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("statstext"), encoding="utf-8"
            )
            conn = connect(db)
            try:
                conn.execute(
                    "UPDATE recovery_operation_events "
                    "SET detail='schema_version=11;history_complete=0' "
                    "WHERE operation='tracking'"
                )
                conn.commit()
                enqueue_job(conn, task="a", branch="agent/a")
                # Claim it rather than marking it terminal directly: started_at
                # is written by the claim, and without it the train has no
                # measurable duration and the branch under test prints None.
                claimed = claim_next_job(conn)
                assert claimed is not None
                mark_job(
                    conn,
                    claimed.id,
                    status="deployed",
                    push_status="succeeded",
                    verify_status="succeeded",
                    train_id="t-stats",
                    train_size=1,
                    expected_claim_token=claimed.claim_token,
                )
                # Must match observability.GATE_EVENT or the per-gate branch
                # of the text renderer is never reached.
                record_run_event(
                    conn,
                    job_id=claimed.id,
                    phase="gate",
                    state="running",
                    message="Running gate 1/1: tests",
                )
                record_run_event(
                    conn,
                    job_id=claimed.id,
                    phase="gate",
                    state="succeeded",
                    message="Passed gate 1/1: tests",
                )
                release_runner_lock(conn)
            finally:
                conn.close()

            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["--repo", str(repo), "--db", str(db), "stats"])

            self.assertEqual(code, 0, f"stats text mode failed: {err.getvalue()}")
            text = out.getvalue()
            self.assertIn("trains:", text)
            self.assertIn("land rate:", text)
            self.assertIn("terminal land rate:", text)
            self.assertIn("validation runs:", text)
            self.assertIn("validated trains:", text)
            self.assertIn("batching:", text)
            self.assertIn("batch savings estimate:", text)
            self.assertIn("recovery operations:", text)
            self.assertIn(
                "evidence gap recovery_reconcile_frequency_before_tracking_start:",
                text,
            )
            self.assertIn("duration:", text)
            self.assertIn("average queue=", text)
            # A KeyError-driven regression would print the literal repr of a
            # missing value rather than a number, so pin that the duration
            # line actually carries the flat keys' values.
            self.assertNotIn("None", text.split("duration:")[1].split("\n")[0])

    def test_single_repo_daemon_accepts_notify_and_builds_configured_chain(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                "project:\n  name: demo\nnotify:\n  webhook_url: https://example.invalid/hook\n",
                encoding="utf-8",
            )
            with patch("mergetrain.commands.daemon.daemon_loop") as loop:
                code = main(["--repo", str(repo), "daemon", "--once", "--notify"])

            self.assertEqual(code, 0)
            self.assertIsNotNone(loop.call_args.kwargs["notifier"])
            self.assertEqual(loop.call_args.kwargs["notification_name"], "demo")
            self.assertEqual(
                loop.call_args.kwargs["notification_transitions"],
                ("landed", "blocked", "needs_reconcile", "daemon_paused"),
            )

    def test_single_repo_daemon_warns_when_notify_has_no_webhook(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                "project:\n  name: demo\n",
                encoding="utf-8",
            )
            err = io.StringIO()
            with patch("mergetrain.commands.daemon.daemon_loop") as loop, redirect_stderr(err):
                code = main(["--repo", str(repo), "daemon", "--once", "--notify"])

            self.assertEqual(code, 0)
            self.assertIn("no headless notification backend", err.getvalue())
            self.assertIsNotNone(loop.call_args.kwargs["notifier"])

    def test_single_repo_validation_daemon_never_deploys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(render_default_config("demo"), encoding="utf-8")
            with (
                patch("mergetrain.commands.daemon.GitRunner") as runner_type,
                patch("mergetrain.commands.daemon.daemon_loop") as loop,
            ):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "daemon",
                        "--once",
                        "--validate-only",
                    ]
                )
                self.assertEqual(code, 0)
                self.assertTrue(loop.call_args.kwargs["validate_only"])
                callback = loop.call_args.kwargs["process_batch"]
                callback(None, [Job(id=1, task="t", branch="b")])
                self.assertFalse(runner_type.return_value.process_batch.call_args.kwargs["deploy"])

    def test_single_repo_validation_daemon_rejects_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(render_default_config("demo"), encoding="utf-8")
            err = io.StringIO()
            with patch("mergetrain.commands.daemon.daemon_loop") as loop, redirect_stderr(err):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "daemon",
                        "--once",
                        "--validate-only",
                        "--notify",
                    ]
                )

            self.assertEqual(code, 1)
            self.assertIn("cannot be combined", err.getvalue())
            loop.assert_not_called()

    def test_hub_daemon_warns_once_per_repo_when_notify_has_no_webhook(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                "project:\n  name: demo\n",
                encoding="utf-8",
            )

            def run_loop(**kwargs):  # type: ignore[no-untyped-def]
                resolver = kwargs["notifier_resolver"]
                self.assertIsNone(resolver(str(repo), "landed:1"))
                self.assertIsNone(resolver(str(repo), "landed:2"))
                return []

            out, err = io.StringIO(), io.StringIO()
            with (
                patch("mergetrain.hub_daemon.hub_daemon_loop", side_effect=run_loop),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                code = main(["hub", "daemon", "--once", "--json", "--notify"])

            self.assertEqual(code, 0)
            self.assertEqual(err.getvalue().count("no configured webhook for demo"), 1)
            self.assertEqual(
                json.loads(out.getvalue()),
                {"contract_version": CONTRACT_VERSION, "ok": True, "outcomes": []},
            )

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
        # Contract 1: ok = the run executed; the graded outcome is in `result`.
        # A completed deploy with a verify warning is ok:true, result:"warning".
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"], "warning")
        self.assertEqual(payload["push_counts"], {"succeeded": 1})
        self.assertEqual(payload["verify_counts"], {"failed": 1})
        self.assertEqual(payload["jobs"][0]["status"], "deployed")
        self.assertEqual(
            _job_result_line(payload["jobs"][0]),
            "#1 deployed (push=succeeded, verify=failed): feature/a",
        )

    def test_run_exit_code_treats_verify_warning_as_shipped(self) -> None:
        # A shipped train whose post-push verify warned must not report the same
        # exit 1 as a run that never shipped — exit 1 means "did not ship".
        self.assertEqual(_run_exit_code({"result": "success"}), 0)
        self.assertEqual(_run_exit_code({"result": "warning"}), 0)
        self.assertEqual(_run_exit_code({"result": "partial"}), 1)
        self.assertEqual(_run_exit_code({"result": "failed"}), 1)

    def test_interrupted_json_envelope_carries_retryable(self) -> None:
        # Ctrl-C during a --json command must emit the one failure shape
        # {code,message,retryable}, not a two-key envelope that KeyErrors a
        # consumer reading error.retryable.
        out = io.StringIO()
        with (
            patch("mergetrain.cli.cmd_status", side_effect=KeyboardInterrupt),
            redirect_stdout(out),
        ):
            code = main(["status", "--json"])
        self.assertEqual(code, 130)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "interrupted")
        self.assertEqual(payload["error"]["message"], "interrupted")
        self.assertFalse(payload["error"]["retryable"])

    def test_legacy_version_output_remains_compatible(self) -> None:
        out = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(out):
            main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(out.getvalue(), f"mergetrain {__version__}\n")

    def test_status_diagnose_exposes_runtime_provenance(self) -> None:
        runtime = {
            "distribution_version": "0.1.0",
            "package_path": "/tmp/site-packages/mergetrain",
            "install_mode": "wheel",
            "source_path": None,
            "source_commit": "a" * 40,
            "source_dirty": None,
        }
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("runtime-diagnose"), encoding="utf-8"
            )
            out = io.StringIO()
            with (
                patch("mergetrain.runtime.runtime_provenance", return_value=runtime),
                redirect_stdout(out),
            ):
                code = main(
                    ["--repo", str(repo), "status", "--diagnose", "--json"]
                )
            payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["diagnostics"]["version"], __version__)
        self.assertEqual(payload["diagnostics"]["runtime"], runtime)

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
            with (
                patch("mergetrain.runtime.runtime_provenance", return_value=runtime),
                redirect_stdout(out),
            ):
                code = main(["--repo", str(repo), "status", "--diagnose", "--json"])
            payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["diagnostics"]["runtime"], runtime)

    def test_doctor_json_redacts_remote_url_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "https://x-access-token:fixture-secret@example.com/repo.git",
                ],
                cwd=repo,
                check=True,
            )
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "status", "--diagnose", "--json"])
            payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["diagnostics"]["git"]["remote_url"],
            "https://x-access-token:[redacted]@example.com/repo.git",
        )
        self.assertNotIn("fixture-secret", out.getvalue())

    def test_doctor_reports_operator_config_in_sync_with_integration_ref(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo,
                check=True,
            )
            (repo / ".mergetrain.yaml").write_text(render_default_config("drift"), encoding="utf-8")
            subprocess.run(["git", "add", ".mergetrain.yaml"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "add config"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "update-ref",
                    "refs/remotes/origin/main",
                    "HEAD",
                ],
                cwd=repo,
                check=True,
            )

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "status", "--diagnose", "--json"])
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        diagnostics = payload["diagnostics"]
        self.assertEqual(diagnostics["config_drift"]["state"], "in_sync")
        self.assertTrue(diagnostics["config_drift"]["comparable"])
        self.assertTrue(diagnostics["config_drift"]["matches"])
        self.assertEqual(diagnostics["recommendations"], [])

    def test_doctor_normalizes_worktree_line_endings_for_config_drift(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo,
                check=True,
            )
            config_text = render_default_config("drift")
            config_path = repo / ".mergetrain.yaml"
            config_path.write_text(config_text, encoding="utf-8")
            subprocess.run(["git", "add", ".mergetrain.yaml"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "add config"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "update-ref",
                    "refs/remotes/origin/main",
                    "HEAD",
                ],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "core.autocrlf", "true"],
                cwd=repo,
                check=True,
            )
            config_path.write_bytes(config_text.replace("\n", "\r\n").encode("utf-8"))

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "status", "--diagnose", "--json"])
            payload = json.loads(out.getvalue())

        drift = payload["diagnostics"]["config_drift"]
        self.assertEqual(code, 0)
        self.assertEqual(drift["state"], "in_sync")
        self.assertTrue(drift["matches"])
        self.assertEqual(
            drift["local"]["blob_sha"],
            drift["integration"]["blob_sha"],
        )

    def test_doctor_reports_redundant_builtin_diff_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo,
                check=True,
            )
            config_text = render_default_config("duplicate").replace(
                "gates: []",
                "gates:\n  - name: diff-check\n    run: git diff --check ${integration_ref}..HEAD",
            )
            (repo / ".mergetrain.yaml").write_text(
                config_text,
                encoding="utf-8",
            )
            subprocess.run(["git", "add", ".mergetrain.yaml"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "add config"], cwd=repo, check=True)
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                cwd=repo,
                check=True,
            )

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "status", "--diagnose", "--json"])
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        recommendation = payload["diagnostics"]["recommendations"][0]
        self.assertEqual(
            recommendation["code"],
            "redundant_builtin_diff_check",
        )
        self.assertEqual(recommendation["evidence"]["configured_gate_count"], 1)

    def test_doctor_warns_when_operator_config_drifted_from_integration_ref(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo,
                check=True,
            )
            config_path = repo / ".mergetrain.yaml"
            config_path.write_text(render_default_config("drift"), encoding="utf-8")
            subprocess.run(["git", "add", ".mergetrain.yaml"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "add config"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "update-ref",
                    "refs/remotes/origin/main",
                    "HEAD",
                ],
                cwd=repo,
                check=True,
            )
            config_path.write_text(render_default_config("operator-copy"), encoding="utf-8")

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "status", "--diagnose", "--json"])
            payload = json.loads(out.getvalue())

        diagnostics = payload["diagnostics"]
        drift = diagnostics["config_drift"]
        self.assertEqual(code, 0)
        self.assertEqual(drift["state"], "drifted")
        self.assertTrue(drift["comparable"])
        self.assertFalse(drift["matches"])
        self.assertNotEqual(
            drift["local"]["blob_sha"],
            drift["integration"]["blob_sha"],
        )
        self.assertEqual(
            diagnostics["recommendations"][0]["code"],
            "operator_config_drift",
        )
        self.assertEqual(payload["next_action"]["code"], "configure_git_remote")

    def test_doctor_compares_nested_repo_config_from_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            nested = repo / "services" / "api"
            nested.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo,
                check=True,
            )
            (nested / ".mergetrain.yaml").write_text(
                render_default_config("nested"), encoding="utf-8"
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "add nested config"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "update-ref",
                    "refs/remotes/origin/main",
                    "HEAD",
                ],
                cwd=repo,
                check=True,
            )

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(nested), "status", "--diagnose", "--json"])
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        diagnostics = payload["diagnostics"]
        self.assertEqual(diagnostics["config_drift"]["state"], "in_sync")
        self.assertEqual(
            diagnostics["config_drift"]["local"]["path"],
            "services/api/.mergetrain.yaml",
        )

    def test_doctor_does_not_invent_drift_without_an_integration_ref(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".mergetrain.yaml").write_text(render_default_config("drift"), encoding="utf-8")

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "status", "--diagnose", "--json"])
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        diagnostics = payload["diagnostics"]
        self.assertEqual(
            diagnostics["config_drift"]["state"],
            "integration_ref_missing",
        )
        self.assertFalse(diagnostics["config_drift"]["comparable"])
        self.assertIsNone(diagnostics["config_drift"]["matches"])
        self.assertEqual(diagnostics["recommendations"], [])

    def test_results_payload_reports_failure_and_partial_outcomes(self) -> None:
        # ok stays true (the run executed); the outcome is graded in `result`.
        failed = _results_payload([Job(id=1, task="a", branch="a", status="failed")])
        self.assertTrue(failed["ok"])
        self.assertEqual(failed["result"], "failed")
        partial = _results_payload(
            [
                Job(id=1, task="a", branch="a", status="validated"),
                Job(id=2, task="b", branch="b", status="blocked"),
            ]
        )
        self.assertTrue(partial["ok"])
        self.assertEqual(partial["result"], "partial")
        self.assertEqual(partial["counts"], {"blocked": 1, "validated": 1})
        self.assertNotIn("claim_token", partial["jobs"][0])

    def test_job_json_redacts_legacy_url_credentials(self) -> None:
        job = Job(
            id=1,
            task="a",
            branch="a",
            status="failed",
            note="push https://user:fixture-secret@example.com/repo.git failed",
        )
        payload = _results_payload([job])
        self.assertNotIn("fixture-secret", json.dumps(payload))
        self.assertIn("https://user:[redacted]@example.com", payload["jobs"][0]["note"])

    def test_inspect_progress_fallback_redacts_a_legacy_note(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            conn = connect(db)
            try:
                job = enqueue_job(conn, task="legacy", branch="feature/legacy")
                mark_job(
                    conn,
                    job.id,
                    status="blocked",
                    note="ACCESS_TOKEN=inspect-secret " + "x" * 1200,
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
                        str(job.id),
                        "--json",
                    ]
                )
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        self.assertNotIn("inspect-secret", json.dumps(payload))
        self.assertEqual(len(payload["progress"]["message"]), 1000)
        self.assertTrue(payload["progress"]["message_truncated"])
        self.assertEqual(len(payload["outcome"]["message"]), 1000)
        self.assertTrue(payload["outcome"]["message_truncated"])
        self.assertEqual(len(payload["job"]["note"]), 1000)
        self.assertTrue(payload["job"]["note_truncated"])

    def test_json_mode_emits_structured_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text("git:\n  push_refs: []\n", encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "status", "--diagnose", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "config_error")

    def test_contract_envelope_ok_is_uniform_and_health_is_separate(self) -> None:
        # A valid but unconfigured repo: the command ran (ok:true), and the
        # repo-health verdict lives in its own `health` field, not in `ok`.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "status", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertIn("health", payload)
            self.assertIn("next_action", payload)

    def test_status_carries_structured_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "status", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["next_action"]["code"], "initialize_config")
            self.assertEqual(payload["next_action"]["command"], "mergetrain init --write")

    def test_next_action_points_at_init_before_any_queue_advice(self) -> None:
        # An unconfigured repo used to be told to enqueue a branch, which
        # enqueue then refused with config_error. next_action is the signal
        # agents are told to act on, so it has to name the actual blocker.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "status", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["next_action"]["code"], "initialize_config")

            main(["--repo", str(repo), "init", "--project", "demo", "--write"])
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--repo", str(repo), "status", "--json"])
            initialized = json.loads(out.getvalue())
            self.assertEqual(initialized["health"], "degraded")
            self.assertEqual(
                initialized["next_action"]["code"],
                "configure_git_remote",
            )

    def test_status_without_queue_is_read_only_and_does_not_create_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            before = sorted(path.relative_to(repo) for path in repo.rglob("*") if ".git" not in path.parts)

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "status", "--json"])

            after = sorted(path.relative_to(repo) for path in repo.rglob("*") if ".git" not in path.parts)
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(before, after)
            self.assertFalse((repo / ".mergetrain").exists())
            self.assertEqual(payload["counts"]["waiting"], 0)

    def test_status_requires_remote_and_integration_ref_before_enqueue_advice(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("readiness"), encoding="utf-8"
            )

            out = io.StringIO()
            with redirect_stdout(out):
                main(["--repo", str(repo), "status", "--json"])
            missing_remote = json.loads(out.getvalue())
            self.assertEqual(missing_remote["health"], "degraded")
            self.assertEqual(
                missing_remote["next_action"]["code"], "configure_git_remote"
            )

            subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=repo, check=True)
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--repo", str(repo), "status", "--json"])
            missing_ref = json.loads(out.getvalue())
            self.assertEqual(missing_ref["health"], "degraded")
            self.assertEqual(missing_ref["next_action"]["code"], "fetch_integration_ref")
            self.assertEqual(missing_ref["next_action"]["command"], "git fetch origin")

    def test_status_reports_a_configured_non_git_path_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            missing_repo = root / "missing"
            config_path = root / "config.yaml"
            config_path.write_text(render_default_config("missing"), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(
                    [
                        "--repo",
                        str(missing_repo),
                        "--config",
                        str(config_path),
                        "status",
                        "--diagnose",
                        "--json",
                    ]
                )
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["health"], "degraded")
            self.assertEqual(payload["next_action"]["code"], "open_git_repository")
            self.assertFalse(missing_repo.exists())

    def test_status_warns_when_only_builtin_diff_check_will_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("warning"), encoding="utf-8"
            )
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--repo", str(repo), "status", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["warnings"][0]["code"], "no_configured_gates")

    def test_root_typo_lists_only_the_public_command_grammar(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            main(["stat"])
        message = err.getvalue()
        for command in ("init", "status", "enqueue", "validate", "deploy", "inspect"):
            self.assertIn(repr(command), message)
        for hidden in ("daemon", "reconcile", "dashboard", "hub", "retry"):
            self.assertNotIn(repr(hidden), message)

    def test_removed_run_next_points_to_deploy_without_claiming(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".mergetrain.yaml").write_text(render_default_config("guard"), encoding="utf-8")
            conn = connect(db)
            try:
                approved = enqueue_job(conn, task="approved", branch="agent/one")
                mark_job(
                    conn,
                    approved.id,
                    status="validated",
                    train_id="t-guard",
                    train_size=1,
                    validated_at="2026-07-25T00:00:00Z",
                    validated_head_sha="a" * 40,
                    validation_base_sha="b" * 40,
                    validation_sha="a" * 40,
                )
                enqueue_job(conn, task="later", branch="agent/two")
            finally:
                conn.close()

            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(
                    ["--repo", str(repo), "--db", str(db), "run-next", "--deploy", "--json"]
                )
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 2)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "removed_interface")
            self.assertIn("mergetrain deploy", payload["error"]["message"])

            # The queued job must be untouched — refusing is not claiming.
            conn = connect(db)
            try:
                queued = [job for job in list_jobs(conn, limit=10) if job.task == "later"]
            finally:
                conn.close()
            self.assertEqual(queued[0].status, "queued")

    def test_next_action_names_a_stranded_claim(self) -> None:
        # A row claimed by a runner that no longer holds the lock is what a crash
        # -- or queue contention that raised after the lease was released --
        # leaves behind. doctor used to report an idle queue for it, while the
        # next deploy would requeue it and clear its validated-train identity.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("stranded"), encoding="utf-8"
            )
            conn = connect(db)
            try:
                job = enqueue_job(conn, task="a", branch="agent/a")
                mark_job(conn, job.id, status="in_progress", note="claimed")
            finally:
                conn.close()

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "--db", str(db), "status", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["next_action"]["code"], "reconcile_stranded_claim")
            self.assertEqual(
                payload["next_action"]["command"],
                "mergetrain reconcile --apply",
            )

    def test_status_rejects_non_positive_limits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for limit in ("0", "-5"):
                with self.subTest(limit=limit):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        code = main(
                            [
                                "--repo",
                                str(repo),
                                "status",
                                "--limit",
                                limit,
                                "--json",
                            ]
                        )
                    payload = json.loads(out.getvalue())
                    self.assertEqual(code, 1)
                    self.assertEqual(payload["error"]["code"], "queue_error")
                    self.assertIn("--limit must be 1 or greater", payload["error"]["message"])

    def test_status_defaults_to_ten_recent_jobs_but_keeps_all_attention(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            conn = connect(db)
            try:
                old_attention = enqueue_job(
                    conn,
                    task="old blocked",
                    branch="feature/blocked",
                )
                mark_job(conn, old_attention.id, status="blocked", note="fix me")
                for index in range(12):
                    job = enqueue_job(
                        conn,
                        task=f"done {index}",
                        branch=f"feature/done-{index}",
                    )
                    mark_job(
                        conn,
                        job.id,
                        status="deployed",
                        push_status="succeeded",
                        verify_status="succeeded",
                    )
            finally:
                conn.close()

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "--db", str(db), "status", "--json"])
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(len(payload["recent_jobs"]), 10)
        self.assertEqual(payload["counts"]["done"], 12)
        self.assertEqual(payload["counts"]["attention"], 1)
        self.assertEqual(
            [job["id"] for job in payload["attention_jobs"]],
            [old_attention.id],
        )

    def test_status_redacts_and_bounds_persisted_attention_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            note = (
                "API_TOKEN=super-secret-value --token option-secret "
                "https://user:url-secret@example.test/ "
                + "x" * 1200
            )
            conn = connect(db)
            try:
                job = enqueue_job(conn, task="secret note", branch="feature/secret")
                mark_job(conn, job.id, status="blocked", note=note)
            finally:
                conn.close()

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "--db", str(db), "status", "--json"])
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        rendered = json.dumps(payload)
        for secret in ("super-secret-value", "option-secret", "url-secret"):
            self.assertNotIn(secret, rendered)
        for key in ("attention_jobs", "recent_jobs"):
            summary = payload[key][0]
            self.assertLessEqual(len(summary["reason"]), 1000)
            self.assertTrue(summary["reason_truncated"])
            self.assertIn("API_TOKEN=[redacted]", summary["reason"])
            self.assertIn("--token [redacted]", summary["reason"])
            self.assertIn("https://user:[redacted]@example.test", summary["reason"])

    def test_status_reads_counts_and_action_rows_from_one_snapshot(self) -> None:
        from mergetrain.persistence.jobs import counts as persisted_counts

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            conn = connect(db)
            try:
                failed_verify = enqueue_job(
                    conn, task="failed verify", branch="feature/verify"
                )
                mark_job(
                    conn,
                    failed_verify.id,
                    status="deployed",
                    push_status="succeeded",
                    verify_status="failed",
                )
                blocked = enqueue_job(conn, task="blocked", branch="feature/blocked")
                mark_job(conn, blocked.id, status="blocked", note="gate stopped")
            finally:
                conn.close()

            def counts_then_commit(read_conn):
                observed = persisted_counts(read_conn)
                writer = sqlite3.connect(db)
                try:
                    writer.execute(
                        "UPDATE deploy_queue SET verify_status='succeeded' WHERE id=?",
                        (failed_verify.id,),
                    )
                    writer.commit()
                finally:
                    writer.close()
                return observed

            out = io.StringIO()
            with (
                patch(
                    "mergetrain.commands.inspection.counts",
                    side_effect=counts_then_commit,
                ),
                redirect_stdout(out),
            ):
                code = main(["--repo", str(repo), "--db", str(db), "status", "--json"])
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["counts"]["attention"], 2)
        self.assertEqual(
            {item["id"] for item in payload["attention_jobs"]},
            {failed_verify.id, blocked.id},
        )
        self.assertEqual(payload["next_action"]["target_job_id"], failed_verify.id)
        self.assertEqual(
            payload["next_action"]["reason_code"],
            "post_push_verification_failed",
        )

    def test_status_keeps_failed_verify_in_attention_and_plans_one_exact_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            conn = connect(db)
            try:
                verify_failed = enqueue_job(conn, task="production verify", branch="feature/v")
                mark_job(
                    conn,
                    verify_failed.id,
                    status="deployed",
                    push_status="succeeded",
                    verify_status="failed",
                    train_id="train-shared",
                    deploy_sha="shared-deploy",
                )
                blocked = enqueue_job(conn, task="blocked", branch="feature/b")
                mark_job(conn, blocked.id, status="blocked", note="gate stopped")
                verify_unknown = enqueue_job(conn, task="unknown", branch="feature/u")
                mark_job(
                    conn,
                    verify_unknown.id,
                    status="deployed",
                    push_status="succeeded",
                    verify_status="unknown",
                    train_id="train-shared",
                    deploy_sha="shared-deploy",
                )
            finally:
                conn.close()

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "--db", str(db), "status", "--json"])
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["counts"]["attention"], 3)
        self.assertEqual(payload["counts"]["done"], 0)
        self.assertEqual(
            payload["next_action"]["code"], "resolve_failed_verification"
        )
        self.assertEqual(payload["next_action"]["target_job_id"], verify_failed.id)
        self.assertEqual(
            payload["next_action"]["command"],
            f"mergetrain verify --job {verify_failed.id}",
        )
        reasons = {job["id"]: job["reason_code"] for job in payload["attention_jobs"]}
        self.assertEqual(reasons[verify_failed.id], "post_push_verification_failed")
        self.assertEqual(reasons[blocked.id], "blocked")
        self.assertEqual(reasons[verify_unknown.id], "post_push_verification_unknown")

    def test_status_keeps_old_verify_failure_actionable_until_explicit_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            conn = connect(db)
            try:
                old_failure = enqueue_job(
                    conn,
                    task="old production verify",
                    branch="feature/old",
                )
                mark_job(
                    conn,
                    old_failure.id,
                    status="deployed",
                    push_status="succeeded",
                    verify_status="failed",
                    train_id="train-old",
                    deploy_sha="deploy-old",
                )
                current = enqueue_job(
                    conn,
                    task="new production deploy",
                    branch="feature/new",
                )
                mark_job(
                    conn,
                    current.id,
                    status="deployed",
                    push_status="succeeded",
                    verify_status="succeeded",
                    train_id="train-new",
                    deploy_sha="deploy-new",
                )
            finally:
                conn.close()

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "--db", str(db), "status", "--json"])
            payload = json.loads(out.getvalue())

            conn = connect(db, read_only=True)
            try:
                persisted_failure = get_job(conn, old_failure.id)
            finally:
                conn.close()

        self.assertEqual(code, 0)
        self.assertEqual(payload["counts"]["attention"], 1)
        self.assertEqual(payload["counts"]["done"], 1)
        recent_by_id = {job["id"]: job for job in payload["recent_jobs"]}
        self.assertEqual(recent_by_id[old_failure.id]["state"], "attention")
        self.assertIsNone(recent_by_id[old_failure.id]["outcome"])
        self.assertEqual(
            payload["next_action"]["target_job_id"],
            old_failure.id,
        )
        self.assertEqual(persisted_failure.verify_status, "failed")

    def test_enqueue_defaults_to_repo_instead_of_process_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(
                ["git", "init", "-q", "--initial-branch=feature/repo-default", str(repo)],
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"], cwd=repo, check=True
            )
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("repo-default"), encoding="utf-8"
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "ready"], cwd=repo, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=repo, check=True)
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                cwd=repo,
                check=True,
            )

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "enqueue",
                        "--task",
                        "repo-root default",
                        "--branch",
                        "feature/repo-default",
                        "--json",
                    ]
                )
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(Path(payload["job"]["worktree_path"]), repo.resolve())
        self.assertEqual(payload["job"]["status"], "queued")

    def test_contract1_version_stamped_top_level_not_nested(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".mergetrain.yaml").write_text(render_default_config("demo"), encoding="utf-8")
            conn = connect(repo / ".mergetrain" / "queue.sqlite")
            enqueue_job(conn, task="a", branch="feature/a")
            conn.close()
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--repo", str(repo), "status", "--json"])
            payload = json.loads(out.getvalue())
            # Top-level frame carries the number...
            self.assertEqual(payload["contract_version"], CONTRACT_VERSION)
            # ...nested job dicts do NOT (the outer frame owns it).
            self.assertNotIn("contract_version", payload["recent_jobs"][0])

    def test_removed_agent_contract_points_to_instruction_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "agent-contract", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 2)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "removed_interface")
            self.assertIn("init --refresh-instructions", payload["error"]["message"])

    def test_duplicate_branch_surfaces_typed_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / ".mergetrain.yaml").write_text(render_default_config("demo"), encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "ready"], cwd=repo, check=True)
            subprocess.run(["git", "branch", "-M", "feature/a"], cwd=repo, check=True)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", head],
                cwd=repo,
                check=True,
            )
            base = [
                "--repo",
                str(repo),
                "enqueue",
                "--task",
                "a",
                "--branch",
                "feature/a",
                "--worktree",
                str(repo),
            ]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(base), 0)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main([*base, "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 1)
            # Agents branch on error.code, not the free-text message.
            self.assertEqual(payload["error"]["code"], "duplicate_active_branch")

    def test_enqueue_captures_exact_shas_without_opt_in_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("default-capture"), encoding="utf-8"
            )
            (repo / "app.txt").write_text("reviewed\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "reviewed"], cwd=repo, check=True)
            subprocess.run(
                ["git", "branch", "-M", "feature/default-capture"],
                cwd=repo,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", head],
                cwd=repo,
                check=True,
            )

            def enqueue(*extra: str) -> tuple[int, dict[str, object]]:
                out = io.StringIO()
                with redirect_stdout(out):
                    code = main(
                        [
                            "--repo",
                            str(repo),
                            "enqueue",
                            "--task",
                            "default capture",
                            "--branch",
                            "feature/default-capture",
                            "--worktree",
                            str(repo),
                            *extra,
                            "--json",
                        ]
                    )
                return code, json.loads(out.getvalue())

            for removed in ("--base-sha", "--head-sha"):
                code, payload = enqueue(removed, "0" * 40)
                self.assertEqual(code, 2, payload)
                self.assertEqual(payload["error"]["code"], "removed_interface")
                self.assertIn(removed, payload["error"]["message"])

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "enqueue",
                        "--task",
                        "default capture",
                        "--branch",
                        "feature/default-capture",
                        "--worktree",
                        str(repo),
                        "--json",
                    ]
                )
            payload = json.loads(out.getvalue())

            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["job"]["base_sha"], head)
            self.assertEqual(payload["job"]["head_sha"], head)

            conn = connect(repo / ".mergetrain" / "queue.sqlite")
            try:
                self.assertEqual(len(list_jobs(conn)), 1)
            finally:
                conn.close()

            code, payload = enqueue("--allow-duplicate")
            self.assertEqual(code, 2, payload)
            self.assertEqual(payload["error"]["code"], "removed_interface")

    def test_retry_captures_fresh_shas_and_inherits_job_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / ".mergetrain.yaml").write_text(render_default_config("demo"), encoding="utf-8")
            (repo / "app.txt").write_text("fixed\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "fixed"], cwd=repo, check=True)
            subprocess.run(["git", "branch", "-M", "feature/retry"], cwd=repo, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(repo / "origin.git")],
                cwd=repo,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", head],
                cwd=repo,
                check=True,
            )
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                original = enqueue_job(
                    conn,
                    task="fix retry",
                    branch="feature/retry",
                    worktree_path=str(repo),
                    note="operator context",
                    auto_deploy=True,
                    approval_destination_sha=deploy_destination_sha(config),
                    approval_execution_policy_sha=(deploy_execution_policy_sha(config)),
                )
                mark_job(conn, original.id, status="failed", note="gate failed")
            finally:
                conn.close()

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "retry", str(original.id), "--json"])
            payload = json.loads(out.getvalue())

            self.assertEqual(code, 0)
            # retry dismisses exactly one outcome, so the key is singular and the
            # value is an object; `dismiss` keeps `dismissed` for its array.
            self.assertEqual(payload["dismissed_job"]["status"], "canceled")
            self.assertEqual(payload["job"]["status"], "queued")
            self.assertEqual(payload["job"]["task"], "fix retry")
            self.assertEqual(payload["job"]["note"], "gate failed")
            self.assertTrue(payload["job"]["auto_deploy"])
            self.assertEqual(payload["job"]["base_sha"], head)
            self.assertEqual(payload["job"]["head_sha"], head)

    def test_supersede_cli_captures_clean_shas_without_inheriting_approval(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=repo,
                check=True,
            )
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("supersede"), encoding="utf-8"
            )
            (repo / "app.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "update-ref",
                    "refs/remotes/origin/main",
                    base,
                ],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "switch", "-qc", "feature/replacement"],
                cwd=repo,
                check=True,
            )
            (repo / "app.txt").write_text("replacement\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "replacement"],
                cwd=repo,
                check=True,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()

            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                old = enqueue_job(
                    conn,
                    task="old train",
                    branch="feature/old",
                )
                mark_job(
                    conn,
                    old.id,
                    status="validated",
                    train_id="train-old",
                    train_size=1,
                    validated_at="2026-07-29T00:00:00Z",
                    validation_sha="validated-old",
                )
            finally:
                conn.close()

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "supersede",
                        "--train-id",
                        "train-old",
                        "--replacement",
                        "release finalization",
                        "feature/replacement",
                        str(repo),
                        "--json",
                    ]
                )
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["superseded_train_id"], "train-old")
        self.assertEqual(payload["superseded_jobs"][0]["status"], "canceled")
        self.assertEqual(
            payload["superseded_jobs"][0]["validation_sha"],
            "validated-old",
        )
        replacement = payload["replacement_jobs"][0]
        self.assertEqual(replacement["status"], "queued")
        self.assertEqual(replacement["base_sha"], base)
        self.assertEqual(replacement["head_sha"], head)
        self.assertEqual(replacement["supersedes_train_id"], "train-old")
        self.assertEqual(
            replacement["supersession_id"],
            payload["supersession_id"],
        )
        self.assertEqual(replacement["validated_at"], "")
        self.assertEqual(replacement["train_id"], "")
        self.assertEqual(payload["next_action"], "validate_queued_jobs")

    def test_retry_rebase_error_does_not_dismiss_original_job(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / ".mergetrain.yaml").write_text(render_default_config("demo"), encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            subprocess.run(["git", "branch", "-M", "feature/retry"], cwd=repo, check=True)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                original = enqueue_job(
                    conn,
                    task="conflict",
                    branch="feature/retry",
                    worktree_path=str(repo),
                )
                mark_job(conn, original.id, status="blocked", note="merge conflict")
            finally:
                conn.close()

            failure = CommandFailed(
                ["git", "rebase", "origin/main"],
                1,
                "",
                "CONFLICT",
                str(repo),
            )
            out = io.StringIO()
            with (
                patch("mergetrain.commands.queue.run_command", side_effect=failure),
                redirect_stdout(out),
            ):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "retry",
                        str(original.id),
                        "--rebase",
                        "--json",
                    ]
                )
            payload = json.loads(out.getvalue())
            conn = connect(config.state.db)
            try:
                current = get_job(conn, original.id)
                job_count = conn.execute("SELECT COUNT(*) FROM deploy_queue").fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(code, 1)
            self.assertEqual(payload["error"]["code"], "command_failed")
            self.assertEqual(current.status, "blocked")
            self.assertEqual(job_count, 1)

    def test_too_new_config_fails_deploy_path_but_permits_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".mergetrain.yaml").write_text(
                "version: 999\nproject:\n  name: future\n", encoding="utf-8"
            )

            def run(argv):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = main(["--repo", str(repo), *argv, "--json"])
                return code, json.loads(out.getvalue())

            # Deploy/state-shipping path: fail closed with the unified envelope.
            code, payload = run(["validate"])
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "config_error")

            # Recovery stays permissive — a rollback must not lock it out.
            code, payload = run(["reconcile"])
            self.assertEqual(payload.get("ok"), True)

            # The single state entry point remains available and points at the fix.
            code, payload = run(["status", "--diagnose"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["next_action"]["code"], "upgrade_mergetrain")
            self.assertEqual(payload["diagnostics"]["config"]["config_version"], 999)

    def test_dismiss_all_reaches_blocked_jobs_older_than_display_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".mergetrain.yaml").write_text(render_default_config("demo"), encoding="utf-8")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                old = enqueue_job(conn, task="old", branch="feature/old")
                mark_job(conn, old.id, status="blocked", note="old failure")
                conn.executemany(
                    "INSERT INTO deploy_queue "
                    "(task, branch, status, requested_at) VALUES (?, ?, 'canceled', ?)",
                    [
                        (f"done-{index}", f"feature/done-{index}", "2026-01-01T00:00:00Z")
                        for index in range(1000)
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "dismiss", "--all", "--json"])
            payload = json.loads(out.getvalue())

            self.assertEqual(code, 0)
            self.assertEqual([item["id"] for item in payload["dismissed"]], [old.id])
            conn = connect(config.state.db)
            try:
                self.assertEqual(get_job(conn, old.id).status, "canceled")
            finally:
                conn.close()

    def test_missing_config_fails_deploy_path_but_permits_recovery(self) -> None:
        # #84 defect 6: with no .mergetrain.yaml, deploy-capable paths must not
        # ship against guessed defaults (origin/main, minimal gates). They fail
        # closed and point at `mergetrain init`; recovery and reads still work.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)

            def run(argv):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = main(["--repo", str(repo), *argv, "--json"])
                return code, json.loads(out.getvalue())

            code, payload = run(["validate"])
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "config_error")
            self.assertIn("init", payload["error"]["message"])

            # Enqueue is deploy-capable too — fail closed before any git checks.
            code, payload = run(["enqueue", "--task", "a", "--branch", "feature/a"])
            self.assertEqual(code, 1)
            self.assertEqual(payload["error"]["code"], "config_error")

            # Recovery and reads stay permissive — a missing config must not
            # lock the operator out of reconcile/status diagnostics.
            code, payload = run(["reconcile"])
            self.assertEqual(payload.get("ok"), True)
            code, payload = run(["status", "--diagnose"])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["health"], "unconfigured")

    def test_global_option_after_subcommand_is_normalized(self) -> None:
        normalized = normalize_global_options(["doctor", "--json", "--repo", "/tmp/example"])
        self.assertEqual(normalized[:2], ["--repo", "/tmp/example"])
        self.assertIn("doctor", normalized)

    def test_global_options_after_terminator_remain_command_data(self) -> None:
        argv = ["run-batch", "--validate-only", "--", "--repo", "/tmp/other"]
        self.assertEqual(normalize_global_options(argv), argv)
        value_argv = ["enqueue", "--task=--repo=/tmp/not-global", "feature/a"]
        self.assertEqual(normalize_global_options(value_argv), value_argv)

        with_global = [
            "doctor",
            "--repo",
            "/tmp/actual",
            "--",
            "--config=/tmp/passthrough",
        ]
        self.assertEqual(
            normalize_global_options(with_global),
            [
                "--repo",
                "/tmp/actual",
                "doctor",
                "--",
                "--config=/tmp/passthrough",
            ],
        )

    def test_agent_contract_has_five_core_rules(self) -> None:
        contract = render_agent_contract()
        self.assertIn("\n5. Never push", contract)
        self.assertNotIn("\n6. ", contract)
        self.assertIn("status --json", contract)
        self.assertIn("exact commits", contract)
        self.assertIn("every named finished branch", contract)
        self.assertIn("last successful enqueue", contract)
        self.assertIn('"Queue for validation" authorizes enqueue only', contract)
        self.assertIn("exact destination and execution policy", contract)

    def test_validate_pauses_while_one_exact_train_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("demo"),
                encoding="utf-8",
            )
            db = repo / ".mergetrain" / "queue.sqlite"
            conn = connect(db)
            try:
                ready = enqueue_job(conn, task="ready", branch="feature/ready")
                mark_job(
                    conn,
                    ready.id,
                    status="validated",
                    train_id="train-ready",
                    train_size=1,
                    validated_at="2026-09-03T00:00:00Z",
                    validation_base_sha="a" * 40,
                    validation_sha="b" * 40,
                    validated_head_sha="c" * 40,
                )
                waiting = enqueue_job(conn, task="waiting", branch="feature/waiting")
            finally:
                conn.close()

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "validate", "--json"])
            payload = json.loads(out.getvalue())

            self.assertEqual(code, 1)
            self.assertEqual(payload["error"]["code"], "validated_train_pending")
            self.assertEqual(payload["next_action"], "deploy_when_approved")
            conn = connect(db)
            try:
                self.assertEqual(get_job(conn, waiting.id).status, "queued")
            finally:
                conn.close()

    def test_deploy_preview_lists_exact_atomic_push_refspecs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            push_endpoint = repo / "upstream.git"
            subprocess.run(
                ["git", "remote", "add", "upstream", str(push_endpoint)],
                cwd=repo,
                check=True,
            )
            (repo / ".mergetrain.yaml").write_text(
                """git:
  remote: upstream
  integration_branch: main
  push_refs:
    - main
    - release
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
            with (
                patch(
                    "mergetrain.commands.deploy.GitRunner.preview_validated_reuse",
                    return_value=decision,
                ),
                redirect_stdout(out),
            ):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "deploy",
                        "--json",
                    ]
                )
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["result"], "confirmation_required")
            self.assertEqual(len(payload["deploy_plan_sha"]), 64)
            self.assertNotIn("terminology", payload)
            self.assertEqual(payload["push_plan"]["remote"], "upstream")
            self.assertEqual(payload["push_plan"]["url"], str(push_endpoint.resolve()))
            self.assertEqual(len(payload["push_plan"]["destination_sha"]), 64)
            self.assertEqual(
                [item["spec"] for item in payload["push_plan"]["refs"]],
                ["HEAD:main", "HEAD:release"],
            )
            self.assertEqual(
                payload["push_plan"]["audit_ref"],
                {
                    "source": "DEPLOY_SHA",
                    "target": "refs/mergetrain/deploys/<DEPLOY_SHA>",
                    "spec": "DEPLOY_SHA:refs/mergetrain/deploys/<DEPLOY_SHA>",
                    "retention": "permanent",
                },
            )
            self.assertEqual(payload["reuse"]["evaluation"], "exact")
            self.assertEqual(payload["reuse"]["estimated_savings"]["mode"], "unavailable")
            self.assertFalse(payload["reuse"]["estimated_savings"]["authorizes_reuse"])
            self.assertEqual(payload["warnings"][0]["code"], "no_configured_gates")
            self.assertNotIn("confirmed_command", payload)
            self.assertNotIn("train_id", payload)

    def test_expected_deploy_plan_rejects_destination_change_before_claim(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", str(repo / "origin.git")],
                cwd=repo,
                check=True,
            )
            config_path = repo / ".mergetrain.yaml"
            config_path.write_text(
                "git:\n  remote: origin\n  integration_branch: main\n  push_refs: [main]\n",
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
                    validated_at="2026-09-02T00:00:00Z",
                    validation_base_sha="a" * 40,
                    validation_sha="b" * 40,
                    validated_head_sha="c" * 40,
                )
            finally:
                conn.close()
            preview_out = io.StringIO()
            with redirect_stdout(preview_out):
                self.assertEqual(
                    main(
                        [
                            "--repo",
                            str(repo),
                            "--db",
                            str(db),
                            "deploy",
                            "--json",
                        ]
                    ),
                    0,
                )
            expected = json.loads(preview_out.getvalue())["deploy_plan_sha"]
            config_path.write_text(
                "git:\n  remote: origin\n  integration_branch: main\n  push_refs: [release]\n",
                encoding="utf-8",
            )

            deploy_out = io.StringIO()
            with redirect_stdout(deploy_out):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "deploy",
                        "--expected-plan",
                        expected,
                        "--json",
                    ]
                )
            payload = json.loads(deploy_out.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(payload["error"]["code"], "deploy_plan_changed")
            conn = connect(db)
            try:
                self.assertEqual(get_job(conn, job.id).status, "validated")
            finally:
                conn.close()

    def test_removed_run_batch_aliases_return_a_migration_error(self) -> None:
        for alias in ("--integrate", "--push"):
            with self.subTest(alias=alias):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = main(["run-batch", alias, "--json"])
                payload = json.loads(out.getvalue())
                self.assertEqual(code, 2)
                self.assertEqual(payload["error"]["code"], "removed_interface")
                self.assertIn("mergetrain validate", payload["error"]["message"])

    def test_removed_hub_list_command_is_rejected(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["hub", "list", "--json"])
        self.assertEqual(raised.exception.code, 2)

    def test_init_write_creates_generic_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "init", "--project", "demo", "--write"])
            self.assertEqual(code, 0)
            self.assertTrue((repo / ".mergetrain.yaml").exists())
            agent_contract = (repo / "AGENTS.mergetrain.md").read_text(encoding="utf-8")
            self.assertIn(
                "only an explicit request to run validation or the complete "
                "end-to-end workflow authorizes `validate`",
                agent_contract,
            )
            self.assertIn("Only a separately authorized runner uses `deploy` or a daemon", agent_contract)
            self.assertIn("human-readable exact plan", agent_contract)
            self.assertIn("Train IDs and hashes stay internal", agent_contract)
            self.assertEqual(
                (repo / ".mergetrain.yaml").read_text(encoding="utf-8"),
                "version: 2\n\nproject:\n  name: demo\n\ngates: []\n",
            )
            payload = json.loads(out.getvalue())
            self.assertIn("standard AGENTS.md and/or CLAUDE.md", payload["next_step"])

    def test_init_write_preflights_all_conflicts_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            existing = repo / "AGENTS.mergetrain.md"
            existing.write_text("keep me\n", encoding="utf-8")

            with patch("sys.stderr", io.StringIO()):
                code = main(["--repo", str(repo), "init", "--project", "demo", "--write"])

            self.assertEqual(code, 1)
            self.assertFalse((repo / ".mergetrain.yaml").exists())
            self.assertFalse((repo / "CLAUDE.mergetrain.md").exists())
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep me\n")

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
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "status",
                        "--diagnose",
                        "--json",
                    ]
                )
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            train = payload["diagnostics"]["validated_trains"][0]
            self.assertEqual(train["train_id"], "train-1")
            self.assertTrue(train["deploy_eligible"])
            self.assertIsNone(train["current_integration_sha"])
            self.assertIsNone(train["integration_changed_since_validation"])

    def test_status_and_doctor_explain_stale_validated_train_base(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True)
            subprocess.run(
                ["git", "config", "user.email", "tests@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Mergetrain Tests"],
                cwd=repo,
                check=True,
            )
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("stale-train"),
                encoding="utf-8",
            )
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True)
            validation_base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            (repo / "README.md").write_text("base\nadvanced\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "advance integration"],
                cwd=repo,
                check=True,
            )
            current_integration_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "update-ref",
                    "refs/remotes/origin/main",
                    current_integration_sha,
                ],
                cwd=repo,
                check=True,
            )

            conn = connect(db)
            try:
                job = enqueue_job(conn, task="stale", branch="feature/stale")
                mark_job(
                    conn,
                    job.id,
                    status="validated",
                    train_id="train-stale",
                    train_size=1,
                    validated_at="2026-09-02T00:00:00Z",
                    validation_base_sha=validation_base_sha,
                    validation_sha="b" * 40,
                    validated_head_sha="c" * 40,
                )
            finally:
                conn.close()

            status_out = io.StringIO()
            with redirect_stdout(status_out):
                status_code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "status",
                        "--diagnose",
                        "--json",
                    ]
                )
            status_payload = json.loads(status_out.getvalue())
            train = status_payload["diagnostics"]["validated_trains"][0]
            self.assertEqual(status_code, 0)
            self.assertEqual(train["current_integration_sha"], current_integration_sha)
            self.assertTrue(train["integration_changed_since_validation"])
            self.assertTrue(train["deploy_eligible"])

            text_out = io.StringIO()
            with redirect_stdout(text_out):
                text_code = main(["--repo", str(repo), "--db", str(db), "status", "--diagnose"])
            self.assertEqual(text_code, 0)
            self.assertIn("warning validated_train_base_changed", text_out.getvalue())

            diagnose_out = io.StringIO()
            with redirect_stdout(diagnose_out):
                diagnose_code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "status",
                        "--diagnose",
                        "--json",
                    ]
                )
            diagnose_payload = json.loads(diagnose_out.getvalue())
            recommendation = next(
                item
                for item in diagnose_payload["diagnostics"]["recommendations"]
                if item["code"] == "validated_train_base_changed"
            )
            self.assertEqual(diagnose_code, 0)
            self.assertEqual(
                recommendation["evidence"]["current_integration_sha"],
                current_integration_sha,
            )
            self.assertEqual(
                recommendation["evidence"]["trains"][0]["train_id"],
                "train-stale",
            )
            self.assertIn("gate policy", " ".join(recommendation["actions"]))

            conn = connect(db)
            try:
                conn.execute(
                    "UPDATE deploy_queue SET validation_base_sha = ? WHERE id = ?",
                    (current_integration_sha, job.id),
                )
                conn.commit()
            finally:
                conn.close()

            current_out = io.StringIO()
            with redirect_stdout(current_out):
                current_code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "status",
                        "--diagnose",
                        "--json",
                    ]
                )
            current_payload = json.loads(current_out.getvalue())
            self.assertEqual(current_code, 0)
            self.assertFalse(
                current_payload["diagnostics"]["validated_trains"][0][
                    "integration_changed_since_validation"
                ]
            )
            self.assertNotIn(
                "validated_train_base_changed",
                [item["code"] for item in current_payload["diagnostics"]["recommendations"]],
            )

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
            # Contract 1: every JSONL stream opens with a stream_start header
            # carrying the contract version (re-emitted on each connect/resume).
            self.assertEqual(records[0]["type"], "stream_start")
            self.assertEqual(records[0]["contract_version"], CONTRACT_VERSION)
            self.assertEqual(records[1]["id"], event.id)
            self.assertEqual(records[1]["type"], "event")
            self.assertNotIn("claim_token", records[1])
            self.assertEqual(records[-1]["type"], "stream_end")
            self.assertEqual(records[-1]["reason"], "success")

    def test_events_jsonl_error_ends_with_machine_readable_frame(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            connect(db).close()
            out = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "events",
                        "--train-id",
                        "missing",
                        "--jsonl",
                    ]
                )

            records = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual(code, 1)
            self.assertEqual(err.getvalue(), "")
            self.assertEqual(records[0]["type"], "stream_start")
            self.assertEqual(records[-1]["type"], "stream_end")
            self.assertEqual(records[-1]["reason"], "error")
            self.assertFalse(records[-1]["ok"])
            self.assertEqual(records[-1]["error"]["code"], "queue_error")

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
            with (
                patch(
                    "mergetrain.commands.inspection.time.sleep",
                    side_effect=KeyboardInterrupt,
                ),
                redirect_stdout(interrupted),
            ):
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

    def test_events_follow_reuses_one_read_only_connection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            conn = connect(db)
            try:
                job = enqueue_job(conn, task="queued", branch="feature/queued")
            finally:
                conn.close()
            advanced = False

            def advance(_interval: float) -> None:
                nonlocal advanced
                if advanced:
                    return
                advanced = True
                writer = connect(db)
                try:
                    mark_job(writer, job.id, status="validated", note="done")
                finally:
                    writer.close()

            out = io.StringIO()
            with (
                patch("mergetrain.commands.inspection.connect", wraps=connect) as observer_connect,
                patch("mergetrain.commands.inspection.time.sleep", side_effect=advance),
                redirect_stdout(out),
            ):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "events",
                        "--job",
                        str(job.id),
                        "--follow",
                        "--jsonl",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertEqual(observer_connect.call_count, 1)
            self.assertTrue(observer_connect.call_args.kwargs["read_only"])

    def test_logs_follow_reuses_one_read_only_connection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            logs = repo / ".mergetrain" / "logs"
            logs.mkdir(parents=True)
            log_path = logs / "job.log"
            log_path.write_text("running\n", encoding="utf-8")
            conn = connect(db)
            owner = f"owner:{os.getpid()}"
            try:
                queued = enqueue_job(conn, task="run", branch="feature/run")
                job = claim_next_job(conn, owner=owner)
                assert job is not None
                mark_job(
                    conn,
                    queued.id,
                    status="in_progress",
                    log_path=str(log_path),
                    expected_claim_token=job.claim_token,
                )
            finally:
                conn.close()

            out = io.StringIO()
            with (
                patch("mergetrain.commands.inspection.connect", wraps=connect) as observer_connect,
                patch(
                    "mergetrain.commands.inspection.time.sleep",
                    side_effect=[None, KeyboardInterrupt],
                ),
                redirect_stdout(out),
            ):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "logs",
                        str(queued.id),
                        "--follow",
                    ]
                )

            self.assertEqual(code, 130)
            self.assertEqual(observer_connect.call_count, 1)
            self.assertTrue(observer_connect.call_args.kwargs["read_only"])

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
