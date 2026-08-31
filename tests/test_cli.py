from __future__ import annotations

import io
import json
import os
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
from mergetrain.config import load_config, render_default_config
from mergetrain.contract import CONTRACT_VERSION
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
                code = main(
                    ["--repo", str(repo), "daemon", "--once", "--notify"]
                )

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
                code = main(
                    ["--repo", str(repo), "daemon", "--once", "--notify"]
                )

            self.assertEqual(code, 0)
            self.assertIn("no headless notification backend", err.getvalue())
            self.assertIsNotNone(loop.call_args.kwargs["notifier"])

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
            with patch(
                "mergetrain.hub_daemon.hub_daemon_loop", side_effect=run_loop
            ), redirect_stdout(out), redirect_stderr(err):
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
        with patch("mergetrain.cli.cmd_status", side_effect=KeyboardInterrupt), redirect_stdout(out):
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
        with patch("mergetrain.runtime.runtime_provenance", return_value=runtime), redirect_stdout(out):
            code = main(["version", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["version"], __version__)
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
        with patch("mergetrain.runtime.runtime_provenance", return_value=runtime), redirect_stdout(out):
                code = main(["--repo", str(repo), "doctor", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["runtime"], runtime)

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
                code = main(["--repo", str(repo), "doctor", "--json"])
            payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["git"]["remote_url"],
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
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("drift"), encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", ".mergetrain.yaml"], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "commit", "-qm", "add config"], cwd=repo, check=True
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
                code = main(["--repo", str(repo), "doctor", "--json"])
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["config_drift"]["state"], "in_sync")
        self.assertTrue(payload["config_drift"]["comparable"])
        self.assertTrue(payload["config_drift"]["matches"])
        self.assertEqual(payload["recommendations"], [])

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
            subprocess.run(
                ["git", "add", ".mergetrain.yaml"], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "commit", "-qm", "add config"], cwd=repo, check=True
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
            subprocess.run(
                ["git", "config", "core.autocrlf", "true"],
                cwd=repo,
                check=True,
            )
            config_path.write_bytes(
                config_text.replace("\n", "\r\n").encode("utf-8")
            )

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "doctor", "--json"])
            payload = json.loads(out.getvalue())

        drift = payload["config_drift"]
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
                "gates:\n"
                "  - name: diff-check\n"
                "    run: git diff --check ${integration_ref}..HEAD",
            )
            (repo / ".mergetrain.yaml").write_text(
                config_text,
                encoding="utf-8",
            )
            subprocess.run(["git", "add", ".mergetrain.yaml"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "add config"], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                cwd=repo,
                check=True,
            )

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "doctor", "--json"])
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        recommendation = payload["recommendations"][0]
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
            config_path.write_text(
                render_default_config("drift"), encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", ".mergetrain.yaml"], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "commit", "-qm", "add config"], cwd=repo, check=True
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
            config_path.write_text(
                render_default_config("operator-copy"), encoding="utf-8"
            )

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "doctor", "--json"])
            payload = json.loads(out.getvalue())

        drift = payload["config_drift"]
        self.assertEqual(code, 0)
        self.assertEqual(drift["state"], "drifted")
        self.assertTrue(drift["comparable"])
        self.assertFalse(drift["matches"])
        self.assertNotEqual(
            drift["local"]["blob_sha"],
            drift["integration"]["blob_sha"],
        )
        self.assertEqual(
            payload["recommendations"][0]["code"],
            "operator_config_drift",
        )
        self.assertEqual(payload["next_action"], "enqueue_clean_branch")

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
                code = main(["--repo", str(nested), "doctor", "--json"])
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(payload["config_drift"]["state"], "in_sync")
        self.assertEqual(
            payload["config_drift"]["local"]["path"],
            "services/api/.mergetrain.yaml",
        )

    def test_doctor_does_not_invent_drift_without_an_integration_ref(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("drift"), encoding="utf-8"
            )

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "doctor", "--json"])
            payload = json.loads(out.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(
            payload["config_drift"]["state"],
            "integration_ref_missing",
        )
        self.assertFalse(payload["config_drift"]["comparable"])
        self.assertIsNone(payload["config_drift"]["matches"])
        self.assertEqual(payload["recommendations"], [])

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

    def test_contract1_envelope_ok_is_uniform_and_health_is_separate(self) -> None:
        # A valid but unconfigured repo: the command ran (ok:true), and the
        # repo-health verdict lives in its own `health` field, not in `ok`.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "doctor", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertIn("health", payload)
            self.assertIn("next_action", payload)

    def test_contract1_status_carries_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "status", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            # The two mandated reads (status/doctor) are now symmetric.
            self.assertIn("next_action", payload)

    def test_next_action_points_at_init_before_any_queue_advice(self) -> None:
        # An unconfigured repo used to be told to enqueue a branch, which
        # enqueue then refused with config_error. next_action is the signal
        # agents are told to act on, so it has to name the actual blocker.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for command in (["doctor", "--json"], ["status", "--json"]):
                with self.subTest(command=command[0]):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        code = main(["--repo", str(repo), *command])
                    payload = json.loads(out.getvalue())
                    self.assertEqual(code, 0)
                    self.assertEqual(payload["next_action"], "initialize_config")

            main(["--repo", str(repo), "init", "--project", "demo", "--write"])
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--repo", str(repo), "doctor", "--json"])
            self.assertEqual(
                json.loads(out.getvalue())["next_action"], "enqueue_clean_branch"
            )

    def test_run_next_deploy_refuses_while_a_validated_train_is_pending(self) -> None:
        # run-next claims the next *queued* job, so it never picks up a pending
        # train's `validated` members. Left unguarded it pushes a different
        # commit and moves the integration ref out from under the exact train a
        # human approved, silently invalidating that validation.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            db = repo / "queue.sqlite"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("guard"), encoding="utf-8"
            )
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
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "validated_train_pending")
            self.assertEqual(payload["pending_train_ids"], ["t-guard"])
            self.assertEqual(
                payload["next_action"], "deploy_validated_train_when_approved"
            )
            self.assertIn("run-batch --deploy --train-id t-guard", payload["error"]["message"])

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

            for command in (["doctor", "--json"], ["status", "--json"]):
                with self.subTest(command=command[0]):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        code = main(["--repo", str(repo), "--db", str(db), *command])
                    payload = json.loads(out.getvalue())
                    self.assertEqual(code, 0)
                    self.assertEqual(payload["next_action"], "recover_stranded_claim")

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
                    self.assertIn(
                        "--limit must be 1 or greater", payload["error"]["message"]
                    )

    def test_contract1_version_stamped_top_level_not_nested(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("demo"), encoding="utf-8"
            )
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
            self.assertNotIn("contract_version", payload["jobs"][0])

    def test_contract1_agent_contract_carries_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "agent-contract", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["name"], "mergetrain agent contract")

    def test_duplicate_branch_surfaces_typed_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("demo"), encoding="utf-8"
            )
            base = ["--repo", str(repo), "enqueue", "--task", "a",
                    "--branch", "feature/a", "--no-ready-check"]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(base), 0)
            out = io.StringIO()
            with redirect_stdout(out):
                code = main([*base, "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 1)
            # Agents branch on error.code, not the free-text message.
            self.assertEqual(payload["error"]["code"], "duplicate_active_branch")

    def test_retry_captures_fresh_shas_and_inherits_job_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"], cwd=repo, check=True
            )
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("demo"), encoding="utf-8"
            )
            (repo / "app.txt").write_text("fixed\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "fixed"], cwd=repo, check=True)
            subprocess.run(
                ["git", "branch", "-M", "feature/retry"], cwd=repo, check=True
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
                )
                mark_job(conn, original.id, status="failed", note="gate failed")
            finally:
                conn.close()

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(
                    ["--repo", str(repo), "retry", str(original.id), "--json"]
                )
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
            subprocess.run(
                ["git", "commit", "-qm", "base"], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "branch", "-M", "main"], cwd=repo, check=True
            )
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
        self.assertEqual(payload["next_action"], "run_batch_validate")

    def test_retry_rebase_error_does_not_dismiss_original_job(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"], cwd=repo, check=True
            )
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("demo"), encoding="utf-8"
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            subprocess.run(
                ["git", "branch", "-M", "feature/retry"], cwd=repo, check=True
            )
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
            with patch("mergetrain.commands.queue.run_command", side_effect=failure), redirect_stdout(out):
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
                job_count = conn.execute(
                    "SELECT COUNT(*) FROM deploy_queue"
                ).fetchone()[0]
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
            code, payload = run(["run-batch", "--validate-only"])
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "config_error")

            # Recovery stays permissive — a rollback must not lock it out.
            code, payload = run(["reconcile"])
            self.assertEqual(payload.get("ok"), True)

            # doctor runs and points at the fix.
            code, payload = run(["doctor"])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["next_action"], "upgrade_mergetrain")
            self.assertEqual(payload["config_version_supported"], 2)

            # status is the other mandated agent read and must give the same
            # fail-closed direction as doctor.
            code, payload = run(["status"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["next_action"], "upgrade_mergetrain")

    def test_dismiss_all_reaches_blocked_jobs_older_than_display_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("demo"), encoding="utf-8"
            )
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

            code, payload = run(["run-batch", "--validate-only"])
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "config_error")
            self.assertIn("init", payload["error"]["message"])

            # Enqueue is deploy-capable too — fail closed before any git checks.
            code, payload = run(
                ["enqueue", "--task", "a", "--branch", "feature/a", "--no-ready-check"]
            )
            self.assertEqual(code, 1)
            self.assertEqual(payload["error"]["code"], "config_error")

            # Recovery and reads stay permissive — a missing config must not
            # lock the operator out of reconcile/doctor.
            code, payload = run(["reconcile"])
            self.assertEqual(payload.get("ok"), True)
            code, payload = run(["doctor"])
            self.assertTrue(payload["ok"])

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

    def test_agent_contract_json(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["agent-contract", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertIn("rules", payload)
        self.assertEqual(payload["boundary"]["daemon_processes_only"], "jobs enqueued with --auto")
        self.assertIn("exact validated train", payload["boundary"]["validated_train_deploy"])
        self.assertIn("then stop", " ".join(payload["rules"]))
        self.assertIn(
            "explicit user/operator approval",
            payload["boundary"]["deploy_requires"],
        )
        self.assertIn("disabled by default", payload["boundary"]["validated_gate_reuse"])
        self.assertIn("read-only", payload["boundary"]["progress_observation"])
        self.assertNotIn("human_vocabulary", payload)

    def test_deploy_preview_lists_exact_atomic_push_refspecs(self) -> None:
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
                "mergetrain.commands.deploy.GitRunner.preview_validated_reuse",
                return_value=decision,
            ), redirect_stdout(out):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(db),
                        "run-batch",
                        "--deploy",
                        "--preview",
                        "--json",
                    ]
                )
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["mode"], "deploy")
            self.assertNotIn("terminology", payload)
            self.assertEqual(payload["push_plan"]["remote"], "upstream")
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
            self.assertEqual(
                payload["reuse"]["estimated_savings"]["mode"], "unavailable"
            )
            self.assertFalse(
                payload["reuse"]["estimated_savings"]["authorizes_reuse"]
            )

    def test_removed_deploy_aliases_are_rejected(self) -> None:
        for alias in ("--integrate", "--push"):
            with self.subTest(alias=alias), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(["run-batch", alias, "--json"])
                self.assertEqual(raised.exception.code, 2)

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
            agent_contract = (repo / "AGENTS.mergetrain.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("A task agent stops after enqueueing", agent_contract)
            self.assertIn("separate explicit user/operator approval", agent_contract)

    def test_init_write_preflights_all_conflicts_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            existing = repo / "AGENTS.mergetrain.md"
            existing.write_text("keep me\n", encoding="utf-8")

            with patch("sys.stderr", io.StringIO()):
                code = main(
                    ["--repo", str(repo), "init", "--project", "demo", "--write"]
                )

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
            with patch(
                "mergetrain.commands.inspection.time.sleep",
                side_effect=KeyboardInterrupt,
            ), redirect_stdout(interrupted):
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
            with patch(
                "mergetrain.commands.inspection.connect", wraps=connect
            ) as observer_connect, patch(
                "mergetrain.commands.inspection.time.sleep", side_effect=advance
            ), redirect_stdout(out):
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
            with patch(
                "mergetrain.commands.inspection.connect", wraps=connect
            ) as observer_connect, patch(
                "mergetrain.commands.inspection.time.sleep",
                side_effect=[None, KeyboardInterrupt],
            ), redirect_stdout(out):
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
