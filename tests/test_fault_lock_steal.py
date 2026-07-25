"""Fault case: ``unlock --force`` steals the lock in the post-push window.

The highest-stakes concurrency window in the product. A runner has already run
``git push --atomic`` (the remote is advanced, irreversibly) but has not yet
written its terminal ``deployed`` row. An operator, following the documented
wedge remedy (``mergetrain unlock --force``), clears the lock in exactly that
window: ``recovery.force_clear_lock_and_split`` parks the marker-bearing row
``needs_reconcile`` and clears its ``claim_token``.

What must hold, and what these tests pin:

* the runner's own ``mark_job(deployed)`` must LOSE the claim-token fence — it
  must not resurrect ``deployed`` on a row an operator just parked, because the
  deploy is no longer this runner's to finalize;
* the loss must surface as ``LostLease`` all the way out to the CLI envelope
  (exit 1, ``error.code == "lost_lease"``, ``retryable == true``), so an
  automated caller retries instead of treating the run as a hard failure;
* no path may write terminal ``failed`` (the push DID land — calling it failed
  is the one lie this tool promises never to tell) and no path may strand an
  ``in_progress`` claim;
* the durable evidence (``pending_deploy_sha`` + pin ref) must survive the
  steal, so the later ``reconcile`` can ask the remote and finalize ``deployed``
  with ``verify_status = 'unknown'`` — without a second push.

Regression risk this guards: drop ``expected_claim_token`` from the runner's
finalize (or widen ``mark_job``'s fence to allow ``needs_reconcile`` as a source
status) and the runner silently overwrites the operator's park with
``deployed``/``verify_status='succeeded'`` — a deploy the remote may or may not
have taken, recorded as verified. Route ``LostLease`` into
``finish_active_after_error`` instead of re-raising it and the same window ends
as terminal ``failed`` on a push that actually landed.
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

# `unittest discover -s tests` puts this dir on sys.path; add it explicitly so a
# single-module run (python -m unittest tests.test_fault_lock_steal) resolves the
# shared bare-remote fixture import below too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_runner import git, make_demo_repo

from mergetrain import recovery as recovery_module
from mergetrain.cli import main
from mergetrain.config import load_config
from mergetrain.errors import LostLease
from mergetrain.git_runner import GitRunner, pending_ref_name
from mergetrain.recovery import force_unlock, reconcile
from mergetrain.store import (
    claim_deploy_batch,
    connect,
    deploy_reconcile_pending,
    enqueue_job,
    force_clear_lock_and_split,
    get_job,
    get_lock,
    list_run_events,
    mark_job,
    record_pending_push,
    utc_now,
)


def _pending_refs(repo: Path) -> str:
    return git(repo, "for-each-ref", "--format=%(refname)", "refs/mergetrain/pending/")


class _GitSpy:
    """Record the git subcommands ``recovery`` runs, delegating to the real call.

    Comparing the remote tip before/after reconcile cannot prove "never
    re-pushes a landed deploy": re-pushing the *same* sha leaves rev-parse
    identical, so that assertion alone passes a reconcile that pushes again.
    Watch the subcommands instead — reconcile's own git seam must stay read-only
    (ls-remote / fetch / merge-base).
    """

    def __init__(self) -> None:
        self.subcommands: list[str] = []
        self._real = recovery_module.run_command

    def __call__(self, args, **kwargs):
        argv = list(args)
        if len(argv) > 1 and argv[0] == "git":
            self.subcommands.append(argv[1])
        return self._real(args, **kwargs)


class ForceUnlockInPostPushWindowTests(unittest.TestCase):
    """The runner is mid-``push_verified_head``; the operator steals the lock."""

    def _live_owner(self) -> str:
        # This process is alive, so the lock reads ALIVE — the wedge case that
        # `unlock` refuses to clear without --force.
        return f"runner:{os.getpid()}"

    def test_steal_after_landed_push_fences_runner_then_reconcile_deploys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            remote = root / "remote.git"
            config = load_config(repo=repo)
            owner = self._live_owner()
            unlock_outcomes = []

            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                ttl = config.queue.lock_ttl_minutes
                claimed = claim_deploy_batch(conn, owner=owner, ttl_minutes=ttl)
                self.assertEqual([j.id for j in claimed], [job.id])
                lease_token = claimed[0].claim_token
                runner = GitRunner(config)
                real_push = runner.push_verified_head

                def land_then_steal(*, worktree, deploy_sha="", log=None, pulse=None):
                    # (1) the real push: from here on the remote is advanced and
                    # the deploy is irreversible.
                    real_push(
                        worktree=worktree, deploy_sha=deploy_sha, log=log, pulse=pulse
                    )
                    # (2) the operator runs the documented wedge remedy from a
                    # separate connection, exactly in the window before the
                    # runner's terminal write.
                    control = connect(config.state.db)
                    try:
                        # A plain `unlock` must refuse an alive owner: the steal
                        # is only ever an explicit --force decision.
                        refused = force_unlock(config, control, force=False)
                        unlock_outcomes.append(refused)
                        unlock_outcomes.append(force_unlock(config, control, force=True))
                    finally:
                        control.close()

                with patch.object(
                    runner, "push_verified_head", side_effect=land_then_steal
                ):
                    # The runner must not quietly "succeed" here: its lease is
                    # gone, so its finalize has to fail loudly and retryably.
                    with self.assertRaises(LostLease):
                        runner.process_batch(
                            conn, claimed, deploy=True, owner=owner, ttl_minutes=ttl
                        )

                refused, forced = unlock_outcomes
                self.assertFalse(refused.cleared)
                self.assertEqual(refused.exit_code, 4)
                self.assertTrue(forced.cleared)
                self.assertEqual(forced.exit_code, 0)
                # The operator is told they stole from a runner that had already
                # recorded a push marker — the honest, actionable signal.
                self.assertEqual(forced.context["in_progress_with_marker"], 1)

                parked = get_job(conn, job.id)
                pending_sha = parked.pending_deploy_sha

                # The operator's park wins; the runner did NOT resurrect
                # deployed, and did not lie the other way with terminal failed
                # either (the push landed — see the remote assertions below).
                self.assertEqual(parked.status, "needs_reconcile")
                # push_status stays 'pending': the remote outcome is not this
                # process's to declare any more.
                self.assertEqual(parked.push_status, "pending")
                self.assertEqual(parked.verify_status, "not_run")
                # No terminal deploy_sha was written, and the claim is not
                # stranded on a dead lease.
                self.assertEqual(parked.deploy_sha, "")
                self.assertEqual(parked.claim_token, "")
                self.assertIn("reconcile", parked.note)
                # Durable evidence survives the steal: marker + pin ref.
                self.assertNotEqual(pending_sha, "")
                self.assertIn(pending_ref_name(job.id), _pending_refs(repo).splitlines())
                # The lock really is gone — the runner never re-acquired it.
                self.assertIsNone(get_lock(conn))
                # The window is fenced against further deploys until reconciled.
                self.assertGreater(deploy_reconcile_pending(conn), 0)
                # And the event stream never claimed completion either.
                phases = {
                    event.phase
                    for event in list_run_events(
                        conn, claim_token=lease_token, limit=200
                    )
                }
                self.assertIn("pushing", phases)
                self.assertNotIn("complete", phases)

                # The push really did land — that is what makes 'failed' a lie.
                self.assertEqual(git(remote, "show", "main:a.txt"), "a")
                self.assertEqual(git(remote, "rev-parse", "main"), pending_sha)

                # Now the remote-verified verdict: reconcile finalizes deployed
                # WITHOUT pushing again (exactly-once), and is honest that it can
                # prove the push landed but not that verify ran.
                before = git(remote, "rev-parse", "main")
                spy = _GitSpy()
                with patch.object(
                    recovery_module, "run_command", side_effect=spy
                ):
                    outcome = reconcile(config, conn, apply=True)
                after = git(remote, "rev-parse", "main")
                healed = get_job(conn, job.id)
            finally:
                conn.close()

            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(outcome.summary["reconciled_deployed"], 1)
            self.assertEqual(healed.status, "deployed")
            self.assertEqual(healed.deploy_sha, pending_sha)
            self.assertEqual(healed.push_status, "succeeded")
            self.assertEqual(healed.verify_status, "unknown")
            self.assertEqual(healed.pending_deploy_sha, "")
            # Exactly-once, two ways: the verdict came from asking the remote,
            # and reconcile's git seam never pushed. rev-parse alone cannot show
            # the second half (a re-push of the same sha is invisible there).
            self.assertIn("ls-remote", spy.subcommands)
            self.assertNotIn("push", spy.subcommands)
            self.assertEqual(after, before)
            self.assertEqual(_pending_refs(repo), "")

    def test_cli_run_batch_reports_lost_lease_as_retryable(self) -> None:
        # Same window, driven through the real CLI so the failure envelope is
        # pinned: an automated caller must see a retryable lost_lease, not a
        # generic failure and not ok:true. The operator side goes through the
        # real `unlock --force` command too.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            remote = root / "remote.git"
            config = load_config(repo=repo)
            unlock_payloads = []

            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
            finally:
                conn.close()

            real_push = GitRunner.push_verified_head

            def land_then_steal(runner_self, **kwargs):
                real_push(runner_self, **kwargs)
                out = io.StringIO()
                with redirect_stdout(out):
                    code = main(
                        ["--repo", str(repo), "unlock", "--force", "--json"]
                    )
                unlock_payloads.append((code, json.loads(out.getvalue())))

            out = io.StringIO()
            with patch.object(
                GitRunner, "push_verified_head", autospec=True,
                side_effect=land_then_steal,
            ):
                with redirect_stdout(out):
                    code = main(["--repo", str(repo), "run-batch", "--deploy", "--json"])
            payload = json.loads(out.getvalue())

            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "lost_lease")
            self.assertTrue(payload["error"]["retryable"])

            unlock_code, unlock_payload = unlock_payloads[0]
            self.assertEqual(unlock_code, 0)
            self.assertTrue(unlock_payload["cleared"])
            self.assertEqual(
                unlock_payload["lock_context"]["in_progress_with_marker"], 1
            )
            # unlock itself never renders a deploy verdict; it points at reconcile.
            self.assertEqual(unlock_payload["next_action"], "reconcile_pending_deploy")

            conn = connect(config.state.db)
            try:
                parked = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(parked.status, "needs_reconcile")
            self.assertEqual(parked.push_status, "pending")
            self.assertNotEqual(parked.pending_deploy_sha, "")
            self.assertEqual(git(remote, "rev-parse", "main"), parked.pending_deploy_sha)

            # The wedge is now the operator's to resolve, and doctor says so.
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--repo", str(repo), "doctor", "--json"])
            self.assertEqual(
                json.loads(out.getvalue())["next_action"], "reconcile_pending_deploy"
            )


class ClaimTokenFenceTests(unittest.TestCase):
    """The store-level fence the runner's finalize depends on, isolated.

    No git, no runner: if ``mark_job``'s claim-token/status fence ever widens to
    accept a parked row, the end-to-end test above would still be the only thing
    catching it. Pin the primitive directly.
    """

    def test_parked_row_rejects_the_old_lease_terminal_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "queue.sqlite")
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                token = "lease-token"
                conn.execute(
                    "UPDATE deploy_queue SET status='in_progress', claim_token=?, "
                    "started_at=? WHERE id=?",
                    (token, utc_now(), job.id),
                )
                conn.commit()
                record_pending_push(
                    conn,
                    job_ids=[job.id],
                    deploy_sha="a" * 40,
                    claim_token=token,
                    remote="origin",
                    push_refs=("main",),
                )

                # The operator's forced steal: lock gone, marker-bearing row
                # parked, claim token cleared.
                self.assertTrue(force_clear_lock_and_split(conn))
                parked = get_job(conn, job.id)
                self.assertEqual(parked.status, "needs_reconcile")
                self.assertEqual(parked.claim_token, "")
                self.assertEqual(parked.pending_deploy_sha, "a" * 40)
                self.assertEqual(parked.push_status, "pending")

                # The old lease's terminal write must be refused, and must leave
                # the parked row byte-for-byte intact.
                with self.assertRaises(LostLease):
                    mark_job(
                        conn,
                        job.id,
                        status="deployed",
                        deploy_sha="a" * 40,
                        push_status="succeeded",
                        verify_status="succeeded",
                        note="runner finalize after the steal",
                        expected_claim_token=token,
                    )
                after = get_job(conn, job.id)
                self.assertEqual(after, parked)

                # A 'failed' write from the same dead lease is refused too: the
                # push may have landed, so only reconcile may rule on it.
                with self.assertRaises(LostLease):
                    mark_job(
                        conn,
                        job.id,
                        status="failed",
                        note="runner error after the steal",
                        expected_claim_token=token,
                    )
                self.assertEqual(get_job(conn, job.id), parked)
            finally:
                conn.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
