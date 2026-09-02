from __future__ import annotations

import unittest

from mergetrain.evidence import _not_landed_reason, run_attempts
from mergetrain.models import Job, RunEvent


def job(**kwargs) -> Job:
    kwargs.setdefault("id", 1)
    kwargs.setdefault("task", "task")
    kwargs.setdefault("branch", "agent/task")
    return Job(**kwargs)


def event(
    event_id: int,
    token: str,
    phase: str,
    state: str,
    message: str,
    *,
    job_id: int | None = None,
    detail: str = "",
) -> RunEvent:
    return RunEvent(
        id=event_id,
        claim_token=token,
        job_id=job_id,
        phase=phase,
        state=state,
        message=message,
        detail=detail,
        created_at=f"2026-07-22T00:00:{event_id:02d}Z",
    )


class NotLandedReasonTests(unittest.TestCase):
    CASES = [
        ({"status": "queued"}, "queued"),
        ({"status": "in_progress"}, "running"),
        ({"status": "validated"}, "awaiting_deploy"),
        ({"status": "needs_reconcile"}, "pending_reconcile"),
        ({"status": "canceled", "note": "superseded by train-new"}, "superseded"),
        ({"status": "canceled"}, "canceled"),
        (
            {
                "status": "blocked",
                "push_status": "failed",
                "conflict_with": "agent/other",
            },
            "push_rejected",
        ),
        ({"status": "blocked", "conflict_with": "agent/other"}, "semantic_conflict"),
        ({"status": "blocked", "note": "semantic conflict detected"}, "semantic_conflict"),
        ({"status": "blocked", "note": "merge conflict in app.py"}, "merge_conflict"),
        ({"status": "blocked", "note": "source HEAD changed"}, "source_identity_mismatch"),
        ({"status": "blocked", "note": "gate fingerprint changed"}, "validated_reuse_mismatch"),
        ({"status": "blocked", "note": "deploy_plan_changed"}, "deploy_authorization_changed"),
        ({"status": "blocked"}, "merge_blocked"),
        ({"status": "failed", "push_status": "failed"}, "push_failed"),
        ({"status": "failed", "note": "command timed out"}, "command_timeout"),
        ({"status": "failed", "note": "gate tests failed"}, "gate_failed"),
        ({"status": "failed"}, "runner_failed"),
        ({"status": "unknown"}, "unknown"),
    ]

    def test_reason_taxonomy_and_structured_precedence(self) -> None:
        for kwargs, expected in self.CASES:
            with self.subTest(expected=expected):
                self.assertEqual(_not_landed_reason([job(**kwargs)]), expected)

    def test_deployed_train_has_no_not_landed_reason(self) -> None:
        self.assertIsNone(_not_landed_reason([job(status="deployed")]))


class RunAttemptTests(unittest.TestCase):
    def test_reconstructs_failure_cancellation_incomplete_and_legacy_deploy(self) -> None:
        events = [
            event(
                1,
                "failed",
                "claiming",
                "active",
                "Validation runner claimed 1 job",
                detail="mode=validate",
            ),
            event(2, "failed", "failed", "error", "Job #1 failed", job_id=1),
            event(
                3,
                "canceled",
                "claiming",
                "active",
                "Validation runner claimed 1 job",
                detail="mode=validate",
            ),
            event(
                4,
                "canceled",
                "canceled",
                "warning",
                "Job #2 canceled",
                job_id=2,
            ),
            event(
                5,
                "incomplete",
                "claiming",
                "active",
                "Validation runner claimed 3 job(s)",
                detail="mode=validate",
            ),
            event(
                6,
                "legacy-deploy",
                "claiming",
                "active",
                "Deploy runner claimed 2 job(s)",
            ),
            event(7, "legacy-deploy", "pushing", "active", "Pushing refs"),
            event(
                8,
                "legacy-deploy",
                "complete",
                "warning",
                "Job #3 deployed; verify needs attention",
                job_id=3,
            ),
            event(
                9,
                "truncated",
                "blocked",
                "error",
                "Job #4 blocked",
                job_id=4,
            ),
        ]

        attempts = {attempt.claim_token: attempt for attempt in run_attempts(events)}
        self.assertEqual(attempts["failed"].outcome, "failed")
        self.assertEqual(attempts["canceled"].outcome, "canceled")
        self.assertEqual(attempts["incomplete"].outcome, "incomplete")
        self.assertEqual(attempts["incomplete"].job_count, 3)
        self.assertEqual(attempts["legacy-deploy"].mode, "deploy")
        self.assertEqual(attempts["legacy-deploy"].outcome, "succeeded")
        self.assertEqual(attempts["legacy-deploy"].job_count, 2)
        self.assertIsNone(attempts["truncated"].job_count)


if __name__ == "__main__":
    unittest.main()
