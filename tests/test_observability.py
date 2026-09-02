from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mergetrain.config import load_config
from mergetrain.models import Job, RunEvent, RunnerLock
from mergetrain.observability import (
    _lease_context,
    _stats_recommendations,
    event_record,
    gate_details,
    history_payload,
    job_outcome,
    normalize_since,
    stats_payload,
    stream_terminal,
    train_outcome,
)
from mergetrain.store import (
    connect,
    enqueue_job,
    finish_recovery_operation,
    list_recovery_operation_events,
    start_recovery_operation,
)


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
        ({"status": "blocked", "note": "approval_destination_changed"}, "deploy_authorization_changed", "failure"),
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

    def test_skipped_gate_event_has_stable_gate_details(self) -> None:
        event = RunEvent(
            id=1,
            phase="gating",
            state="skipped",
            message="Skipped gate 2/3: docs",
            detail="no changed paths matched configured paths",
            created_at="2026-07-22T00:00:00Z",
        )
        self.assertEqual(
            gate_details(event),
            {
                "index": 2,
                "total": 3,
                "name": "docs",
                "state": "skipped",
            },
        )


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
            self.assertEqual(train["gates"][0]["duration_seconds"], 10.0)

            stats = stats_payload(config)
            self.assertEqual(stats["trains"]["total"], 2)
            self.assertEqual(stats["trains"]["landed"], 1)
            self.assertEqual(stats["trains"]["failed"], 1)
            self.assertEqual(stats["trains"]["finished"], 2)
            self.assertEqual(stats["trains"]["land_rate"], 0.5)
            self.assertEqual(stats["jobs"]["total"], 3)
            self.assertEqual(stats["median_duration_seconds"], 90.0)
            self.assertEqual(stats["p95_duration_seconds"], 120.0)
            self.assertEqual(stats["average_queue_seconds"], 45.0)
            self.assertEqual(stats["gates"][0]["name"], "tests")
            self.assertEqual(stats["gates"][0]["median_seconds"], 10.0)
            self.assertEqual(stats["gates"][0]["timed_runs"], 1)
            self.assertEqual(
                stats["latency"]["coverage"]["observed_runs"], 1
            )
            self.assertEqual(stats["recommendations"], [])
            empty = stats_payload(config, since="2026-07-23T00:00:00Z")
            self.assertEqual(empty["trains"]["total"], 0)
            self.assertIsNone(empty["trains"]["land_rate"])

    def test_stats_reports_product_outcomes_and_conservative_batch_savings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = load_config(repo=root, db_override=root / "queue.sqlite")
            conn = connect(config.state.db)
            try:
                conn.execute(
                    "UPDATE recovery_operation_events "
                    "SET detail='schema_version=11;history_complete=0' "
                    "WHERE operation='tracking'"
                )
                conn.commit()
                landed_first = enqueue_job(conn, task="land-a", branch="agent/a")
                landed_second = enqueue_job(conn, task="land-b", branch="agent/b")
                superseded = enqueue_job(
                    conn, task="superseded", branch="agent/old"
                )
                blocked = enqueue_job(
                    conn, task="conflict", branch="agent/conflict"
                )
                queued = enqueue_job(conn, task="queued", branch="agent/queued")
                conn.execute(
                    "UPDATE deploy_queue SET status='deployed', "
                    "train_id='train-landed', train_size=2, "
                    "requested_at='2026-07-22T00:00:00Z', "
                    "validated_at='2026-07-22T00:01:20Z', "
                    "started_at='2026-07-22T00:02:00Z', "
                    "finished_at='2026-07-22T00:02:30Z', "
                    "push_status='succeeded', verify_status='succeeded' "
                    "WHERE id IN (?, ?)",
                    (landed_first.id, landed_second.id),
                )
                conn.execute(
                    "UPDATE deploy_queue SET status='canceled', "
                    "train_id='train-old', train_size=1, "
                    "requested_at='2026-07-22T00:00:00Z', "
                    "validated_at='2026-07-22T00:01:00Z', "
                    "started_at='2026-07-22T00:00:30Z', "
                    "finished_at='2026-07-22T00:03:00Z', "
                    "note='superseded by train-new' WHERE id=?",
                    (superseded.id,),
                )
                conn.execute(
                    "UPDATE deploy_queue SET status='blocked', "
                    "requested_at='2026-07-22T00:03:30Z', "
                    "started_at='2026-07-22T00:04:00Z', "
                    "finished_at='2026-07-22T00:04:10Z', "
                    "note='merge conflict in app.py' WHERE id=?",
                    (blocked.id,),
                )
                conn.execute(
                    "UPDATE deploy_queue SET "
                    "requested_at='2026-07-22T00:03:30Z' WHERE id=?",
                    (queued.id,),
                )
                events = [
                    (
                        "validate-landed",
                        None,
                        "claiming",
                        "active",
                        "Validate runner claimed 2 job(s)",
                        "mode=validate",
                        "2026-07-22T00:01:00Z",
                    ),
                    (
                        "validate-landed",
                        None,
                        "gating",
                        "active",
                        "Running gate 1/1: tests",
                        "",
                        "2026-07-22T00:01:10Z",
                    ),
                    (
                        "validate-landed",
                        None,
                        "gating",
                        "success",
                        "Passed gate 1/1: tests",
                        "",
                        "2026-07-22T00:01:20Z",
                    ),
                    (
                        "validate-landed",
                        landed_first.id,
                        "ready",
                        "success",
                        f"Job #{landed_first.id} validated",
                        "",
                        "2026-07-22T00:01:20Z",
                    ),
                    (
                        "validate-landed",
                        landed_second.id,
                        "ready",
                        "success",
                        f"Job #{landed_second.id} validated",
                        "",
                        "2026-07-22T00:01:20Z",
                    ),
                    (
                        "deploy-landed",
                        None,
                        "claiming",
                        "active",
                        "Deploy runner claimed 2 job(s)",
                        "mode=deploy",
                        "2026-07-22T00:02:00Z",
                    ),
                    (
                        "deploy-landed",
                        None,
                        "gating",
                        "active",
                        "Running gate 1/1: tests",
                        "",
                        "2026-07-22T00:02:00Z",
                    ),
                    (
                        "deploy-landed",
                        None,
                        "gating",
                        "success",
                        "Passed gate 1/1: tests",
                        "",
                        "2026-07-22T00:02:20Z",
                    ),
                    (
                        "deploy-landed",
                        landed_first.id,
                        "complete",
                        "success",
                        f"Job #{landed_first.id} deployed",
                        "",
                        "2026-07-22T00:02:30Z",
                    ),
                    (
                        "deploy-landed",
                        landed_second.id,
                        "complete",
                        "success",
                        f"Job #{landed_second.id} deployed",
                        "",
                        "2026-07-22T00:02:30Z",
                    ),
                    (
                        "validate-partial",
                        None,
                        "claiming",
                        "active",
                        "Validate runner claimed 2 job(s)",
                        "mode=validate",
                        "2026-07-22T00:04:00Z",
                    ),
                    (
                        "validate-partial",
                        None,
                        "gating",
                        "active",
                        "Running gate 1/1: tests",
                        "",
                        "2026-07-22T00:04:01Z",
                    ),
                    (
                        "validate-partial",
                        None,
                        "gating",
                        "error",
                        "Failed gate 1/1: tests",
                        "",
                        "2026-07-22T00:04:05Z",
                    ),
                    (
                        "validate-partial",
                        blocked.id,
                        "blocked",
                        "error",
                        f"Job #{blocked.id} blocked",
                        "",
                        "2026-07-22T00:04:10Z",
                    ),
                    (
                        "validate-partial",
                        queued.id,
                        "ready",
                        "success",
                        f"Job #{queued.id} validated",
                        "",
                        "2026-07-22T00:04:10Z",
                    ),
                ]
                conn.executemany(
                    "INSERT INTO run_events "
                    "(claim_token, job_id, phase, state, message, detail, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    events,
                )
                conn.commit()
            finally:
                conn.close()

            stats = stats_payload(config)
            self.assertEqual(stats["trains"]["status_counts"]["deployed"], 1)
            self.assertEqual(stats["trains"]["canceled"], 1)
            self.assertEqual(stats["trains"]["open"], 1)
            self.assertEqual(stats["trains"]["terminal"], 3)
            self.assertEqual(stats["trains"]["terminal_land_rate"], 0.3333)
            self.assertEqual(stats["trains"]["not_landed"], 3)
            self.assertEqual(stats["jobs"]["status_counts"]["deployed"], 2)
            self.assertEqual(stats["jobs"]["status_counts"]["queued"], 1)

            reasons = stats["outcomes"]["not_landed_reason_counts"]
            self.assertEqual(reasons["superseded"], 1)
            self.assertEqual(reasons["merge_conflict"], 1)
            self.assertEqual(reasons["queued"], 1)
            self.assertEqual(stats["outcomes"]["conflicts"]["terminal_trains"], 3)
            self.assertEqual(
                stats["outcomes"]["conflicts"]["merge_conflict_rate"],
                0.3333,
            )

            validation_runs = stats["validation"]["runs"]
            self.assertEqual(validation_runs["attempted"], 2)
            self.assertEqual(validation_runs["succeeded"], 1)
            self.assertEqual(validation_runs["partial"], 1)
            self.assertEqual(validation_runs["runs_with_failure"], 1)
            self.assertEqual(validation_runs["failure_rate_denominator"], 2)
            self.assertEqual(validation_runs["failure_rate"], 0.5)

            validated_trains = stats["validation"]["trains"]
            self.assertEqual(validated_trains["total"], 2)
            self.assertEqual(validated_trains["deployed"], 1)
            self.assertEqual(validated_trains["terminal_without_deploy"], 1)
            self.assertEqual(validated_trains["superseded"], 1)
            self.assertEqual(validated_trains["deployment_rate"], 0.5)

            batching = stats["batching"]
            self.assertEqual(batching["observed_runs"], 3)
            self.assertEqual(batching["runs_with_job_count"], 3)
            self.assertEqual(batching["jobs_per_run"]["median"], 2.0)
            self.assertEqual(batching["multi_job_runs"], 3)
            self.assertEqual(batching["multi_job_run_rate"], 1.0)
            savings = batching["estimated_savings"]
            self.assertEqual(savings["eligible_successful_multi_job_runs"], 2)
            self.assertEqual(savings["observed_gate_executions"], 2)
            self.assertEqual(savings["estimated_gate_executions_avoided"], 2)
            self.assertEqual(savings["estimated_gate_seconds_avoided"], 30.0)
            self.assertEqual(
                stats["evidence_gaps"][0]["metric"],
                "recovery_reconcile_frequency_before_tracking_start",
            )

            empty = stats_payload(config, since="2026-07-23T00:00:00Z")
            self.assertIsNone(empty["validation"]["runs"]["failure_rate"])
            self.assertEqual(empty["batching"]["jobs_per_run"]["samples"], 0)

    def test_stats_counts_recovery_operations_from_an_explicit_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = load_config(repo=root, db_override=root / "queue.sqlite")
            conn = connect(config.state.db)
            try:
                conn.execute(
                    "UPDATE recovery_operation_events "
                    "SET detail='schema_version=11;history_complete=0' "
                    "WHERE operation='tracking'"
                )
                conn.commit()
                reconcile_event = start_recovery_operation(
                    conn,
                    operation="reconcile",
                    applied=False,
                )
                finish_recovery_operation(
                    conn,
                    reconcile_event.invocation_id,
                    state="success",
                )
                recover_event = start_recovery_operation(
                    conn,
                    operation="recover",
                    applied=True,
                )
                finish_recovery_operation(
                    conn,
                    recover_event.invocation_id,
                    state="remote_unreachable",
                )
                start_recovery_operation(
                    conn,
                    operation="reconcile",
                    applied=True,
                )
                baseline = list_recovery_operation_events(conn)[0].created_at
            finally:
                conn.close()

            partial = stats_payload(config)
            recovery = partial["recovery"]
            self.assertFalse(recovery["history_complete"])
            self.assertEqual(recovery["tracking_started_at"], baseline)
            self.assertEqual(recovery["observed_invocations"], 3)
            self.assertEqual(recovery["terminal_invocations"], 2)
            self.assertEqual(
                recovery["operation_counts"],
                {"reconcile": 2, "recover": 1},
            )
            self.assertEqual(recovery["state_counts"]["success"], 1)
            self.assertEqual(recovery["state_counts"]["remote_unreachable"], 1)
            self.assertEqual(recovery["state_counts"]["incomplete"], 1)
            self.assertEqual(
                recovery["apply_mode_counts"],
                {"apply": 2, "dry_run": 1},
            )
            self.assertEqual(
                partial["evidence_gaps"][0]["metric"],
                "recovery_reconcile_frequency_before_tracking_start",
            )

            complete = stats_payload(config, since=baseline)
            self.assertTrue(complete["recovery"]["history_complete"])
            self.assertEqual(complete["recovery"]["observed_invocations"], 3)
            self.assertEqual(complete["evidence_gaps"], [])

    def test_new_database_has_a_complete_all_history_recovery_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = load_config(repo=root, db_override=root / "queue.sqlite")
            conn = connect(config.state.db)
            conn.close()

            stats = stats_payload(config)
            self.assertTrue(stats["recovery"]["history_complete"])
            self.assertEqual(stats["recovery"]["observed_invocations"], 0)
            self.assertEqual(stats["evidence_gaps"], [])

    def test_stats_attributes_runner_phases_and_recommends_slow_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".mergetrain.yaml").write_text(
                "project:\n  name: timings\n"
                "gates:\n"
                "  - name: tests\n"
                "    run: python -m pytest\n",
                encoding="utf-8",
            )
            config = load_config(repo=root, db_override=root / "queue.sqlite")
            conn = connect(config.state.db)

            def stamp(base: datetime, seconds: int) -> str:
                return (
                    (base + timedelta(seconds=seconds))
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )

            try:
                for index in range(3):
                    base = datetime(
                        2026, 7, 22, index, tzinfo=timezone.utc
                    )
                    queued = enqueue_job(
                        conn,
                        task=f"job-{index}",
                        branch=f"agent/{index}",
                    )
                    conn.execute(
                        "UPDATE deploy_queue SET status='deployed', "
                        "train_id=?, train_size=1, requested_at=?, "
                        "validated_at=?, started_at=?, finished_at=?, "
                        "push_status='succeeded', verify_status='succeeded' "
                        "WHERE id=?",
                        (
                            f"train-{index}",
                            stamp(base, 0),
                            stamp(base, 110),
                            stamp(base, 170),
                            stamp(base, 282),
                            queued.id,
                        ),
                    )
                    validation = f"validation-{index}"
                    deploy = f"deploy-{index}"
                    conn.executemany(
                        "INSERT INTO run_events "
                        "(claim_token, job_id, phase, state, message, detail, "
                        "created_at) VALUES (?, ?, ?, ?, ?, '', ?)",
                        [
                            (
                                validation,
                                None,
                                "claiming",
                                "active",
                                "Runner claimed 1 job(s)",
                                stamp(base, 30),
                            ),
                            (
                                validation,
                                None,
                                "fetching",
                                "active",
                                "Fetching origin/main",
                                stamp(base, 30),
                            ),
                            (
                                validation,
                                None,
                                "fetching",
                                "success",
                                "Integration worktree prepared",
                                stamp(base, 32),
                            ),
                            (
                                validation,
                                queued.id,
                                "assembling",
                                "active",
                                f"Merging agent/{index}",
                                stamp(base, 33),
                            ),
                            (
                                validation,
                                queued.id,
                                "assembling",
                                "success",
                                f"Merged agent/{index}",
                                stamp(base, 35),
                            ),
                            (
                                validation,
                                None,
                                "gating",
                                "active",
                                "Running gate 1/1: tests",
                                stamp(base, 40),
                            ),
                            (
                                validation,
                                None,
                                "gating",
                                "success",
                                "Passed gate 1/1: tests",
                                stamp(base, 110),
                            ),
                            (
                                validation,
                                queued.id,
                                "ready",
                                "success",
                                f"Job #{queued.id} validated",
                                stamp(base, 110),
                            ),
                            (
                                deploy,
                                None,
                                "claiming",
                                "active",
                                "Integrate runner claimed 1 job(s)",
                                stamp(base, 170),
                            ),
                            (
                                deploy,
                                None,
                                "fetching",
                                "active",
                                "Fetching origin/main",
                                stamp(base, 170),
                            ),
                            (
                                deploy,
                                None,
                                "fetching",
                                "success",
                                "Integration worktree prepared",
                                stamp(base, 172),
                            ),
                            (
                                deploy,
                                queued.id,
                                "assembling",
                                "active",
                                f"Merging agent/{index}",
                                stamp(base, 173),
                            ),
                            (
                                deploy,
                                queued.id,
                                "assembling",
                                "success",
                                f"Merged agent/{index}",
                                stamp(base, 175),
                            ),
                            (
                                deploy,
                                None,
                                "gating",
                                "active",
                                "Running gate 1/1: tests",
                                stamp(base, 180),
                            ),
                            (
                                deploy,
                                None,
                                "gating",
                                "success",
                                "Passed gate 1/1: tests",
                                stamp(base, 250),
                            ),
                            (
                                deploy,
                                None,
                                "pushing",
                                "active",
                                "Pushing verified HEAD atomically",
                                stamp(base, 250),
                            ),
                            (
                                deploy,
                                None,
                                "pushing",
                                "success",
                                "Atomic push completed",
                                stamp(base, 252),
                            ),
                            (
                                deploy,
                                None,
                                "verifying",
                                "active",
                                "Running post-push verification",
                                stamp(base, 252),
                            ),
                            (
                                deploy,
                                None,
                                "verifying",
                                "success",
                                "Post-push verification passed",
                                stamp(base, 282),
                            ),
                            (
                                deploy,
                                queued.id,
                                "complete",
                                "success",
                                f"Job #{queued.id} deployed",
                                stamp(base, 282),
                            ),
                        ],
                    )
                    conn.commit()
                conn.execute(
                    "INSERT INTO run_events "
                    "(claim_token, phase, state, message, detail, created_at) "
                    "VALUES ('partial', 'gating', 'active', "
                    "'Running gate 1/1: tests', '', "
                    "'2026-07-22T05:00:00Z')"
                )
                conn.commit()
            finally:
                conn.close()

            stats = stats_payload(config)
            latency = stats["latency"]
            self.assertEqual(latency["queue_wait"]["samples"], 3)
            self.assertEqual(latency["queue_wait"]["median_seconds"], 30.0)
            self.assertEqual(latency["approval_wait"]["samples"], 3)
            self.assertEqual(latency["approval_wait"]["median_seconds"], 60.0)
            self.assertEqual(latency["runs"]["validate"]["samples"], 3)
            self.assertEqual(latency["runs"]["validate"]["median_seconds"], 80.0)
            self.assertEqual(latency["runs"]["deploy"]["samples"], 3)
            self.assertEqual(latency["runs"]["deploy"]["median_seconds"], 112.0)
            self.assertEqual(latency["coverage"]["observed_runs"], 7)
            self.assertEqual(
                latency["coverage"]["runs_with_observed_start"], 6
            )
            self.assertEqual(latency["coverage"]["runs_with_terminal"], 6)
            self.assertEqual(latency["coverage"]["complete_runs"], 6)
            self.assertEqual(latency["coverage"]["runs_with_job_identity"], 6)
            self.assertEqual(latency["coverage"]["retained_events"], 61)
            self.assertEqual(latency["coverage"]["retention_limit"], 5000)
            self.assertTrue(latency["coverage"]["history_complete"])
            tests_gate = next(
                gate for gate in stats["gates"] if gate["name"] == "tests"
            )
            self.assertEqual(tests_gate["timed_runs"], 6)
            recommendation = stats["recommendations"][0]
            self.assertEqual(
                recommendation["code"], "slow_unscoped_gate"
            )
            self.assertEqual(
                recommendation["evidence"]["p95_seconds"], 70.0
            )
            self.assertEqual(
                recommendation["evidence"]["threshold_seconds"], 60.0
            )

            partial = stats_payload(
                config, since="2026-07-22T00:00:50Z"
            )["latency"]
            self.assertEqual(partial["runs"]["validate"]["samples"], 2)
            self.assertEqual(partial["queue_wait"]["samples"], 2)
            self.assertEqual(
                partial["coverage"]["runs_with_observed_start"], 5
            )
            self.assertEqual(partial["coverage"]["runs_with_terminal"], 6)
            self.assertEqual(partial["coverage"]["complete_runs"], 5)

    def test_stats_recommendations_use_latest_twenty_complete_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".mergetrain.yaml").write_text(
                "gates:\n  - name: tests\n    run: 'true'\n",
                encoding="utf-8",
            )
            config = load_config(repo=root, db_override=root / "queue.sqlite")
            conn = connect(config.state.db)
            try:
                base = datetime(2026, 8, 1, tzinfo=timezone.utc)
                rows = []
                for index in range(25):
                    token = f"run-{index:02d}"
                    started = base + timedelta(minutes=index * 5)
                    duration = 120 if index < 5 else 10
                    rows.extend(
                        [
                            (
                                token,
                                "claiming",
                                "active",
                                "Validation runner claimed 1 job",
                                "mode=validate",
                                started,
                            ),
                            (
                                token,
                                "gating",
                                "active",
                                "Running gate 1/1: tests",
                                "",
                                started + timedelta(seconds=1),
                            ),
                            (
                                token,
                                "gating",
                                "success",
                                "Passed gate 1/1: tests",
                                "",
                                started + timedelta(seconds=1 + duration),
                            ),
                            (
                                token,
                                "ready",
                                "success",
                                "Validation complete",
                                "",
                                started + timedelta(seconds=2 + duration),
                            ),
                        ]
                    )
                # The newest token is incomplete and must not displace a
                # completed run from the current window.
                rows.extend(
                    [
                        (
                            "incomplete",
                            "claiming",
                            "active",
                            "Validation runner claimed 1 job",
                            "mode=validate",
                            base + timedelta(hours=4),
                        ),
                        (
                            "incomplete",
                            "gating",
                            "active",
                            "Running gate 1/1: tests",
                            "",
                            base + timedelta(hours=4, seconds=1),
                        ),
                    ]
                )
                conn.executemany(
                    "INSERT INTO run_events "
                    "(claim_token, phase, state, message, detail, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (*row[:-1], row[-1].isoformat().replace("+00:00", "Z"))
                        for row in rows
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            stats = stats_payload(config)

        self.assertEqual(stats["gates"][0]["p95_seconds"], 120.0)
        self.assertEqual(stats["current"]["window"]["complete_runs"], 20)
        self.assertEqual(stats["current"]["gates"][0]["p95_seconds"], 10.0)
        self.assertEqual(stats["current"]["latency"]["coverage"]["complete_runs"], 20)
        self.assertEqual(stats["recommendations"], [])

    def test_approval_tail_alone_does_not_create_workflow_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = load_config(repo=td, db_override=Path(td) / "queue.sqlite")
        latency = {
            "approval_wait": {
                "samples": 3,
                "median_seconds": 300.0,
                "p95_seconds": 36000.0,
            },
            "runs": {
                "deploy": {
                    "samples": 3,
                    "median_seconds": 120.0,
                    "p95_seconds": 180.0,
                }
            },
        }
        self.assertEqual(_stats_recommendations(config, [], latency), [])

        latency["approval_wait"]["median_seconds"] = 1200.0
        recommendation = _stats_recommendations(config, [], latency)[0]
        self.assertEqual(recommendation["code"], "approval_wait_dominates")
        self.assertEqual(recommendation["evidence"]["threshold_seconds"], 900.0)

    def test_event_frame_reports_a_stranded_runner_as_lost(self) -> None:
        # A one-shot events reader (how the MCP server reads progress) has no
        # other lease signal. Collapsing "the runner is gone" into "inactive"
        # made an abandoned train look like an idle queue; inspect already drew
        # the distinction, so the two reads disagreed about the same state.
        from mergetrain.models import RunEvent

        event = RunEvent(
            id=1,
            job_id=7,
            claim_token="stale",
            phase="gating",
            state="active",
            message="Running gate 1/1: tests",
            created_at="2026-07-25T00:00:05Z",
        )
        running = Job(
            id=7,
            task="a",
            branch="agent/a",
            status="in_progress",
            claim_token="stale",
            started_at="2026-07-25T00:00:00Z",
        )
        # No lock at all: the runner that claimed this job is gone.
        self.assertEqual(event_record(event, [running], None)["lease_liveness"], "lost")

        # A lock held by a different token is equally not this job's runner.
        other = RunnerLock(
            name="runner", owner="runner:2", token="different", liveness="alive"
        )
        self.assertEqual(event_record(event, [running], other)["lease_liveness"], "lost")

        # The live owner still reports its own liveness.
        mine = RunnerLock(
            name="runner", owner="runner:1", token="stale", liveness="alive"
        )
        self.assertEqual(event_record(event, [running], mine)["lease_liveness"], "alive")

        # And a terminal job with no runner is idle, not lost.
        done = Job(id=7, task="a", branch="agent/a", status="deployed")
        self.assertEqual(event_record(event, [done], None)["lease_liveness"], "inactive")

    def test_since_normalization_rejects_invalid_timestamp(self) -> None:
        self.assertEqual(
            normalize_since("2026-07-22T09:00:00+09:00"),
            "2026-07-22T00:00:00Z",
        )
        with self.assertRaisesRegex(ValueError, "ISO-8601"):
            normalize_since("yesterday")


if __name__ == "__main__":
    unittest.main()
