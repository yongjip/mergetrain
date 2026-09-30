from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from mergetrain.cli import main
from mergetrain.config import load_config
from mergetrain.daemon import (
    _grade_batch,
    _grade_validation_batch,
    daemon_loop,
    daemon_tick,
)
from mergetrain.deploy_plan import deploy_destination_sha, deploy_execution_policy_sha
from mergetrain.errors import ConfigError, MergetrainError, QueueBusy, QueueError
from mergetrain.models import Job
from mergetrain.persistence.claims import claim_all_queued
from mergetrain.persistence.connection import connect
from mergetrain.persistence.jobs import enqueue_job, get_job, list_jobs, mark_job
from mergetrain.persistence.leases import (
    acquire_runner_lock,
    default_owner,
    get_lock,
    release_runner_lock,
)


class GradeBatchTests(unittest.TestCase):
    def _jobs(self, *statuses):
        return [Job(id=i, task="t", branch=f"b{i}", status=s) for i, s in enumerate(statuses)]

    def test_all_deployed_is_landed(self) -> None:
        self.assertEqual(_grade_batch(self._jobs("deployed", "deployed"), 2, lambda _: None), "landed:2")

    def test_a_landing_with_failed_or_unfinished_verification_is_unverified(self) -> None:
        for verify_status in ("failed", "unknown"):
            with self.subTest(verify_status=verify_status):
                jobs = [
                    Job(id=1, task="t", branch="b1", status="deployed", verify_status="succeeded"),
                    Job(id=2, task="t", branch="b2", status="deployed", verify_status=verify_status),
                ]
                said: list[str] = []
                self.assertEqual(_grade_batch(jobs, 2, said.append), "unverified:2")
                self.assertTrue(said)

    def test_nothing_deployed_is_no_landing_not_processed(self) -> None:
        # The bug: a sweep where every job blocked reported as a green deploy.
        out = _grade_batch(self._jobs("blocked", "failed"), 2, lambda _: None)
        self.assertEqual(out, "no_landing:2")

    def test_some_deployed_is_partial(self) -> None:
        out = _grade_batch(self._jobs("deployed", "blocked"), 2, lambda _: None)
        self.assertEqual(out, "partial:1/2")

    def test_uninspectable_result_falls_back_to_processed(self) -> None:
        self.assertEqual(_grade_batch(None, 3, lambda _: None), "processed:3")

    def test_validation_grading_never_implies_a_deploy(self) -> None:
        self.assertEqual(
            _grade_validation_batch(
                self._jobs("validated", "validated"), 2, lambda _: None
            ),
            "validated:2",
        )
        self.assertEqual(
            _grade_validation_batch(
                self._jobs("validated", "blocked"), 2, lambda _: None
            ),
            "validation_partial:1/2",
        )
        self.assertEqual(
            _grade_validation_batch(
                self._jobs("blocked", "failed"), 2, lambda _: None
            ),
            "validation_failed:2",
        )
        self.assertEqual(
            _grade_validation_batch(None, 3, lambda _: None),
            "validation_processed:3",
        )


