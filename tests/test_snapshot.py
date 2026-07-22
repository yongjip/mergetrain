from __future__ import annotations

import unittest

from mergetrain.snapshot import next_action

PAST = "2000-01-01T00:00:00Z"  # any past ISO timestamp -> the lease is expired
FUTURE = "2999-01-01T00:00:00Z"


class NextActionTests(unittest.TestCase):
    def test_every_outcome(self) -> None:
        cases = [
            ({"lock": {"liveness": "alive", "expires_at": PAST}, "counts": {"in_progress": 1}},
             "unlock_wedged_runner"),
            ({"lock": {"liveness": "alive", "expires_at": FUTURE}, "counts": {}},
             "wait_for_runner"),
            ({"lock": {"liveness": "alive", "expires_at": "not-a-timestamp"},
              "counts": {"in_progress": 1}},
             "unlock_wedged_runner"),
            ({"lock": {"liveness": "alive"}, "counts": {"in_progress": 1}},
             "unlock_wedged_runner"),
            ({"lock": None, "counts": {"needs_reconcile": 1}}, "reconcile_pending_deploy"),
            ({"lock": None, "counts": {"in_progress_with_marker": 1}}, "reconcile_pending_deploy"),
            ({"lock": None, "counts": {"blocked_with_marker": 1}}, "reconcile_conflict_manual"),
            ({"lock": None, "counts": {"blocked": 1}}, "fix_blocked_job"),
            ({"lock": None, "counts": {"failed": 1}}, "fix_blocked_job"),
            ({"lock": None, "counts": {"deployed_verify_unknown": 1}}, "verify_reconciled_deploy"),
            ({"lock": None, "counts": {},
              "validated_trains": [{"train_id": "t1", "deploy_eligible": True}]},
             "deploy_validated_train_when_approved"),
            ({"lock": None, "counts": {},
              "validated_trains": [{"train_id": None, "deploy_eligible": False}]},
             "cancel_and_reenqueue_legacy_validated_jobs"),
            ({"lock": None, "counts": {"auto_queued": 1}},
             "run_daemon_or_run_batch_deploy_when_approved"),
            ({"lock": None, "counts": {"queued": 1}}, "run_batch_validate"),
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
            ({"lock": None, "counts": {"blocked": 1, "blocked_with_marker": 1}},
             "reconcile_conflict_manual"),
            # needs_reconcile beats a ready validated train
            ({"lock": None, "counts": {"needs_reconcile": 1},
              "validated_trains": [{"deploy_eligible": True}]},
             "reconcile_pending_deploy"),
            # a validated train with no deploy_eligible member -> re-enqueue legacy
            ({"lock": None, "counts": {}, "validated_trains": [{"deploy_eligible": False}]},
             "cancel_and_reenqueue_legacy_validated_jobs"),
            # auto_queued beats plain queued
            ({"lock": None, "counts": {"queued": 2, "auto_queued": 1}},
             "run_daemon_or_run_batch_deploy_when_approved"),
            # a live, unexpired lock beats the reconcile signal
            ({"lock": {"liveness": "alive", "expires_at": FUTURE},
              "counts": {"needs_reconcile": 1}},
             "wait_for_runner"),
            # a wedged (expired, still-alive-looking) runner with in-progress work
            ({"lock": {"liveness": "alive", "expires_at": PAST},
              "counts": {"in_progress": 1, "needs_reconcile": 1}},
             "unlock_wedged_runner"),
            # the marker reconcile path is gated on liveness != "alive"
            ({"lock": {"liveness": "alive", "expires_at": FUTURE},
              "counts": {"in_progress_with_marker": 1}},
             "wait_for_runner"),
            # expired+alive but in_progress == 0 and only a marker -> falls through
            ({"lock": {"liveness": "alive", "expires_at": PAST},
              "counts": {"in_progress": 0, "in_progress_with_marker": 1}},
             "enqueue_clean_branch"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(next_action(payload), expected)


if __name__ == "__main__":
    unittest.main()
