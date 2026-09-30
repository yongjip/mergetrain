from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mergetrain.commands.inspection import _job_display_state
from mergetrain.config import load_config
from mergetrain.models import Job
from mergetrain.persistence.claims import claim_all_queued
from mergetrain.persistence.connection import connect
from mergetrain.persistence.events import record_run_event
from mergetrain.persistence.jobs import enqueue_job, mark_job
from mergetrain.persistence.leases import release_runner_lock
from mergetrain.snapshot import (
    PUBLIC_REASON_LIMIT,
    attention_reason_code,
    build_repo_snapshot,
    next_action,
    plan_next_action,
    public_reason,
)

PAST = "2000-01-01T00:00:00Z"  # any past ISO timestamp -> the lease is expired
FUTURE = "2999-01-01T00:00:00Z"


class NextActionTests(unittest.TestCase):
    def test_public_reason_redacts_before_applying_the_length_limit(self) -> None:
        secret = "s" * (PUBLIC_REASON_LIMIT * 2)
        reason, truncated = public_reason(
            Job(
                id=1,
                task="a",
                branch="a",
                status="blocked",
                note=f"API_TOKEN={secret}",
            )
        )
        self.assertEqual(reason, "API_TOKEN=[redacted]")
        self.assertFalse(truncated)

        exact, exact_truncated = public_reason(
            Job(
                id=2,
                task="a",
                branch="a",
                status="blocked",
                note="x" * PUBLIC_REASON_LIMIT,
            )
        )
        self.assertEqual(len(exact or ""), PUBLIC_REASON_LIMIT)
        self.assertFalse(exact_truncated)

        long, long_truncated = public_reason(
            Job(
                id=3,
                task="a",
                branch="a",
                status="blocked",
                note="x" * (PUBLIC_REASON_LIMIT + 1),
            )
        )
        self.assertEqual(len(long or ""), PUBLIC_REASON_LIMIT)
        self.assertTrue(long_truncated)

    def test_count_only_verify_failure_never_targets_an_unrelated_job(self) -> None:
        blocked = Job(id=9, task="blocked", branch="b", status="blocked")
        plan = plan_next_action(
            {
                "lock": None,
                "counts": {"deployed_verify_failed": 1, "blocked": 1},
            },
            attention_jobs=[blocked],
        )
        self.assertEqual(plan.code, "resolve_failed_verification")
        self.assertIsNone(plan.target_job_id)
        self.assertIsNone(plan.command)
        self.assertIsNone(plan.reason_code)

    def test_job_projection_matrix_keeps_verification_failures_actionable(self) -> None:
        cases = [
            (Job(id=1, task="a", branch="a", status="queued"), "waiting", None),
            (Job(id=2, task="a", branch="a", status="in_progress"), "running", None),
            (Job(id=3, task="a", branch="a", status="validated"), "ready", None),
            (Job(id=4, task="a", branch="a", status="blocked"), "attention", "blocked"),
            (Job(id=5, task="a", branch="a", status="failed"), "attention", "failed"),
            (
                Job(
                    id=6,
                    task="a",
                    branch="a",
                    status="deployed",
                    push_status="succeeded",
                    verify_status="failed",
                ),
                "attention",
                "post_push_verification_failed",
            ),
            (
                Job(
                    id=7,
                    task="a",
                    branch="a",
                    status="deployed",
                    push_status="succeeded",
                    verify_status="unknown",
                ),
                "attention",
                "post_push_verification_unknown",
            ),
            (
                Job(
                    id=8,
                    task="a",
                    branch="a",
                    status="deployed",
                    push_status="succeeded",
                    verify_status="succeeded",
                ),
                "done",
                None,
            ),
            (Job(id=9, task="a", branch="a", status="canceled"), "done", None),
        ]
        for job, state, reason in cases:
            with self.subTest(status=job.status, verify_status=job.verify_status):
                self.assertEqual(_job_display_state(job), state)
                self.assertEqual(attention_reason_code(job), reason)

    def test_every_outcome(self) -> None:
        cases = [
            (
                {"lock": {"liveness": "alive", "expires_at": PAST}, "counts": {"in_progress": 1}},
                "unlock_wedged_runner",
            ),
            (
                {"lock": {"liveness": "alive", "expires_at": FUTURE}, "counts": {}},
                "wait_for_runner",
            ),
            (
                {
                    "lock": {"liveness": "alive", "expires_at": "not-a-timestamp"},
                    "counts": {"in_progress": 1},
                },
                "unlock_wedged_runner",
            ),
            ({"lock": {"liveness": "alive"}, "counts": {"in_progress": 1}}, "unlock_wedged_runner"),
            ({"lock": None, "counts": {"needs_reconcile": 1}}, "reconcile_pending_deploy"),
            ({"lock": None, "counts": {"in_progress_with_marker": 1}}, "reconcile_pending_deploy"),
            ({"lock": None, "counts": {"blocked_with_marker": 1}}, "reconcile_conflict_manual"),
            ({"lock": None, "counts": {"blocked": 1}}, "fix_blocked_job"),
            ({"lock": None, "counts": {"failed": 1}}, "fix_blocked_job"),
            (
                {"lock": None, "counts": {"deployed_verify_failed": 1}},
                "resolve_failed_verification",
            ),
            ({"lock": None, "counts": {"deployed_verify_unknown": 1}}, "verify_reconciled_deploy"),
            (
                {
                    "lock": None,
                    "counts": {},
                    "validated_trains": [{"train_id": "t1", "deploy_eligible": True}],
                },
                "deploy_when_approved",
            ),
            (
                {
                    "lock": None,
                    "counts": {},
                    "validated_trains": [{"train_id": None, "deploy_eligible": False}],
                },
                "cancel_and_reenqueue_legacy_validated_jobs",
            ),
            ({"lock": None, "counts": {"auto_queued": 1}}, "run_daemon_when_approved"),
            ({"lock": None, "counts": {"queued": 1}}, "validate_queued_jobs"),
            ({"lock": None, "counts": {}, "gc": {"worktree_candidates": ["wt"]}}, "gc_available"),
            ({"lock": None, "counts": {}}, "enqueue_clean_branch"),
            ({}, "enqueue_clean_branch"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(next_action(payload), expected)

    def test_branch_precedence(self) -> None:
        cases = [
            # blocked_with_marker beats fix_blocked_job
            (
                {"lock": None, "counts": {"blocked": 1, "blocked_with_marker": 1}},
                "reconcile_conflict_manual",
            ),
            # needs_reconcile beats a ready validated train
            (
                {
                    "lock": None,
                    "counts": {"needs_reconcile": 1},
                    "validated_trains": [{"deploy_eligible": True}],
                },
                "reconcile_pending_deploy",
            ),
            # a validated train with no deploy_eligible member -> re-enqueue legacy
            (
                {"lock": None, "counts": {}, "validated_trains": [{"deploy_eligible": False}]},
                "cancel_and_reenqueue_legacy_validated_jobs",
            ),
            # auto_queued beats plain queued
            ({"lock": None, "counts": {"queued": 2, "auto_queued": 1}}, "run_daemon_when_approved"),
            # a live, unexpired lock beats the reconcile signal
            (
                {
                    "lock": {"liveness": "alive", "expires_at": FUTURE},
                    "counts": {"needs_reconcile": 1},
                },
                "wait_for_runner",
            ),
            # a wedged (expired, still-alive-looking) runner with in-progress work
            (
                {
                    "lock": {"liveness": "alive", "expires_at": PAST},
                    "counts": {"in_progress": 1, "needs_reconcile": 1},
                },
                "unlock_wedged_runner",
            ),
            # the marker reconcile path is gated on liveness != "alive"
            (
                {
                    "lock": {"liveness": "alive", "expires_at": FUTURE},
                    "counts": {"in_progress_with_marker": 1},
                },
                "wait_for_runner",
            ),
            # expired+alive but in_progress == 0 and only a marker -> falls through
            (
                {
                    "lock": {"liveness": "alive", "expires_at": PAST},
                    "counts": {"in_progress": 0, "in_progress_with_marker": 1},
                },
                "enqueue_clean_branch",
            ),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(next_action(payload), expected)


class RepoSnapshotTests(unittest.TestCase):
    """The full per-repo read model that `hub status --json` embeds."""

    def make_config(self, root: Path):
        return load_config(repo=root, db_override=root / "queue.sqlite")

    def test_snapshot_points_too_new_config_at_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".mergetrain.yaml").write_text(
                "version: 999\nproject:\n  name: future\n", encoding="utf-8"
            )
            config = self.make_config(root)
            connect(config.state.db).close()
            payload = build_repo_snapshot(config, read_only=True)
            self.assertEqual(payload["next_action"], "upgrade_mergetrain")

    def test_snapshot_omits_the_runner_username_and_integration_worktree_paths(self) -> None:
        """#231: unlock events and command-failure notes leaked both."""

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            integration = config.state.worktree_root / "demo-mergetrain-7-0a1b2c3d"
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="gate", branch="codex/gate")
                mark_job(
                    conn,
                    job.id,
                    status="failed",
                    note=f"command failed (1) in {integration}: /bin/sh -c 'make test'",
                )
                # An unlock audit event recorded before owners were masked.
                record_run_event(
                    conn,
                    phase="unlock",
                    state="cleared",
                    message="cleared dead runner lock (alice:4242)",
                    detail=json.dumps({"owner": "alice:4242", "liveness": "dead"}),
                )
            finally:
                conn.close()

            payload = build_repo_snapshot(config, read_only=True)

            self.assertNotIn("alice", json.dumps(payload))
            note = next(item["note"] for item in payload["jobs"] if item["id"] == job.id)
            self.assertNotIn(str(config.state.worktree_root), note)
            self.assertIn(os.path.join("[worktrees]", "demo-mergetrain-7-0a1b2c3d"), note)
            unlock = next(item for item in payload["events"] if item["phase"] == "unlock")
            self.assertEqual(unlock["message"], "cleared dead runner lock (local:4242)")
            self.assertEqual(json.loads(unlock["detail"])["owner"], "local:4242")

    def test_snapshot_masks_local_paths_before_bounding_a_note(self) -> None:
        # The note was bounded first and its paths masked after, so a path cut
        # at the limit kept its prefix, home directory included. A command
        # that ran in the checkout itself named it with no mask at all.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            repo = str(config.repo)
            integration = config.state.worktree_root / "demo-mergetrain-7-0a1b2c3d"
            head = f"command failed (1) in {integration}: make test\n"
            # The next path starts under the checkout and crosses the limit.
            filler = "x" * (PUBLIC_REASON_LIMIT - len(head) - len(repo) // 2)
            report = f"{integration}/tests/test_app.py:12: AssertionError"
            conn = connect(config.state.db)
            try:
                gate = enqueue_job(conn, task="gate", branch="codex/gate")
                mark_job(conn, gate.id, status="failed", note=head + filler + report)
                fetch = enqueue_job(conn, task="fetch", branch="codex/fetch")
                mark_job(
                    conn,
                    fetch.id,
                    status="blocked",
                    note=f"command failed (128) in {repo}: git fetch origin",
                )
            finally:
                conn.close()

            payload = build_repo_snapshot(config, read_only=True)

            notes = {item["id"]: item for item in payload["jobs"]}
            self.assertNotIn(repo[: len(repo) // 2], notes[gate.id]["note"])
            self.assertTrue(notes[gate.id]["note"].endswith("tests/test_app.py:12: AssertionError"))
            self.assertFalse(notes[gate.id]["note_truncated"])
            self.assertEqual(
                notes[fetch.id]["note"], "command failed (128) in [repo]: git fetch origin"
            )

    def test_snapshot_is_live_and_omits_local_paths_and_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            try:
                enqueue_job(
                    conn,
                    task="snapshot",
                    branch="codex/snapshot",
                    worktree_path="/private/sensitive/worktree",
                )
                claimed = claim_all_queued(conn, owner=owner)
                record_run_event(
                    conn,
                    claim_token=claimed[0].claim_token,
                    job_id=claimed[0].id,
                    phase="assembling",
                    state="success",
                    message=f"Merged {claimed[0].branch}",
                )
                record_run_event(
                    conn,
                    claim_token=claimed[0].claim_token,
                    phase="gating",
                    state="active",
                    message="Running gate 1/1: diff-check",
                    detail="git diff --check origin/main..HEAD",
                )
            finally:
                conn.close()

            payload = build_repo_snapshot(config, read_only=True)
            self.assertEqual(payload["train"]["selection"], "running")
            self.assertEqual(payload["progress"]["phase"], "gating")
            self.assertEqual(payload["progress"]["completed_job_ids"], [claimed[0].id])
            self.assertNotIn("gating", payload["progress"]["completed_phases"])
            self.assertEqual(
                payload["progress"]["current_gate"],
                {
                    "index": 1,
                    "total": 1,
                    "name": "diff-check",
                    "state": "active",
                    "command": "git diff --check origin/main..HEAD",
                    "started_at": payload["progress"]["updated_at"],
                    "finished_at": "",
                    "duration_seconds": None,
                },
            )
            self.assertEqual(
                [gate["state"] for gate in payload["progress"]["gates"]],
                ["active"],
            )
            self.assertFalse(payload["project"]["reuse"]["enabled"])
            self.assertEqual(payload["project"]["reuse"]["max_age_minutes"], 60)
            self.assertEqual(payload["reuse"]["evaluation"], "not_evaluated")
            self.assertIsNone(payload["reuse"]["eligible"])
            self.assertFalse(
                payload["reuse"]["estimated_savings"]["authorizes_reuse"]
            )
            self.assertFalse(payload["eta"]["available"])
            self.assertEqual(payload["eta"]["coverage"], "none")
            self.assertEqual(payload["lock"]["owner"], f"local:{os.getpid()}")
            self.assertIn("heartbeat_at", payload["lock"])
            self.assertNotIn("worktree_path", payload["jobs"][0])
            self.assertNotIn("log_path", payload["jobs"][0])
            self.assertNotIn("claim_token", payload["events"][0])
            self.assertNotIn("runtime", payload)
            self.assertNotIn("terminology", payload["project"])
            self.assertEqual(payload["project"]["push_specs"], ["HEAD:main"])

            cleanup = connect(config.state.db)
            try:
                release_runner_lock(cleanup, owner=owner, token=claimed[0].claim_token)
            finally:
                cleanup.close()

    def test_snapshot_estimates_running_gate_eta_from_recent_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".mergetrain.yaml").write_text(
                """gates:
  - name: unit
    run: python -m unittest
""",
                encoding="utf-8",
            )
            config = self.make_config(root)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)

            def event(
                token: str,
                phase: str,
                state: str,
                message: str,
                created_at: str,
            ) -> None:
                recorded = record_run_event(
                    conn,
                    claim_token=token,
                    phase=phase,
                    state=state,
                    message=message,
                )
                conn.execute(
                    "UPDATE run_events SET created_at = ? WHERE id = ?",
                    (created_at, recorded.id),
                )

            try:
                event(
                    "history",
                    "fetching",
                    "active",
                    "Fetching main",
                    "2026-07-29T11:00:00Z",
                )
                event(
                    "history",
                    "fetching",
                    "success",
                    "Integration worktree prepared",
                    "2026-07-29T11:00:10Z",
                )
                event(
                    "history",
                    "assembling",
                    "active",
                    "Assembling train with 1 job(s)",
                    "2026-07-29T11:00:10Z",
                )
                event(
                    "history",
                    "assembling",
                    "success",
                    "Assembled 1 job(s)",
                    "2026-07-29T11:00:30Z",
                )
                event(
                    "history",
                    "gating",
                    "active",
                    "Running train gates",
                    "2026-07-29T11:00:30Z",
                )
                event(
                    "history",
                    "gating",
                    "active",
                    "Running gate 1/2: diff-check",
                    "2026-07-29T11:00:30Z",
                )
                event(
                    "history",
                    "gating",
                    "success",
                    "Passed gate 1/2: diff-check",
                    "2026-07-29T11:00:40Z",
                )
                event(
                    "history",
                    "gating",
                    "active",
                    "Running gate 2/2: unit",
                    "2026-07-29T11:00:40Z",
                )
                event(
                    "history",
                    "gating",
                    "success",
                    "Passed gate 2/2: unit",
                    "2026-07-29T11:01:10Z",
                )
                event(
                    "history",
                    "gating",
                    "success",
                    "All train gates passed",
                    "2026-07-29T11:01:10Z",
                )
                conn.commit()

                enqueue_job(conn, task="snapshot ETA", branch="codex/snapshot-eta")
                claimed = claim_all_queued(conn, owner=owner)
                token = claimed[0].claim_token
                event(
                    token,
                    "fetching",
                    "active",
                    "Fetching main",
                    "2026-07-29T12:00:00Z",
                )
                event(
                    token,
                    "fetching",
                    "success",
                    "Integration worktree prepared",
                    "2026-07-29T12:00:05Z",
                )
                event(
                    token,
                    "assembling",
                    "active",
                    "Assembling train with 1 job(s)",
                    "2026-07-29T12:00:05Z",
                )
                event(
                    token,
                    "assembling",
                    "success",
                    "Assembled 1 job(s)",
                    "2026-07-29T12:00:20Z",
                )
                event(
                    token,
                    "gating",
                    "active",
                    "Running train gates",
                    "2026-07-29T12:00:20Z",
                )
                event(
                    token,
                    "gating",
                    "active",
                    "Running gate 1/2: diff-check",
                    "2026-07-29T12:00:20Z",
                )
                event(
                    token,
                    "gating",
                    "success",
                    "Passed gate 1/2: diff-check",
                    "2026-07-29T12:00:30Z",
                )
                event(
                    token,
                    "gating",
                    "active",
                    "Running gate 2/2: unit",
                    "2026-07-29T12:00:30Z",
                )
                conn.commit()
            finally:
                conn.close()

            with patch(
                "mergetrain.snapshot.utc_now",
                return_value="2026-07-29T12:00:35Z",
            ):
                payload = build_repo_snapshot(config, read_only=True)

            self.assertTrue(payload["eta"]["available"])
            self.assertEqual(payload["eta"]["coverage"], "complete")
            self.assertEqual(payload["eta"]["sample_count"], 1)
            self.assertEqual(payload["eta"]["estimated_remaining_seconds"], 25.0)
            self.assertEqual(payload["eta"]["expected_at"], "2026-07-29T12:01:00Z")
            self.assertEqual(
                [
                    (
                        gate["name"],
                        gate["median_seconds"],
                        gate["remaining_seconds"],
                    )
                    for gate in payload["eta"]["gates"]
                ],
                [("diff-check", 10.0, 0.0), ("unit", 30.0, 25.0)],
            )
            self.assertEqual(
                {
                    phase["name"]: phase["median_seconds"]
                    for phase in payload["eta"]["phases"]
                }["gating"],
                40.0,
            )

            cleanup = connect(config.state.db)
            try:
                release_runner_lock(cleanup, owner=owner, token=token)
            finally:
                cleanup.close()

    def test_snapshot_exposes_push_targets_without_terminology_surface(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".mergetrain.yaml").write_text(
                """git:
  remote: upstream
  integration_branch: main
  push_refs:
    - main
    - release
""",
                encoding="utf-8",
            )
            config = self.make_config(root)
            connect(config.state.db).close()
            payload = build_repo_snapshot(config, read_only=True)
            self.assertNotIn("terminology", payload["project"])
            self.assertEqual(payload["project"]["remote"], "upstream")
            self.assertEqual(payload["project"]["push_specs"], ["HEAD:main", "HEAD:release"])

    def test_snapshot_preserves_skipped_gate_after_train_gates_pass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".mergetrain.yaml").write_text(
                """gates:
  - name: docs
    run: make docs
    paths:
      - docs/**
""",
                encoding="utf-8",
            )
            config = self.make_config(root)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            try:
                enqueue_job(conn, task="snapshot", branch="codex/snapshot")
                claimed = claim_all_queued(conn, owner=owner)
                token = claimed[0].claim_token
                record_run_event(
                    conn,
                    claim_token=token,
                    phase="gating",
                    state="skipped",
                    message="Skipped gate 2/2: docs",
                    detail="no changed paths matched configured paths",
                )
                record_run_event(
                    conn,
                    claim_token=token,
                    phase="gating",
                    state="success",
                    message="All train gates passed",
                )
            finally:
                conn.close()

            payload = build_repo_snapshot(config, read_only=True)
            self.assertEqual(
                [gate["state"] for gate in payload["progress"]["gates"]],
                ["success", "skipped"],
            )
            self.assertIsNone(payload["progress"]["current_gate"])

            cleanup = connect(config.state.db)
            try:
                release_runner_lock(cleanup, owner=owner, token=token)
            finally:
                cleanup.close()

    def test_snapshot_exposes_deployed_verification_attention(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="deploy", branch="codex/deploy")
                mark_job(
                    conn,
                    job.id,
                    status="deployed",
                    push_status="succeeded",
                    verify_status="failed",
                    note="post-push verify warning: health check failed",
                )
                record_run_event(
                    conn,
                    job_id=job.id,
                    phase="complete",
                    state="warning",
                    message=f"Job #{job.id} deployed; verification needs attention",
                    detail="post-push verify warning: health check failed",
                )
            finally:
                conn.close()

            payload = build_repo_snapshot(config, read_only=True)
            self.assertEqual(payload["jobs"][0]["status"], "deployed")
            self.assertEqual(payload["jobs"][0]["push_status"], "succeeded")
            self.assertEqual(payload["jobs"][0]["verify_status"], "failed")
            self.assertEqual(payload["events"][-1]["state"], "warning")

    def test_snapshot_removes_worktree_path_embedded_in_job_note(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            sensitive = "/private/sensitive/integration-worktree"
            conn = connect(config.state.db)
            try:
                job = enqueue_job(
                    conn,
                    task="failed gate",
                    branch="codex/failure",
                    worktree_path=sensitive,
                )
                mark_job(
                    conn,
                    job.id,
                    status="failed",
                    note=f"command failed (1) in {sensitive}: make test",
                )
            finally:
                conn.close()

            payload = build_repo_snapshot(config, read_only=True)
            note = payload["jobs"][0]["note"]
            self.assertNotIn(sensitive, note)
            self.assertIn("[worktree]", note)


if __name__ == "__main__":
    unittest.main()
