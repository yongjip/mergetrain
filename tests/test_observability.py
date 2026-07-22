from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mergetrain.config import load_config
from mergetrain.models import Job, RunnerLock
from mergetrain.observability import (
    _lease_context,
    history_payload,
    job_outcome,
    normalize_since,
    stats_payload,
    stream_terminal,
    train_outcome,
)
from mergetrain.store import connect, enqueue_job


def job(**kwargs) -> Job:
    kwargs.setdefault("id", 1)
    kwargs.setdefault("task", "t")
    kwargs.setdefault("branch", "agent/a")
    return Job(**kwargs)


class JobOutcomeTests(unittest.TestCase):
    # (Job kwargs) -> (category, severity). push_status is checked BEFORE any
    # note substring for blocked/failed — that ordering is the trap being pinned.
    CASES = [
        ({"status": "deployed"}, "deployed", "success"),
        ({"status": "deployed", "verify_status": "failed"}, "post_push_verification_failed", "warning"),
        ({"status": "validated"}, "validated", "success"),
        ({"status": "canceled"}, "canceled", "failure"),
        ({"status": "blocked", "push_status": "failed", "note": "remote rejected the update"}, "push_rejected", "failure"),
        # push_status wins over a "conflict" note (the ordering trap):
        ({"status": "blocked", "push_status": "failed", "note": "merge conflict in app.txt"}, "push_rejected", "failure"),
        ({"status": "blocked", "note": "merge conflict in app.txt"}, "merge_conflict", "failure"),
        ({"status": "blocked", "note": "source HEAD changed under the runner"}, "source_identity_mismatch", "failure"),
        ({"status": "blocked", "note": "validated reuse mismatch"}, "validated_reuse_mismatch", "failure"),
        ({"status": "blocked", "note": "gate fingerprint changed"}, "validated_reuse_mismatch", "failure"),
        ({"status": "blocked"}, "merge_blocked", "failure"),  # empty note falls through
        ({"status": "failed", "push_status": "failed", "note": "remote rejected the update"}, "push_failed", "failure"),
        ({"status": "failed", "note": "the command timed out"}, "command_timeout", "failure"),
        ({"status": "failed", "note": "gate 'tests' failed: exit 1"}, "gate_failed", "failure"),
        ({"status": "failed"}, "runner_failed", "failure"),  # empty note falls through
        ({"status": "in_progress"}, "running", "pending"),
        ({"status": "queued"}, "queued", "pending"),
        ({"status": "needs_reconcile"}, "needs_reconcile", "pending"),
    ]

    def test_categories_and_severities(self) -> None:
        for kwargs, category, severity in self.CASES:
            with self.subTest(**kwargs):
                out = job_outcome(job(**kwargs))
                self.assertEqual(out["category"], category)
                self.assertEqual(out["severity"], severity)

    def test_failure_and_warning_projections(self) -> None:
        failure = job_outcome(job(status="failed", note="gate 'x' failed"))
        self.assertEqual(failure["failure_category"], "gate_failed")
        self.assertEqual(failure["warning_categories"], [])
        warning = job_outcome(job(status="deployed", verify_status="failed"))
        self.assertIsNone(warning["failure_category"])
        self.assertEqual(warning["warning_categories"], ["post_push_verification_failed"])
        secret = job_outcome(job(status="failed", note="API_TOKEN=do-not-leak"))
        self.assertNotIn("do-not-leak", secret["message"])


class TrainOutcomeTests(unittest.TestCase):
    def test_severity_precedence(self) -> None:
        cases = [
            # failure beats warning
            ([job(id=1, branch="a", status="failed"),
              job(id=2, branch="b", status="deployed", verify_status="failed")],
             "failure", "train_failed"),
            # warning beats success
            ([job(id=1, branch="a", status="deployed", verify_status="failed"),
              job(id=2, branch="b", status="validated")],
             "warning", "train_completed_with_warnings"),
            # clean success requires every job validated/deployed
            ([job(id=1, branch="a", status="validated"),
              job(id=2, branch="b", status="deployed")],
             "success", "train_completed"),
            ([job(id=1, branch="a", status="queued")], "pending", "train_pending"),
            ([], "pending", "train_pending"),
        ]
        for jobs, severity, category in cases:
            with self.subTest(category=category):
                out = train_outcome(jobs)
                self.assertEqual(out["severity"], severity)
                self.assertEqual(out["category"], category)


class StreamTerminalTests(unittest.TestCase):
    def _live_lock(self) -> RunnerLock:
        return RunnerLock(
            name="runner", owner="me", token="tok", liveness="live",
            heartbeat_at="2026-01-01T00:00:00Z", expires_at="2026-01-01T00:05:00Z",
        )

    def _running(self, token: str = "tok", **kw) -> Job:
        return job(status="in_progress", claim_token=token, **kw)

    def test_open_stream_returns_none(self) -> None:
        self.assertIsNone(stream_terminal([], self._live_lock()))
        # a running job under a matching live lease keeps the stream open
        self.assertIsNone(stream_terminal([self._running()], self._live_lock()))
        # a queued (not yet running) job keeps it open too
        self.assertIsNone(stream_terminal([job(status="queued")], self._live_lock()))

    def test_lost_lease_variants(self) -> None:
        lock = self._live_lock()
        cases = [
            ([self._running()], None),                                   # no lock
            ([self._running(token="other")], lock),                      # token mismatch
            ([self._running()], RunnerLock(name="r", owner="me", token="tok", liveness="dead")),  # dead
            ([self._running(id=1, branch="a", token="t1"),
              self._running(id=2, branch="b", token="t2")], lock),       # multi-token
            ([self._running(token="")], lock),                           # empty claim token
        ]
        for jobs, given_lock in cases:
            with self.subTest(n=len(jobs)):
                result = stream_terminal(jobs, given_lock)
                self.assertIsNotNone(result)
                self.assertEqual((result["reason"], result["exit_code"]), ("lost_lease", 1))

    def test_terminal_reasons(self) -> None:
        cases = [
            ([job(status="needs_reconcile")], "needs_reconcile", 1),
            ([job(id=1, branch="a", status="needs_reconcile"),
              job(id=2, branch="b", status="validated")], "needs_reconcile", 1),
            ([job(id=1, branch="a", status="needs_reconcile"),
              job(id=2, branch="b", status="failed")], "needs_reconcile", 1),
            ([job(status="failed")], "failure", 1),
            ([job(status="blocked")], "failure", 1),
            ([job(status="canceled")], "canceled", 1),
            ([job(id=1, branch="a", status="validated"),
              job(id=2, branch="b", status="deployed")], "success", 0),
        ]
        for jobs, reason, code in cases:
            with self.subTest(reason=reason):
                result = stream_terminal(jobs, self._live_lock())
                self.assertEqual((result["reason"], result["exit_code"]), (reason, code))