class DaemonTests(unittest.TestCase):
    def test_unresolvable_destination_blocks_auto_jobs_before_runner_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            job = enqueue_job(
                conn,
                task="auto",
                branch="auto",
                auto_deploy=True,
                approval_destination_sha="a" * 64,
            )
            conn.close()

            def invalid_destination() -> str:
                raise MergetrainError("multiple push URLs")

            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: self.fail("auto job reached runner work"),
                owner="daemon:1",
                say=lambda _: None,
                approval_destination_sha=invalid_destination,
            )

            self.assertEqual(outcome, "no_landing:1")
            conn = connect(db)
            try:
                blocked = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(blocked.status, "blocked")
            self.assertIn("approval_destination_changed", blocked.note)

    def test_an_unreadable_config_pauses_the_tick_without_blocking_jobs(self) -> None:
        """#231: one tick with the config missing must not block auto jobs."""

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            job = enqueue_job(
                conn,
                task="auto",
                branch="auto",
                auto_deploy=True,
                approval_destination_sha="a" * 64,
                approval_execution_policy_sha="b" * 64,
            )
            conn.close()

            def config_missing() -> str:
                raise ConfigError("no .mergetrain.yaml; run mergetrain init --write")

            with self.assertRaises(ConfigError):
                daemon_tick(
                    db_path=str(db),
                    process_batch=lambda conn, jobs: self.fail("ran while config was missing"),
                    owner="daemon:1",
                    say=lambda _: None,
                    approval_destination_sha=config_missing,
                    approval_execution_policy_sha=lambda: "b" * 64,
                )
            conn = connect(db)
            try:
                paused = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual((paused.status, paused.note), ("queued", ""))

            # Once the config is back, the same job is claimed and processed.
            processed: list[int] = []
            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: processed.extend(item.id for item in jobs),
                owner="daemon:1",
                say=lambda _: None,
                approval_destination_sha=lambda: "a" * 64,
                approval_execution_policy_sha=lambda: "b" * 64,
            )
            self.assertEqual(processed, [job.id])
            self.assertEqual(outcome, "processed:1")

    def test_a_tick_that_could_not_drop_its_lease_does_not_wedge_the_daemon(self) -> None:
        """Contention that outlasts a failed batch left the daemon's own live
        lease on an in-progress row, and every later tick then idled."""

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            job = enqueue_job(
                conn,
                task="auto",
                branch="auto",
                auto_deploy=True,
                approval_destination_sha="a" * 64,
                approval_execution_policy_sha="b" * 64,
            )
            conn.close()
            owner = f"daemon:{os.getpid()}"

            def batch_hits_contention(conn, jobs):  # type: ignore[no-untyped-def]
                raise QueueBusy("database is locked")

            busy = QueueBusy("database is locked")
            with (
                patch("mergetrain.daemon.force_clear_lock_and_split", side_effect=busy),
                patch("mergetrain.daemon.release_runner_lock", side_effect=busy),
                self.assertRaises(QueueBusy),
            ):
                daemon_tick(
                    db_path=str(db),
                    process_batch=batch_hits_contention,
                    owner=owner,
                    say=lambda _: None,
                    approval_destination_sha="a" * 64,
                    approval_execution_policy_sha="b" * 64,
                )
            conn = connect(db)
            try:
                self.assertEqual(get_job(conn, job.id).status, "in_progress")
                lock = get_lock(conn)
                self.assertEqual(lock.owner if lock else "", owner)
            finally:
                conn.close()

            processed: list[int] = []
            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: processed.extend(item.id for item in jobs),
                owner=owner,
                say=lambda _: None,
                approval_destination_sha="a" * 64,
                approval_execution_policy_sha="b" * 64,
            )
            self.assertEqual(processed, [job.id])
            self.assertEqual(outcome, "processed:1")

    def test_execution_policy_mismatch_blocks_auto_jobs_before_runner_work(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            job = enqueue_job(
                conn,
                task="auto",
                branch="auto",
                auto_deploy=True,
                approval_destination_sha="destination-a",
                approval_execution_policy_sha="policy-a",
            )
            conn.close()

            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: self.fail(
                    "auto job reached runner work"
                ),
                owner="daemon:1",
                say=lambda _: None,
                approval_destination_sha="destination-a",
                approval_execution_policy_sha="policy-b",
            )

            self.assertEqual(outcome, "no_landing:1")
            conn = connect(db)
            try:
                blocked = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(blocked.status, "blocked")
            self.assertIn("approval_execution_policy_changed", blocked.note)

    def test_jobs_blocked_for_a_changed_approval_notify_as_blocked(self) -> None:
        """#23: the tick that blocked them reported idle, which never notifies."""

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            enqueue_job(
                conn,
                task="auto",
                branch="auto",
                auto_deploy=True,
                approval_destination_sha="destination-a",
                approval_execution_policy_sha="policy-a",
            )
            conn.close()
            received: list[tuple[str, str]] = []

            daemon_loop(
                db_path=str(db),
                process_batch=lambda conn, jobs: self.fail("auto job reached runner work"),
                owner="daemon:1",
                once=True,
                say=lambda _: None,
                install_signal_handlers=False,
                notifier=lambda title, message: received.append((title, message)),
                notification_name="svc",
                notification_transitions=("blocked",),
                notification_state_path=Path(td) / "notify.json",
                approval_destination_sha="destination-b",
                approval_execution_policy_sha="policy-a",
            )

            self.assertEqual(
                received, [("mergetrain · svc", "Nothing landed — 1 job blocked or failed")]
            )

    def test_a_job_blocked_beside_a_landing_makes_the_tick_partial(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            for destination in ("destination-a", "destination-old"):
                enqueue_job(
                    conn,
                    task=destination,
                    branch=destination,
                    auto_deploy=True,
                    approval_destination_sha=destination,
                    approval_execution_policy_sha="policy-a",
                )
            conn.close()

            def land(conn, jobs):  # type: ignore[no-untyped-def]
                return [
                    mark_job(
                        conn, job.id, status="deployed", expected_claim_token=job.claim_token
                    )
                    for job in jobs
                ]

            outcome = daemon_tick(
                db_path=str(db),
                process_batch=land,
                owner="daemon:1",
                say=lambda _: None,
                approval_destination_sha="destination-a",
                approval_execution_policy_sha="policy-a",
            )

            self.assertEqual(outcome, "partial:1/2")

    def test_a_runner_that_takes_the_lock_after_the_probe_is_waited_for(self) -> None:
        """#42: ordinary lock contention is not a daemon error to notify about."""

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            enqueue_job(
                conn,
                task="auto",
                branch="auto",
                auto_deploy=True,
                approval_destination_sha="a" * 64,
                approval_execution_policy_sha="b" * 64,
            )
            # A manual validate that has finished its jobs but still holds the
            # lock while it cleans up, so the probe sees no work in progress.
            runner = f"alice:{os.getpid()}"
            lock = acquire_runner_lock(conn, owner=runner)
            conn.close()
            received: list[tuple[str, str]] = []

            outcome = daemon_loop(
                db_path=str(db),
                process_batch=lambda conn, jobs: self.fail("the lock was held"),
                owner="daemon:1",
                once=True,
                say=lambda _: None,
                install_signal_handlers=False,
                notifier=lambda title, message: received.append((title, message)),
                notification_name="svc",
                notification_state_path=Path(td) / "notify.json",
                approval_destination_sha="a" * 64,
                approval_execution_policy_sha="b" * 64,
            )

            self.assertEqual(outcome, "idle")
            self.assertEqual(received, [])
            conn = connect(db)
            try:
                release_runner_lock(conn, owner=runner, token=lock.token)
            finally:
                conn.close()

    def test_validation_loop_rejects_deploy_notifier(self) -> None:
        with self.assertRaisesRegex(QueueError, "does not support"):
            daemon_loop(
                db_path="unused.sqlite",
                process_batch=lambda conn, jobs: [],
                once=True,
                notifier=lambda title, message: None,
                validate_only=True,
            )

    def test_validate_only_processes_manual_jobs_then_pauses_at_train(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            manual = enqueue_job(conn, task="manual", branch="manual")
            auto = enqueue_job(conn, task="auto", branch="auto", auto_deploy=True)
            conn.close()

            seen: list[int] = []

            def validate(conn, jobs):  # type: ignore[no-untyped-def]
                seen.extend(job.id for job in jobs)
                return [
                    mark_job(
                        conn,
                        job.id,
                        status="validated",
                        expected_claim_token=job.claim_token,
                    )
                    for job in jobs
                ]

            outcome = daemon_tick(
                db_path=str(db),
                process_batch=validate,
                owner="daemon:999999",
                say=lambda _: None,
                validate_only=True,
            )
            self.assertEqual(outcome, "validated:1")
            self.assertEqual(seen, [manual.id])

            second = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: self.fail(
                    "a second train was claimed while validation was paused"
                ),
                owner="daemon:999999",
                say=lambda _: None,
                validate_only=True,
            )
            self.assertEqual(second, "validation_paused")
            conn = connect(db)
            try:
                self.assertEqual(get_job(conn, manual.id).status, "validated")
                self.assertEqual(get_job(conn, auto.id).status, "queued")
            finally:
                conn.close()

    def test_validate_only_pauses_on_incomplete_validated_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            old = enqueue_job(conn, task="validated", branch="validated")
            mark_job(conn, old.id, status="validated")
            waiting = enqueue_job(conn, task="waiting", branch="waiting")
            conn.close()

            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: self.fail("queued job was claimed"),
                owner="daemon:999999",
                say=lambda _: None,
                validate_only=True,
            )

            self.assertEqual(outcome, "validation_paused")
            conn = connect(db)
            try:
                self.assertEqual(get_job(conn, waiting.id).status, "queued")
                self.assertEqual(conn.execute("SELECT * FROM locks").fetchall(), [])
            finally:
                conn.close()

    def test_tick_reports_live_marker_owner_as_active_not_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            # Another live process: its PID is alive, and its owner is not the
            # daemon's, which only ever finds its own lease left by a failed tick.
            runner = f"other-runner:{os.getpid()}"
            conn = connect(db)
            enqueue_job(
                conn, task="active", branch="active", auto_deploy=True
            )
            claimed = claim_all_queued(conn, owner=runner, auto_only=True)
            token = claimed[0].claim_token
            conn.execute(
                "UPDATE deploy_queue SET pending_deploy_sha='active-sha' WHERE id=?",
                (claimed[0].id,),
            )
            conn.commit()
            conn.close()

            messages: list[str] = []
            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: self.fail("active job was reclaimed"),
                owner=default_owner(),
                say=messages.append,
            )

            self.assertEqual(outcome, "idle")
            self.assertTrue(any("runner is active" in item for item in messages))
            conn = connect(db)
            try:
                self.assertEqual(get_job(conn, claimed[0].id).status, "in_progress")
                release_runner_lock(conn, owner=runner, token=token)
            finally:
                conn.close()

    def test_daemon_once_processes_only_auto_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            manual = enqueue_job(conn, task="manual", branch="manual")
            auto = enqueue_job(conn, task="auto", branch="auto", auto_deploy=True)
            conn.close()

            seen = []

            def process_batch(conn, jobs):  # type: ignore[no-untyped-def]
                seen.extend(job.id for job in jobs)

            daemon_loop(
                db_path=str(db),
                process_batch=process_batch,
                owner="daemon:999999",
                once=True,
                say=lambda _: None,
                install_signal_handlers=False,
            )
            self.assertEqual(seen, [auto.id])
            conn = connect(db)
            try:
                jobs = {job.id: job for job in list_jobs(conn)}
            finally:
                conn.close()
            self.assertEqual(jobs[manual.id].status, "queued")

    def test_tick_pauses_when_claim_parks_orphans_as_needs_reconcile(self) -> None:
        # TOCTOU guard: the pre-claim reconcile check passes, but acquiring
        # the lock requeues a dead owner's orphans and parks a marker-bearing
        # job as needs_reconcile. The same claim must then refuse to deploy.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            orphan = enqueue_job(conn, task="orphan", branch="orphan")
            auto = enqueue_job(conn, task="auto", branch="auto", auto_deploy=True)
            # A dead runner (impossible PID) left an expired lock, an
            # in_progress job carrying its claim token, and a durable
            # pending-deploy marker from a push that may have landed.
            conn.execute(
                """
                INSERT INTO locks (name, owner, acquired_at, heartbeat_at, expires_at, token)
                VALUES ('runner', 'daemon:999999', '2000-01-01T00:00:00Z',
                        '2000-01-01T00:00:00Z', '2000-01-01T00:00:01Z', 'dead-token')
                """
            )
            conn.execute(
                "UPDATE deploy_queue SET status='in_progress', claim_token='dead-token', "
                "pending_deploy_sha='deadbeef' WHERE id = ?",
                (orphan.id,),
            )
            conn.commit()
            conn.close()

            # Call the claim directly: the daemon's pre-claim check has
            # already passed in the TOCTOU scenario, so the guard must live
            # inside the claim transaction itself.
            conn = connect(db)
            try:
                jobs = claim_all_queued(
                    conn, owner="daemon:1", auto_only=True
                )
                self.assertEqual(jobs, [])
                self.assertEqual(get_job(conn, orphan.id).status, "needs_reconcile")
                self.assertEqual(get_job(conn, auto.id).status, "queued")
                lock = conn.execute("SELECT * FROM locks").fetchall()
            finally:
                conn.close()
            # The claim released its own lock instead of deploying past the
            # freshly parked reconcile.
            self.assertEqual(lock, [])

            # And the daemon tick reports the pause rather than "idle".
            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: None,
                owner="daemon:1",
                say=lambda _: None,
            )
            self.assertEqual(outcome, "reconcile_paused")

    def test_tick_rechecks_reconcile_after_orphan_heal(self) -> None:
        from mergetrain.persistence.leases import recover_orphans as real_recover_orphans

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            orphan = enqueue_job(conn, task="orphan", branch="orphan")
            conn.execute(
                "UPDATE deploy_queue SET status='in_progress', claim_token='old' "
                "WHERE id = ?",
                (orphan.id,),
            )
            conn.commit()
            conn.close()

            def marker_arrives_during_heal(conn_arg, **kwargs):  # type: ignore[no-untyped-def]
                healed = real_recover_orphans(conn_arg, **kwargs)
                conn_arg.execute(
                    "UPDATE deploy_queue SET status='needs_reconcile', "
                    "pending_deploy_sha='deadbeef' WHERE id = ?",
                    (orphan.id,),
                )
                conn_arg.commit()
                return healed

            seen: list[int] = []
            with patch(
                "mergetrain.daemon.recover_orphans",
                side_effect=marker_arrives_during_heal,
            ):
                outcome = daemon_tick(
                    db_path=str(db),
                    process_batch=lambda conn, jobs: seen.extend(j.id for j in jobs),
                    owner="daemon:1",
                    say=lambda _: None,
                )

            self.assertEqual(outcome, "reconcile_paused")
            self.assertEqual(seen, [])

    def test_tick_rechecks_reconcile_after_claim_race(self) -> None:
        from mergetrain.persistence.claims import claim_all_queued as real_claim_all_queued

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            auto = enqueue_job(
                conn, task="auto", branch="auto", auto_deploy=True
            )
            raced = enqueue_job(conn, task="raced", branch="raced")
            conn.close()

            def orphan_appears_before_claim(conn_arg, **kwargs):  # type: ignore[no-untyped-def]
                control = connect(db)
                try:
                    control.execute(
                        "UPDATE deploy_queue SET status='in_progress', "
                        "claim_token='old', pending_deploy_sha='deadbeef' WHERE id = ?",
                        (raced.id,),
                    )
                    control.commit()
                finally:
                    control.close()
                return real_claim_all_queued(conn_arg, **kwargs)

            seen: list[int] = []
            with patch(
                "mergetrain.daemon.claim_all_queued",
                side_effect=orphan_appears_before_claim,
            ):
                outcome = daemon_tick(
                    db_path=str(db),
                    process_batch=lambda conn, jobs: seen.extend(j.id for j in jobs),
                    owner="daemon:1",
                    say=lambda _: None,
                )

            self.assertEqual(outcome, "reconcile_paused")
            self.assertEqual(seen, [])
            conn = connect(db)
            try:
                self.assertEqual(get_job(conn, auto.id).status, "queued")
                self.assertEqual(get_job(conn, raced.id).status, "needs_reconcile")
            finally:
                conn.close()


