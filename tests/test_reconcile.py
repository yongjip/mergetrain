"""Fault-injection matrix for 0.3.0 Phase 2 recovery (RFC §8).

Each test drives the queue into a specific crash/wedge state and asserts the
post-recovery truth: never mark ``deployed`` unless a push ref actually carries
the sha, never re-push a landed deploy, never guess when the remote is
unreachable. Builds on the real bare-remote fixture in ``test_git_runner``.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

# Reuse the bare-remote fixture from the sibling test module. `unittest discover
# -s tests` puts this dir on sys.path; add it explicitly so a single-module run
# (python -m unittest tests.test_reconcile) resolves the import too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mergetrain.cli import main
from mergetrain.config import load_config
from mergetrain.daemon import daemon_loop
from mergetrain.errors import CommandFailed
from mergetrain.git_runner import GitRunner, pending_ref_name
from mergetrain.recovery import _classify, reconcile, recover, sweep_pending_refs
from mergetrain.store import (
    acquire_runner_lock,
    cancel_job,
    claim_deploy_batch,
    claim_next_job,
    connect,
    deploy_reconcile_pending,
    enqueue_job,
    force_clear_lock_and_split,
    get_job,
    get_lock,
    mark_job,
    record_pending_push,
    release_runner_lock,
    utc_now,
)

# A pid that is never live, so a lock left by the "crashed" runner reads as DEAD
# during recovery (the test process itself is alive, so it cannot be the owner).
DEAD_OWNER = "ghost:999999"

from test_git_runner import git, make_demo_repo, py_path, rmtree


class _Crash(BaseException):
    """A BaseException so it escapes process_batch's ``except Exception`` and
    leaves the true post-crash on-disk state (in_progress + marker)."""


def _pending_commit(repo: Path) -> str:
    """Assemble a train HEAD (main + feature/a) without moving any branch."""
    git(repo, "switch", "-c", "_train_tmp", "main")
    git(repo, "merge", "--no-ff", "-m", "train", "feature/a")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    git(repo, "branch", "-D", "_train_tmp")
    return sha


def _pin(repo: Path, job_id: int, sha: str) -> None:
    git(repo, "update-ref", pending_ref_name(job_id), sha)


def _pending_refs(repo: Path) -> str:
    return git(repo, "for-each-ref", "--format=%(refname)", "refs/mergetrain/pending/")


def _stage_in_progress(conn, job_id: int, token: str, *, cancel: str = "") -> None:
    conn.execute(
        "UPDATE deploy_queue SET status='in_progress', claim_token=?, started_at=?, "
        "cancel_requested_at=? WHERE id=?",
        (token, utc_now(), cancel, job_id),
    )
    conn.commit()


def _set_needs_reconcile(conn, job_id: int, sha: str, *, cancel: str = "") -> None:
    conn.execute(
        "UPDATE deploy_queue SET status='needs_reconcile', pending_deploy_sha=?, "
        "push_status='pending', claim_token='', cancel_requested_at=? WHERE id=?",
        (sha, cancel, job_id),
    )
    conn.commit()


class ReconcileClassifierTests(unittest.TestCase):
    """RFC §5 decision table, driven directly against the bare remote."""

    def _prepare(self, root: Path):
        repo, _ = make_demo_repo(root)
        config = load_config(repo=repo)
        conn = connect(config.state.db)
        job = enqueue_job(conn, task="a", branch="feature/a")
        pending = _pending_commit(repo)
        _pin(repo, job.id, pending)
        return repo, config, conn, job, pending

    def test_landed_push_reconciles_to_deployed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, config, conn, job, pending = self._prepare(root)
            try:
                git(repo, "push", "origin", f"{pending}:main")  # the push landed
                _set_needs_reconcile(conn, job.id, pending)
                outcome = reconcile(config, conn, apply=True)
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(outcome.summary["reconciled_deployed"], 1)
            self.assertEqual(healed.status, "deployed")
            self.assertEqual(healed.deploy_sha, pending)
            self.assertEqual(healed.push_status, "succeeded")
            # reconcile can prove the deploy landed but not that verify ran.
            self.assertEqual(healed.verify_status, "unknown")
            self.assertEqual(healed.pending_deploy_sha, "")
            self.assertEqual(_pending_refs(repo), "")

    def test_unlanded_push_requeues_and_never_repushes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, config, conn, job, pending = self._prepare(root)
            try:
                _set_needs_reconcile(conn, job.id, pending)  # never pushed
                outcome = reconcile(config, conn, apply=True)
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(outcome.summary["requeued"], 1)
            self.assertEqual(healed.status, "queued")
            self.assertEqual(healed.pending_deploy_sha, "")
            self.assertEqual(_pending_refs(repo), "")
            # the remote never advanced
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:a.txt")

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, config, conn, job, pending = self._prepare(root)
            try:
                git(repo, "push", "origin", f"{pending}:main")
                _set_needs_reconcile(conn, job.id, pending)
                outcome = reconcile(config, conn, apply=False)
                unchanged = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(outcome.summary["reconciled_deployed"], 1)
            self.assertFalse(outcome.applied)
            self.assertEqual(unchanged.status, "needs_reconcile")
            self.assertEqual(unchanged.pending_deploy_sha, pending)

    def test_cancel_raced_push_that_landed_is_deployed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, config, conn, job, pending = self._prepare(root)
            try:
                git(repo, "push", "origin", f"{pending}:main")
                _set_needs_reconcile(conn, job.id, pending, cancel=utc_now())
                reconcile(config, conn, apply=True)
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            # The remote decides: a landed push wins over the late cancel.
            self.assertEqual(healed.status, "deployed")

    def test_cancel_raced_push_that_did_not_land_is_canceled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, config, conn, job, pending = self._prepare(root)
            try:
                _set_needs_reconcile(conn, job.id, pending, cancel=utc_now())
                outcome = reconcile(config, conn, apply=True)
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(outcome.summary["canceled"], 1)
            self.assertEqual(healed.status, "canceled")

    def test_concurrent_transition_during_remote_probe_survives_apply(self) -> None:
        """A newer state must win the classify/apply CAS race."""
        from mergetrain import recovery as recovery_module

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, config, conn, job, pending = self._prepare(root)
            real_apply = recovery_module._apply

            def cancel_then_apply(config_arg, conn_arg, decision):  # type: ignore[no-untyped-def]
                control = connect(config.state.db)
                try:
                    mark_job(
                        control,
                        decision.job.id,
                        status="canceled",
                        note="concurrent transition won",
                    )
                finally:
                    control.close()
                real_apply(config_arg, conn_arg, decision)

            try:
                _set_needs_reconcile(conn, job.id, pending)
                with patch(
                    "mergetrain.recovery._apply", side_effect=cancel_then_apply
                ):
                    reconcile(config, conn, apply=True)
                healed = get_job(conn, job.id)
            finally:
                conn.close()

            self.assertEqual(healed.status, "canceled")
            self.assertEqual(healed.note, "concurrent transition won")

    def test_pruned_pending_sha_without_pin_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, config, conn, job, pending = self._prepare(root)
            try:
                # Drop the pin ref and prune so the sha is unresolvable. The
                # reflog must be expired first or it keeps the dangling commit.
                git(repo, "update-ref", "-d", pending_ref_name(job.id))
                git(repo, "reflog", "expire", "--expire=now", "--all")
                git(repo, "gc", "--prune=now")
                _set_needs_reconcile(conn, job.id, pending)
                outcome = reconcile(config, conn, apply=True)
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(outcome.exit_code, 10)
            self.assertEqual(outcome.summary["conflicts"], 1)
            self.assertEqual(healed.status, "blocked")
            # marker + pin preserved for forensics (blocked keeps the marker).
            self.assertEqual(healed.pending_deploy_sha, pending)

    def test_pruned_pending_sha_kept_alive_by_pin_still_classifies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, config, conn, job, pending = self._prepare(root)
            try:
                git(repo, "gc", "--prune=now")  # pin ref keeps the dangling sha alive
                _set_needs_reconcile(conn, job.id, pending)
                outcome = reconcile(config, conn, apply=True)
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(healed.status, "queued")

    def test_mixed_push_refs_block(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            # Add a second push ref that P will NOT land on.
            cfg_path = repo / ".mergetrain.yaml"
            cfg_path.write_text(
                cfg_path.read_text(encoding="utf-8").replace(
                    "  push_refs:\n    - main\n",
                    "  push_refs:\n    - main\n    - release\n",
                ),
                encoding="utf-8",
            )
            git(repo, "push", "origin", "main:release")  # release starts at base
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                pending = _pending_commit(repo)
                _pin(repo, job.id, pending)
                git(repo, "push", "origin", f"{pending}:main")  # lands on main only
                _set_needs_reconcile(conn, job.id, pending)
                outcome = reconcile(config, conn, apply=True)
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(outcome.exit_code, 10)
            self.assertEqual(healed.status, "blocked")

    def test_remote_unreachable_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, config, conn, job, pending = self._prepare(root)
            try:
                _set_needs_reconcile(conn, job.id, pending)
                before = get_job(conn, job.id)
                rmtree(root / "remote.git")  # remote gone
                from mergetrain.errors import RemoteUnreachable

                with self.assertRaises(RemoteUnreachable):
                    reconcile(config, conn, apply=True)
                after = get_job(conn, job.id)
            finally:
                conn.close()
            # zero DB mutation — the job stays parked, exactly as before.
            self.assertEqual(after.status, "needs_reconcile")
            self.assertEqual(after.pending_deploy_sha, before.pending_deploy_sha)


class OrphanSplitTests(unittest.TestCase):
    """The marker-aware 3-way split (RFC §4), via lock acquisition."""

    def _run_split(self, conn) -> None:
        # No prior lock + in_progress orphans triggers the marker-aware split.
        acquire_runner_lock(conn, owner=f"runner:{os.getpid()}")
        release_runner_lock(conn, owner=None)

    def test_split_routes_by_marker_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                clean = enqueue_job(conn, task="clean", branch="b/clean")
                marked = enqueue_job(conn, task="marked", branch="b/marked")
                canceled = enqueue_job(conn, task="canceled", branch="b/canceled")
                raced = enqueue_job(conn, task="raced", branch="b/raced")

                _stage_in_progress(conn, clean.id, "t-clean")
                _stage_in_progress(conn, marked.id, "t-marked")
                _stage_in_progress(conn, canceled.id, "t-canceled", cancel=utc_now())
                _stage_in_progress(conn, raced.id, "t-raced", cancel=utc_now())

                record_pending_push(
                    conn, job_ids=[marked.id], deploy_sha="a" * 40, claim_token="t-marked"
                )
                record_pending_push(
                    conn, job_ids=[raced.id], deploy_sha="b" * 40, claim_token="t-raced"
                )

                self._run_split(conn)

                clean_j = get_job(conn, clean.id)
                marked_j = get_job(conn, marked.id)
                canceled_j = get_job(conn, canceled.id)
                raced_j = get_job(conn, raced.id)
            finally:
                conn.close()

            self.assertEqual(clean_j.status, "queued")
            # marker present -> parked for reconcile, marker preserved
            self.assertEqual(marked_j.status, "needs_reconcile")
            self.assertEqual(marked_j.pending_deploy_sha, "a" * 40)
            # cancel + no marker -> honored offline
            self.assertEqual(canceled_j.status, "canceled")
            # P6: cancel raced the marker -> needs_reconcile, both signals preserved
            self.assertEqual(raced_j.status, "needs_reconcile")
            self.assertEqual(raced_j.pending_deploy_sha, "b" * 40)
            self.assertTrue(raced_j.cancel_requested_at)


class CrashRecoveryTests(unittest.TestCase):
    """End-to-end: crash mid-deploy, then recover() to the truthful state."""

    def _crash_after_push(self, runner: GitRunner):
        real_push = runner.push_verified_head

        def push_then_crash(*, worktree, deploy_sha="", log=None, pulse=None):
            real_push(worktree=worktree, deploy_sha=deploy_sha, log=log, pulse=pulse)
            raise _Crash()

        return patch.object(runner, "push_verified_head", side_effect=push_then_crash)

    def test_batch_crash_after_push_recovers_to_deployed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                ttl = config.queue.lock_ttl_minutes
                claimed = claim_deploy_batch(conn, owner=DEAD_OWNER, ttl_minutes=ttl)
                runner = GitRunner(config)
                with self._crash_after_push(runner):
                    with self.assertRaises(_Crash):
                        runner.process_batch(
                            conn, claimed, deploy=True, owner=DEAD_OWNER, ttl_minutes=ttl
                        )
                crashed = get_job(conn, job.id)
                # true post-crash state: row in_progress, marker durable, remote moved
                self.assertEqual(crashed.status, "in_progress")
                self.assertNotEqual(crashed.pending_deploy_sha, "")
                self.assertEqual(crashed.push_status, "pending")
                self.assertEqual(git(root / "remote.git", "show", "main:a.txt"), "a")
            finally:
                conn.close()

            conn = connect(config.state.db)
            try:
                outcome = recover(config, conn, gc=False)
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(healed.status, "deployed")
            self.assertEqual(healed.push_status, "succeeded")
            self.assertEqual(healed.verify_status, "unknown")
            self.assertEqual(_pending_refs(repo), "")

    def test_ambiguous_push_parks_needs_reconcile_then_reconcile_deploys(self) -> None:
        # In-process ambiguous push: the remote atomically ACCEPTS the push, then
        # the client hits a transport error. The job must park needs_reconcile
        # (marker preserved) and block later deploys — never terminal 'failed' a
        # re-deploy would push over. reconcile then sees the sha already on the
        # remote and marks it deployed WITHOUT a second push (exactly-once, #3).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                ttl = config.queue.lock_ttl_minutes
                claimed = claim_deploy_batch(conn, owner=DEAD_OWNER, ttl_minutes=ttl)
                runner = GitRunner(config)
                real_push = runner.push_verified_head

                def land_then_drop(*, worktree, deploy_sha="", log=None, pulse=None):
                    real_push(worktree=worktree, deploy_sha=deploy_sha, log=log, pulse=pulse)
                    raise CommandFailed(
                        ["git", "push"], 1,
                        stderr="fatal: the remote end hung up unexpectedly",
                    )

                with patch.object(runner, "push_verified_head", side_effect=land_then_drop):
                    runner.process_batch(
                        conn, claimed, deploy=True, owner=DEAD_OWNER, ttl_minutes=ttl
                    )
                parked = get_job(conn, job.id)
                self.assertEqual(parked.status, "needs_reconcile")
                self.assertNotEqual(parked.pending_deploy_sha, "")
                self.assertGreater(deploy_reconcile_pending(conn), 0)
                self.assertEqual(git(root / "remote.git", "show", "main:a.txt"), "a")
                before = git(root / "remote.git", "rev-parse", "main")
                outcome = reconcile(config, conn, apply=True)
                after = git(root / "remote.git", "rev-parse", "main")
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(healed.status, "deployed")
            self.assertEqual(healed.push_status, "succeeded")
            self.assertEqual(after, before)  # reconcile never re-pushed
            self.assertEqual(_pending_refs(repo), "")

    def test_ambiguous_push_with_late_cancel_preserves_remote_truth(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                ttl = config.queue.lock_ttl_minutes
                claimed = claim_deploy_batch(conn, owner=DEAD_OWNER, ttl_minutes=ttl)
                runner = GitRunner(config)
                real_push = runner.push_verified_head

                def land_cancel_then_drop(*, worktree, deploy_sha="", log=None, pulse=None):
                    real_push(
                        worktree=worktree,
                        deploy_sha=deploy_sha,
                        log=log,
                        pulse=pulse,
                    )
                    control = connect(config.state.db)
                    try:
                        cancel_job(control, job.id)
                    finally:
                        control.close()
                    raise CommandFailed(
                        ["git", "push"],
                        1,
                        stderr="fatal: the remote end hung up unexpectedly",
                    )

                with patch.object(
                    runner, "push_verified_head", side_effect=land_cancel_then_drop
                ):
                    runner.process_batch(
                        conn, claimed, deploy=True, owner=DEAD_OWNER, ttl_minutes=ttl
                    )
                parked = get_job(conn, job.id)
                self.assertEqual(parked.status, "needs_reconcile")
                self.assertNotEqual(parked.pending_deploy_sha, "")
                self.assertTrue(parked.cancel_requested_at)
                self.assertNotEqual(_pending_refs(repo), "")

                outcome = reconcile(config, conn, apply=True)
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(healed.status, "deployed")
            self.assertEqual(healed.push_status, "succeeded")
            self.assertIn("late cancel ignored", healed.note)
            self.assertEqual(_pending_refs(repo), "")

    def test_reconcile_uses_the_marker_target_not_the_current_config(self) -> None:
        # #84 defect 3 (durable target): the config's remote/push_refs can change
        # between a crashed push and the reconcile. Reconcile must evaluate the
        # target the interrupted push actually used — captured in the marker —
        # not whatever the config now says.
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                ttl = config.queue.lock_ttl_minutes
                claimed = claim_deploy_batch(conn, owner=DEAD_OWNER, ttl_minutes=ttl)
                runner = GitRunner(config)
                real_push = runner.push_verified_head

                def land_then_drop(*, worktree, deploy_sha="", log=None, pulse=None):
                    real_push(worktree=worktree, deploy_sha=deploy_sha, log=log, pulse=pulse)
                    raise CommandFailed(
                        ["git", "push"], 1,
                        stderr="fatal: the remote end hung up unexpectedly",
                    )

                with patch.object(runner, "push_verified_head", side_effect=land_then_drop):
                    runner.process_batch(
                        conn, claimed, deploy=True, owner=DEAD_OWNER, ttl_minutes=ttl
                    )
                parked = get_job(conn, job.id)
                self.assertEqual(parked.status, "needs_reconcile")
                # The marker captured the real push target.
                self.assertEqual(parked.pending_deploy_remote, config.git.remote)
                self.assertEqual(parked.pending_deploy_refs, "main")
            finally:
                conn.close()

            # The config now points at a bogus remote AND a ref the push never
            # touched. Trusting the config, reconcile could not even reach the
            # remote; trusting the marker it reaches origin/main, sees the sha,
            # and deploys.
            drifted = replace(
                config,
                git=replace(
                    config.git,
                    remote="nonexistent-remote",
                    push_refs=("deploy-elsewhere",),
                ),
            )
            conn = connect(config.state.db)
            try:
                outcome = reconcile(drifted, conn, apply=True)
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(healed.status, "deployed")
            self.assertEqual(outcome.summary["reconciled_deployed"], 1)

    def test_isolation_push_site_writes_marker(self) -> None:
        # Proves the one-by-one process_one push site is instrumented too.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                ttl = config.queue.lock_ttl_minutes
                claimed = claim_next_job(conn, owner=DEAD_OWNER, ttl_minutes=ttl)
                runner = GitRunner(config)
                with self._crash_after_push(runner):
                    with self.assertRaises(_Crash):
                        runner.process_one(
                            conn, claimed, deploy=True, owner=DEAD_OWNER, ttl_minutes=ttl
                        )
                crashed = get_job(conn, job.id)
                self.assertEqual(crashed.status, "in_progress")
                self.assertNotEqual(crashed.pending_deploy_sha, "")
                self.assertEqual(git(root / "remote.git", "show", "main:a.txt"), "a")
            finally:
                conn.close()

            conn = connect(config.state.db)
            try:
                recover(config, conn, gc=False)
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(healed.status, "deployed")


class DeployGateTests(unittest.TestCase):
    def test_run_next_claim_rechecks_reconcile_after_reaping_dead_owner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            conn = connect(db)
            try:
                orphan = enqueue_job(conn, task="pushing", branch="feature/pushing")
                queued = enqueue_job(conn, task="next", branch="feature/next")
                old_lock = acquire_runner_lock(conn, owner=DEAD_OWNER)
                conn.execute(
                    "UPDATE deploy_queue SET status='in_progress', claim_token=?, "
                    "pending_deploy_sha=?, push_status='pending' WHERE id=?",
                    (old_lock.token, "a" * 40, orphan.id),
                )
                conn.commit()

                claimed = claim_next_job(
                    conn,
                    owner=f"replacement:{os.getpid()}",
                    deploy=True,
                )

                self.assertIsNone(claimed)
                self.assertEqual(get_job(conn, orphan.id).status, "needs_reconcile")
                self.assertEqual(get_job(conn, queued.id).status, "queued")
                self.assertIsNone(get_lock(conn))

                # Validation remains safe and available while remote deploy
                # truth is unresolved; only a deploy claim is refused.
                validation = claim_next_job(
                    conn,
                    owner=f"validator:{os.getpid()}",
                    deploy=False,
                )
                self.assertEqual(validation.id, queued.id)
                release_runner_lock(
                    conn,
                    owner=f"validator:{os.getpid()}",
                    token=validation.claim_token,
                )
            finally:
                conn.close()

    def test_run_batch_deploy_hard_blocked_while_needs_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                _set_needs_reconcile(conn, job.id, "c" * 40)
            finally:
                conn.close()

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "run-batch", "--deploy", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(payload["next_action"], "reconcile_pending_deploy")
            self.assertFalse(payload["ok"])
            # Contract 1: the deploy-block now uses the uniform failure envelope.
            self.assertEqual(payload["error"]["code"], "reconcile_pending_deploy")
            self.assertEqual(payload["needs_reconcile"], 1)
            # the remote must not have advanced
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:a.txt")


class DoctorNextActionTests(unittest.TestCase):
    def _doctor(self, repo: Path) -> dict:
        out = io.StringIO()
        with redirect_stdout(out):
            main(["--repo", str(repo), "doctor", "--json"])
        return json.loads(out.getvalue())

    def test_needs_reconcile_reports_reconcile_pending_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                _set_needs_reconcile(conn, job.id, "d" * 40)
            finally:
                conn.close()
            self.assertEqual(self._doctor(repo)["next_action"], "reconcile_pending_deploy")

    def test_reconciled_deploy_reports_verify_reconciled_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                from mergetrain.store import mark_job

                mark_job(
                    conn,
                    job.id,
                    status="deployed",
                    deploy_sha="e" * 40,
                    push_status="succeeded",
                    verify_status="unknown",
                )
            finally:
                conn.close()
            self.assertEqual(
                self._doctor(repo)["next_action"], "verify_reconciled_deploy"
            )
            # `mergetrain verify --ack` discharges it — the next_action was
            # otherwise permanent (deployed_verify_unknown never decremented).
            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "verify", "--ack", "succeeded", "--json"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["resolved"][0]["verify_status"], "succeeded")
            self.assertNotEqual(payload["next_action"], "verify_reconciled_deploy")
            self.assertNotEqual(
                self._doctor(repo)["next_action"], "verify_reconciled_deploy"
            )

    def test_wedged_lock_reports_unlock_wedged_runner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                # expired lease, owner still alive, work mid-flight
                acquire_runner_lock(
                    conn, owner=f"user:{os.getpid()}", ttl_minutes=-1
                )
                job = enqueue_job(conn, task="a", branch="feature/a")
                _stage_in_progress(conn, job.id, "t")
            finally:
                conn.close()
            self.assertEqual(self._doctor(repo)["next_action"], "unlock_wedged_runner")


class VerifyRerunTests(unittest.TestCase):
    def _stage_unknown_deploy(self, repo: Path) -> tuple[int, str]:
        config = load_config(repo=repo)
        deploy_sha = git(repo, "rev-parse", "feature/a")
        conn = connect(config.state.db)
        try:
            job = enqueue_job(conn, task="a", branch="feature/a")
            mark_job(
                conn,
                job.id,
                status="deployed",
                deploy_sha=deploy_sha,
                push_status="succeeded",
                verify_status="unknown",
            )
        finally:
            conn.close()
        return job.id, deploy_sha

    def test_verify_without_ack_reruns_hook_at_deploy_sha(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            marker = root / "verify-rerun.txt"
            command = (
                f'{sys.executable} -c "from pathlib import Path; '
                f"Path('{py_path(marker)}').write_text("
                "Path('a.txt').read_text())\""
            )
            repo, _ = make_demo_repo(root, verify_command=command)
            job_id, deploy_sha = self._stage_unknown_deploy(repo)

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "verify", "--json"])
            payload = json.loads(out.getvalue())

            self.assertEqual(code, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "a\n")
            self.assertEqual(
                payload["resolved"],
                [{"job_id": job_id, "verify_status": "succeeded"}],
            )
            conn = connect(load_config(repo=repo).state.db)
            try:
                resolved = get_job(conn, job_id)
            finally:
                conn.close()
            self.assertEqual(resolved.verify_status, "succeeded")
            self.assertIn(deploy_sha, resolved.note)

    def test_verify_without_ack_returns_one_when_hook_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            marker = root / "verify-failed.txt"
            command = (
                f'{sys.executable} -c "from pathlib import Path; '
                f"Path('{py_path(marker)}').write_text('ran'); "
                "raise SystemExit(1)\""
            )
            repo, _ = make_demo_repo(root, verify_command=command)
            job_id, _ = self._stage_unknown_deploy(repo)

            out = io.StringIO()
            with redirect_stdout(out):
                code = main(["--repo", str(repo), "verify", "--json"])
            payload = json.loads(out.getvalue())

            self.assertEqual(code, 1)
            self.assertEqual(marker.read_text(encoding="utf-8"), "ran")
            self.assertEqual(
                payload["resolved"],
                [{"job_id": job_id, "verify_status": "failed"}],
            )

class CommandExitCodeTests(unittest.TestCase):
    def _run(self, repo: Path, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["--repo", str(repo), *argv])
        raw = out.getvalue()
        return code, (json.loads(raw) if raw.strip() else {})

    def test_reconcile_nothing_to_do_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            code, payload = self._run(repo, "reconcile", "--json")
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])

    def test_reconcile_live_lock_exits_three(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                acquire_runner_lock(conn, owner=f"user:{os.getpid()}", ttl_minutes=30)
            finally:
                conn.close()
            code, payload = self._run(repo, "reconcile", "--json")
            self.assertEqual(code, 3)
            self.assertTrue(payload["error"]["retryable"])

    def test_reconcile_remote_unreachable_exits_seven(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                _set_needs_reconcile(conn, job.id, "f" * 40)
            finally:
                conn.close()
            rmtree(root / "remote.git")
            code, payload = self._run(repo, "reconcile", "--apply", "--json")
            self.assertEqual(code, 7)
            self.assertEqual(payload["error"]["code"], "remote_unreachable")

    def test_unlock_no_lock_exits_five(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            code, payload = self._run(repo, "unlock", "--json")
            self.assertEqual(code, 5)
            self.assertFalse(payload["cleared"])
            # Contract 1: the command ran, so ok is true with no error envelope;
            # the exit code + cleared carry the "no lock" outcome.
            self.assertTrue(payload["ok"])
            self.assertNotIn("error", payload)

    def test_unlock_alive_owner_refused_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                acquire_runner_lock(conn, owner=f"user:{os.getpid()}", ttl_minutes=30)
            finally:
                conn.close()
            code, payload = self._run(repo, "unlock", "--json")
            self.assertEqual(code, 4)
            self.assertFalse(payload["cleared"])

    def test_unlock_force_clears_alive_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                acquire_runner_lock(conn, owner=f"user:{os.getpid()}", ttl_minutes=30)
            finally:
                conn.close()
            code, payload = self._run(repo, "unlock", "--force", "--json")
            self.assertEqual(code, 0)
            self.assertTrue(payload["cleared"])
            self.assertIsNotNone(payload["audit_event_id"])
            conn = connect(config.state.db)
            try:
                self.assertIsNone(get_lock(conn))
            finally:
                conn.close()

    def test_unlock_force_aborts_when_remote_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                acquire_runner_lock(conn, owner=f"user:{os.getpid()}", ttl_minutes=30)
            finally:
                conn.close()
            rmtree(root / "remote.git")
            code, _ = self._run(repo, "unlock", "--force", "--json")
            self.assertEqual(code, 7)
            # the lock must be untouched
            conn = connect(config.state.db)
            try:
                self.assertIsNotNone(get_lock(conn))
            finally:
                conn.close()


class ReviewHardeningTests(unittest.TestCase):
    """Regression tests for the adversarial-review findings (Phase 2 hardening)."""

    def test_sibling_ref_shadow_does_not_false_deploy(self) -> None:
        # ls-remote is a tail match: a `refs/tags/main` must never be attributed
        # to the push ref `main` when `refs/heads/main` is absent (finding #6).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                pending = _pending_commit(repo)
                _pin(repo, job.id, pending)
                # remote carries the sha only under refs/tags/main; the push ref
                # refs/heads/main is deleted → the deploy never landed on it.
                git(repo, "push", "origin", f"{pending}:refs/tags/main")
                git(root / "remote.git", "update-ref", "-d", "refs/heads/main")
                _set_needs_reconcile(conn, job.id, pending)
                reconcile(config, conn, apply=True)
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            # Must NOT be deployed off a sibling ref; the branch is absent → requeue.
            self.assertNotEqual(healed.status, "deployed")
            self.assertEqual(healed.status, "queued")

    def test_unresolvable_remote_tip_blocks_not_requeues(self) -> None:
        # A remote_sha that is not a local object => cannot determine containment
        # => blocked (refuse to guess), never a silent requeue (finding #7).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                pending = _pending_commit(repo)
                _pin(repo, job.id, pending)
                conn.execute(
                    "UPDATE deploy_queue SET pending_deploy_sha=? WHERE id=?",
                    (pending, job.id),
                )
                conn.commit()
                job = get_job(conn, job.id)
                decision = _classify(config, job, {"main": "a" * 40})
            finally:
                conn.close()
            self.assertEqual(decision.decision, "blocked")
            self.assertIn("refusing to guess", decision.reason)

    def test_daemon_pauses_deploy_while_reconcile_pending(self) -> None:
        # The daemon deploy path must honor the reconcile hard-block (finding #3).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                parked = enqueue_job(conn, task="a", branch="feature/a")
                _set_needs_reconcile(conn, parked.id, "c" * 40)
                auto = enqueue_job(
                    conn, task="b", branch="feature/b", auto_deploy=True
                )
            finally:
                conn.close()
            processed: list[list[int]] = []

            def spy(conn, jobs):  # type: ignore[no-untyped-def]
                processed.append([j.id for j in jobs])

            daemon_loop(
                db_path=str(config.state.db),
                process_batch=spy,
                once=True,
                say=lambda _m: None,
                install_signal_handlers=False,
            )
            self.assertEqual(processed, [])  # deploy paused; nothing claimed
            conn = connect(config.state.db)
            try:
                self.assertEqual(get_job(conn, auto.id).status, "queued")
            finally:
                conn.close()
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:b.txt")

    def test_force_clear_scoped_to_token_aborts_on_mismatch(self) -> None:
        # unlock --force must not clobber a different runner's lock/jobs that
        # appeared during the remote probe (finding #2).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                lock = acquire_runner_lock(conn, owner="runnerA:1", ttl_minutes=30)
                job = enqueue_job(conn, task="a", branch="feature/a")
                _stage_in_progress(conn, job.id, "tok")
                # a stale token matches nothing → abort, no split, nothing touched
                self.assertFalse(
                    force_clear_lock_and_split(conn, owner="runnerA:1", token="WRONG")
                )
                self.assertEqual(get_job(conn, job.id).status, "in_progress")
                self.assertIsNotNone(get_lock(conn))
                # the authorized token clears the lock and splits orphans
                self.assertTrue(
                    force_clear_lock_and_split(
                        conn, owner="runnerA:1", token=lock.token
                    )
                )
                self.assertIsNone(get_lock(conn))
                self.assertEqual(get_job(conn, job.id).status, "queued")
            finally:
                conn.close()

    def test_gc_sweep_deletes_stale_pins_but_keeps_blocked(self) -> None:
        # Stale pin refs are swept; a reconcile-conflict blocked pin is kept for
        # forensics (finding #1 / decision Q6).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                deployed = enqueue_job(conn, task="d", branch="feature/d")
                blocked = enqueue_job(conn, task="b", branch="feature/b")
                _pin(repo, deployed.id, _pending_commit(repo))
                _pin(repo, blocked.id, _pending_commit(repo))
                mark_job(
                    conn,
                    deployed.id,
                    status="deployed",
                    deploy_sha="a" * 40,
                    push_status="succeeded",
                    verify_status="unknown",
                )
                mark_job(conn, blocked.id, status="blocked", note="reconcile conflict")
                swept = sweep_pending_refs(config, conn)
            finally:
                conn.close()
            refs = _pending_refs(repo).splitlines()
            self.assertIn(pending_ref_name(blocked.id), refs)
            self.assertNotIn(pending_ref_name(deployed.id), refs)
            self.assertTrue(any(s["job_id"] == deployed.id for s in swept))

    def test_recover_gc_removes_orphans_sweeps_pins_and_spares_new_runner(self) -> None:
        from mergetrain.git_runner import apply_gc as real_apply_gc

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            worktree_root = config.state.worktree_root
            worktree_root.mkdir(parents=True, exist_ok=True)
            orphan = worktree_root / f"{config.project.name}-mergetrain-1-orphan"
            live = worktree_root / f"{config.project.name}-mergetrain-2-live"
            orphan.mkdir()
            live.mkdir()
            conn = connect(config.state.db)
            live_lock = None
            try:
                stale = enqueue_job(conn, task="stale", branch="feature/a")
                deploy_sha = git(repo, "rev-parse", "feature/a")
                _pin(repo, stale.id, deploy_sha)
                mark_job(
                    conn,
                    stale.id,
                    status="deployed",
                    deploy_sha=deploy_sha,
                    push_status="succeeded",
                    verify_status="unknown",
                )

                def runner_starts_after_snapshot(
                    config_arg, *, delete_branches=(), protect=(), live_worktree_now=None
                ):  # type: ignore[no-untyped-def]
                    nonlocal live_lock
                    control = connect(config.state.db)
                    try:
                        live_lock = acquire_runner_lock(
                            control,
                            owner=f"runner:{os.getpid()}",
                            ttl_minutes=30,
                            worktree_path=str(live),
                        )
                    finally:
                        control.close()
                    return real_apply_gc(
                        config_arg,
                        delete_branches=delete_branches,
                        protect=protect,
                        live_worktree_now=live_worktree_now,
                    )

                with patch(
                    "mergetrain.recovery.apply_gc",
                    side_effect=runner_starts_after_snapshot,
                ):
                    outcome = recover(config, conn, gc=True)
            finally:
                if live_lock is not None:
                    release_runner_lock(
                        conn, owner=f"runner:{os.getpid()}", token=live_lock.token
                    )
                conn.close()

            self.assertIsNotNone(outcome.gc)
            assert outcome.gc is not None
            self.assertFalse(orphan.exists())
            self.assertTrue(live.exists())
            self.assertTrue(
                any(
                    item["job_id"] == stale.id
                    for item in outcome.gc["swept_pending_refs"]
                )
            )
            self.assertNotIn(pending_ref_name(stale.id), _pending_refs(repo))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