class LeaseContextTests(unittest.TestCase):
    def test_live_lost_and_inactive(self) -> None:
        live = RunnerLock(name="r", owner="me", token="tok", liveness="live",
                          heartbeat_at="hb", expires_at="exp")
        running = job(status="in_progress", claim_token="tok")
        self.assertEqual(
            _lease_context(running, live),
            {"heartbeat_at": "hb", "expires_at": "exp", "liveness": "live", "lost": False},
        )
        # a running job whose lease does not match is "lost" (blanked heartbeat)
        mismatch = _lease_context(running, RunnerLock(name="r", owner="me", token="other", liveness="live"))
        self.assertEqual((mismatch["liveness"], mismatch["lost"]), ("lost", True))
        self.assertEqual(mismatch["heartbeat_at"], "")
        self.assertEqual(_lease_context(running, None)["liveness"], "lost")
        # a non-running job is "inactive", never "lost"
        inactive = _lease_context(job(status="queued"), live)
        self.assertEqual((inactive["liveness"], inactive["lost"]), ("inactive", False))


class HistoryStatsTests(unittest.TestCase):
    def test_groups_complete_trains_and_aggregates_retained_gate_timing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = load_config(repo=root, db_override=root / "queue.sqlite")
            conn = connect(config.state.db)
            try:
                failed = enqueue_job(conn, task="old", branch="agent/old")
                first = enqueue_job(conn, task="a", branch="agent/a")
                second = enqueue_job(conn, task="b", branch="agent/b")
                conn.execute(
                    "UPDATE deploy_queue SET status='failed', "
                    "requested_at='2026-07-22T00:03:30Z', "
                    "started_at='2026-07-22T00:04:00Z', "
                    "finished_at='2026-07-22T00:05:00Z' WHERE id=?",
                    (failed.id,),
                )
                conn.execute(
                    "UPDATE deploy_queue SET status='deployed', train_id='train-1', "
                    "train_size=2, requested_at='2026-07-22T00:00:00Z', "
                    "started_at='2026-07-22T00:01:00Z', "
                    "finished_at='2026-07-22T00:03:00Z', "
                    "push_status='succeeded', verify_status='succeeded' "
                    "WHERE id IN (?, ?)",
                    (first.id, second.id),
                )
                conn.executemany(
                    "INSERT INTO run_events "
                    "(claim_token, phase, state, message, detail, created_at) "
                    "VALUES ('train-token', 'gating', ?, ?, '', ?)",
                    [
                        (
                            "active",
                            "Running gate 1/1: tests",
                            "2026-07-22T00:01:10Z",
                        ),
                        (
                            "success",
                            "Passed gate 1/1: tests",
                            "2026-07-22T00:01:20Z",
                        ),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            history = history_payload(config, limit=1)
            self.assertEqual(len(history["items"]), 1)
            train = history["items"][0]
            self.assertEqual(train["train_id"], "train-1")
            self.assertEqual(len(train["jobs"]), 2)
            self.assertEqual(train["duration_seconds"], 120.0)
            self.assertEqual(train["queue_seconds"], 60.0)
            self.assertEqual(train["gates"][0]["elapsed_seconds"], 10.0)

            stats = stats_payload(config)
            self.assertEqual(stats["trains"]["total"], 2)
            self.assertEqual(stats["trains"]["landed"], 1)
            self.assertEqual(stats["trains"]["failed"], 1)
            self.assertEqual(stats["trains"]["land_rate"], 0.5)
            self.assertEqual(stats["jobs"]["total"], 3)
            self.assertEqual(stats["duration_seconds"], {"median": 90.0, "p95": 120.0})
            self.assertEqual(stats["average_queue_seconds"], 45.0)
            self.assertEqual(stats["gates"][0]["name"], "tests")
            self.assertEqual(stats["gates"][0]["median_seconds"], 10.0)
            empty = stats_payload(config, since="2026-07-23T00:00:00Z")
            self.assertEqual(empty["trains"]["total"], 0)
            self.assertIsNone(empty["trains"]["land_rate"])

    def test_since_normalization_rejects_invalid_timestamp(self) -> None:
        self.assertEqual(
            normalize_since("2026-07-22T09:00:00+09:00"),
            "2026-07-22T00:00:00Z",
        )
        with self.assertRaisesRegex(ValueError, "ISO-8601"):
            normalize_since("yesterday")


if __name__ == "__main__":
    unittest.main()