class OrphanSelfHealTests(unittest.TestCase):
    def test_batch_exception_requeues_the_claim_and_next_tick_is_not_idle(self) -> None:
        # #84 defect 1: a batch that raises used to release only the lock, so
        # the claimed row stranded in_progress and every later tick reported
        # idle. The exception must requeue the claim; the next tick reprocesses.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            job = enqueue_job(conn, task="a", branch="a", auto_deploy=True)
            conn.close()

            calls = {"n": 0}

            def blow_up(conn, jobs):  # type: ignore[no-untyped-def]
                calls["n"] += 1
                raise RuntimeError("batch blew up")

            with self.assertRaises(RuntimeError):
                daemon_tick(
                    db_path=str(db),
                    process_batch=blow_up,
                    owner="daemon:1",
                    say=lambda _: None,
                )
            self.assertEqual(calls["n"], 1)
            conn = connect(db)
            try:
                # Requeued (a recoverable state), not stranded in_progress, and
                # the lease was dropped.
                self.assertEqual(get_job(conn, job.id).status, "queued")
                self.assertEqual(conn.execute("SELECT * FROM locks").fetchall(), [])
            finally:
                conn.close()

            seen: list[int] = []
            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: seen.extend(j.id for j in jobs),
                owner="daemon:1",
                say=lambda _: None,
            )
            self.assertEqual(seen, [job.id])
            self.assertNotEqual(outcome, "idle")

    def test_idle_tick_self_heals_a_dead_runners_stranded_claim(self) -> None:
        # A hard crash (no finally ran): a dead runner left a job in_progress
        # with its claim token and an expired lock, no queued work, and no
        # marker. Nothing else flags it, so the tick itself must recover it.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            job = enqueue_job(conn, task="a", branch="a", auto_deploy=True)
            conn.execute(
                """
                INSERT INTO locks (name, owner, acquired_at, heartbeat_at, expires_at, token)
                VALUES ('runner', 'ghost:999999', '2000-01-01T00:00:00Z',
                        '2000-01-01T00:00:00Z', '2000-01-01T00:00:01Z', 'dead-token')
                """
            )
            conn.execute(
                "UPDATE deploy_queue SET status='in_progress', claim_token='dead-token' WHERE id = ?",
                (job.id,),
            )
            conn.commit()
            conn.close()

            seen: list[int] = []
            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: seen.extend(j.id for j in jobs),
                owner="daemon:1",
                say=lambda _: None,
            )
            # The orphan is recovered and reprocessed in the same tick — never
            # left idle — and the dead runner's lock is gone.
            self.assertEqual(seen, [job.id])
            self.assertNotEqual(outcome, "idle")
            conn = connect(db)
            try:
                self.assertEqual(conn.execute("SELECT * FROM locks").fetchall(), [])
            finally:
                conn.close()

    def test_idle_tick_leaves_a_live_runners_in_progress_job_untouched(self) -> None:
        # The heal must never reap a live runner's train: a job in_progress
        # under a live lease (this process's own pid) with no queued work stays
        # in_progress, and the tick reports idle without disturbing it.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            job = enqueue_job(conn, task="a", branch="a", auto_deploy=True)
            live = f"host:{os.getpid()}"
            conn.execute(
                """
                INSERT INTO locks (name, owner, acquired_at, heartbeat_at, expires_at, token)
                VALUES ('runner', ?, '2999-01-01T00:00:00Z',
                        '2999-01-01T00:00:00Z', '2999-01-01T00:00:01Z', 'live-token')
                """,
                (live,),
            )
            conn.execute(
                "UPDATE deploy_queue SET status='in_progress', claim_token='live-token' WHERE id = ?",
                (job.id,),
            )
            conn.commit()
            conn.close()

            seen: list[int] = []
            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: seen.extend(j.id for j in jobs),
                owner="daemon:1",
                say=lambda _: None,
            )
            self.assertEqual(seen, [])
            self.assertEqual(outcome, "idle")
            conn = connect(db)
            try:
                self.assertEqual(get_job(conn, job.id).status, "in_progress")
                locks = conn.execute("SELECT owner FROM locks").fetchall()
            finally:
                conn.close()
            self.assertEqual([row["owner"] for row in locks], [live])


