from __future__ import annotations

import unittest

from mergetrain.commands.inspection import _job_display_state
from mergetrain.models import Job
from mergetrain.snapshot import (
    PUBLIC_REASON_LIMIT,
    attention_reason_code,
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


if __name__ == "__main__":
    unittest.main()
