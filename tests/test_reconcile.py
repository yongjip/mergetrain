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
from mergetrain.git_runner import GitRunner, pending_ref_name
from mergetrain.recovery import _classify, force_unlock, recover, reconcile, sweep_pending_refs
from mergetrain.store import (
    acquire_runner_lock,
    claim_deploy_batch,
    claim_next_job,
    connect,
    counts,
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

from test_git_runner import git, make_demo_repo, rmtree


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

        def push_then_crash(*, worktree, log=None, pulse=None):
            real_push(worktree=worktree, log=log, pulse=pulse)  # the push lands
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