class DaemonOnceExitStatusTests(unittest.TestCase):
    """#24: a scheduler running `daemon --once` sees only its exit status."""

    def _run_once(self, root: Path, *, queued: bool, batch) -> int:  # type: ignore[no-untyped-def]
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", str(root / "remote.git")], cwd=repo, check=True
        )
        (repo / ".mergetrain.yaml").write_text("project:\n  name: demo\n", encoding="utf-8")
        config = load_config(repo=repo)
        conn = connect(config.state.db)
        try:
            if queued:
                enqueue_job(
                    conn,
                    task="auto",
                    branch="auto",
                    auto_deploy=True,
                    approval_destination_sha=deploy_destination_sha(config),
                    approval_execution_policy_sha=deploy_execution_policy_sha(config),
                )
        finally:
            conn.close()
        runner = Mock()
        runner.process_batch.side_effect = batch
        with (
            patch("mergetrain.commands.daemon.GitRunner", return_value=runner),
            redirect_stdout(io.StringIO()),
        ):
            return main(["--repo", str(repo), "daemon", "--once"])

    def test_once_fails_when_the_tick_errored_or_did_not_land(self) -> None:
        def finish(status: str):  # type: ignore[no-untyped-def]
            def batch(conn, jobs, **kwargs):  # type: ignore[no-untyped-def]
                return [
                    mark_job(conn, job.id, status=status, expected_claim_token=job.claim_token)
                    for job in jobs
                ]

            return batch

        def crash(conn, jobs, **kwargs):  # type: ignore[no-untyped-def]
            raise OSError("disk full")

        cases = (
            ("landed", True, finish("deployed"), 0),
            ("idle", False, finish("deployed"), 0),
            ("no_landing", True, finish("blocked"), 1),
            ("error", True, crash, 1),
        )
        for name, queued, batch, expected in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as td:
                self.assertEqual(self._run_once(Path(td), queued=queued, batch=batch), expected)


class ReadOnlyTickTests(unittest.TestCase):
    def test_idle_tick_does_not_resolve_a_deploy_destination(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            enqueue_job(conn, task="manual", branch="manual")
            conn.close()

            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: self.fail("manual job was claimed"),
                owner="daemon:1",
                say=lambda _: None,
                approval_destination_sha=lambda: self.fail(
                    "an idle auto-deploy tick resolved the destination"
                ),
            )

            self.assertEqual(outcome, "idle")

    def test_non_sovereign_tick_never_creates_or_migrates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            # Missing database: the hub path must refuse, not create.
            with self.assertRaises(QueueError):
                daemon_tick(
                    db_path=str(db),
                    process_batch=lambda conn, jobs: None,
                    owner="daemon:1",
                    say=lambda _: None,
                )
            self.assertFalse(db.exists())

            # Old schema stamp: the hub path must report, not migrate. And an
            # idle tick must not rewrite the repo's journal mode either.
            conn = connect(db)
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("PRAGMA user_version = 6")
            conn.commit()
            conn.close()
            with self.assertRaises(QueueError):
                daemon_tick(
                    db_path=str(db),
                    process_batch=lambda conn, jobs: None,
                    owner="daemon:1",
                    say=lambda _: None,
                )
            raw = sqlite3.connect(db)
            try:
                self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(
                    raw.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete"
                )
            finally:
                raw.close()

    def test_sovereign_tick_creates_its_own_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            outcome = daemon_tick(
                db_path=str(db),
                process_batch=lambda conn, jobs: None,
                owner="daemon:1",
                say=lambda _: None,
                sovereign=True,
            )
            self.assertEqual(outcome, "idle")
            self.assertTrue(db.is_file())

    def test_read_only_connect_survives_uri_special_characters(self) -> None:
        # Characters that are URI-special (so an unescaped sqlite URI would
        # truncate the filename or drop mode=ro) yet legal in a filename. '?'
        # exercises the query-string truncation but is illegal on Windows, so
        # include it only where the OS allows it; '#'/'%' cover the rest
        # everywhere.
        name = "we#dir%41" if os.name == "nt" else "we?rd#dir%41"
        with tempfile.TemporaryDirectory() as td:
            weird = Path(td) / name
            weird.mkdir()
            db = weird / "queue.sqlite"
            conn = connect(db)
            enqueue_job(conn, task="a", branch="a")
            conn.close()
            observer = connect(db, read_only=True)
            try:
                self.assertEqual(len(list_jobs(observer)), 1)
                with self.assertRaises(sqlite3.OperationalError):
                    observer.execute("UPDATE deploy_queue SET note = 'w'")
            finally:
                observer.close()


if __name__ == "__main__":
    unittest.main()
