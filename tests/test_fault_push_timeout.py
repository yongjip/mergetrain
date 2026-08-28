"""Fault case 5: a `git push` that outlives ``queue.command_timeout_seconds``.

Every other ambiguous-push test in this repo *simulates* the transport failure by
patching ``push_verified_head`` to raise a hand-written ``CommandFailed``
(tests/test_git_runner.py::test_push_failure_is_not_reported_as_deployed,
tests/test_reconcile.py::test_ambiguous_push_parks_needs_reconcile_*). Those pin
the decision logic but not the *classification input*: they never let the real
timeout path in ``command_runner._run_managed`` produce the stderr that
``is_push_rejection`` is then asked to judge.

A wedged push is the most common real ambiguous push there is (a hung forge, a
saturated link, a slow server-side hook), and it is the one member of the
ambiguous family that needs no signals, so unlike the SIGKILL cases it runs on
Windows CI too. If the timeout stderr ever started matching one of the rejection
regexes in ``git_runner`` — a new "! [remote rejected]" advice line from a future
git, or a looser pattern added to ``_REF_REJECTION`` — mergetrain would park the
job terminal ``blocked``/``push_status='failed'`` for refs that may well be on
the remote. That is the exact lie the 1.0 gate forbids, and nothing else in the
suite would catch it.

The two variants below differ only in *which* server-side hook sleeps, which is
what decides the ground truth on the remote:

  * ``pre-receive`` sleeps -> the hook never exits 0, so no ref update is
    applied. reconcile must requeue.
  * ``post-receive`` sleeps -> refs are ALREADY applied before the hook runs, so
    the deploy really did land. reconcile must finalize ``deployed`` without a
    second push.

Both variants assert the same three facts in one test body, deliberately coupled
so they fail together: the classifier verdict (``is_push_rejection`` is False for
the captured stderr), the parking decision (``needs_reconcile``, never
``failed``), and the durable marker (``pending_deploy_sha`` + pin ref +
``push_status='pending'``). Splitting them into separate tests would let the
classifier and the parking decision drift apart with a green suite.
"""

from __future__ import annotations

import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# `unittest discover -s tests` puts this dir on sys.path; add it explicitly so a
# single-module run (python -m unittest tests.test_fault_push_timeout) resolves
# the shared bare-remote fixture import below too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mergetrain.atomic_push as atomic_push_module
import mergetrain.git_ops as git_ops_module
from mergetrain.config import MergetrainConfig, load_config
from mergetrain.git_ops import is_push_rejection, pending_ref_name
from mergetrain.git_runner import GitRunner
from mergetrain.recovery import reconcile
from mergetrain.store import (
    claim_deploy_batch,
    connect,
    deploy_reconcile_pending,
    enqueue_job,
    get_job,
)

# A pid that is never live, so the lock the "wedged" runner leaves behind reads
# as DEAD when reconcile checks it (the test process is alive, so it cannot be
# the owner). Same convention as tests/test_reconcile.py.
DEAD_OWNER = "ghost:999999"

# Small enough to keep the file fast, large enough that the local git plumbing
# in the deploy path (fetch/worktree add/merge/gate) never trips it.
PUSH_TIMEOUT_SECONDS = 2

# Comfortably past PUSH_TIMEOUT_SECONDS so the hook is still sleeping when the
# runner kills the push, but bounded so a hook that somehow survives the kill
# cannot outlive the test.
HOOK_SLEEP_SECONDS = 15

TIMEOUT_STDERR_MARKER = f"command timed out after {PUSH_TIMEOUT_SECONDS:g} seconds"

# Reuse the sibling module's bare-remote fixture rather than forking it. Imported
# below the constants so `ruff`'s isort pass sees it as its own block, matching
# tests/test_reconcile.py (tests/** already waive E402 for exactly this).
from test_git_runner import git, make_demo_repo  # noqa: E402


def _install_sleeping_hook(remote: Path, hook: str) -> None:
    """Make the bare remote wedge inside ``hook`` for HOOK_SLEEP_SECONDS.

    Sleeping server-side is the only way to produce a *genuine* timeout kill of
    a real `git push`, which is the whole point of this file: the stderr the
    classifier sees has to come from the product's own timeout path.
    """

    path = remote / "hooks" / hook
    path.parent.mkdir(parents=True, exist_ok=True)
    # LF endings and a /bin/sh shebang: git ships its own sh on Windows and
    # refuses a CRLF shebang line.
    path.write_text(
        f"#!/bin/sh\nsleep {HOOK_SLEEP_SECONDS}\nexit 0\n",
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class PushTimeoutIsAmbiguousTests(unittest.TestCase):
    """A timed-out push must park for reconcile, never report a terminal state."""

    def _shrink_command_timeout(self, repo: Path) -> MergetrainConfig:
        """Rewrite the fixture's timeout through the real config file.

        Mutating the frozen QueueConfig in memory would skip the parse path; a
        tiny timeout has to survive ``load_config`` for this fault to be
        reproducible from a real repo's ``.mergetrain.yaml``.
        """

        path = repo / ".mergetrain.yaml"
        text = path.read_text(encoding="utf-8")
        # Guard against the shared fixture changing its default out from under
        # us: a silent no-op replace would leave a 30s timeout and the push
        # would simply succeed, quietly voiding the whole file.
        self.assertEqual(text.count("command_timeout_seconds: 30"), 1)
        path.write_text(
            text.replace(
                "command_timeout_seconds: 30",
                f"command_timeout_seconds: {PUSH_TIMEOUT_SECONDS}",
            ),
            encoding="utf-8",
        )
        config = load_config(repo=repo)
        self.assertEqual(
            config.queue.command_timeout_seconds, PUSH_TIMEOUT_SECONDS
        )
        return config

    def _deploy_against_wedged_remote(self, root: Path, hook: str):
        """Run one deploy whose push is killed by the command timeout.

        Returns (config, repo, remote, job_id, parked job, classifier calls,
        deploy_reconcile_pending count, open db connection). The caller owns the
        connection and must close it.
        """

        repo, _marker = make_demo_repo(root)
        remote = root / "remote.git"
        config = self._shrink_command_timeout(repo)
        _install_sleeping_hook(remote, hook)

        real_is_push_rejection = git_ops_module.is_push_rejection
        calls: list[tuple[str, bool]] = []

        def spy(stderr: str) -> bool:
            # Pass-through spy: the product's own classifier decides, we only
            # record what it was asked and what it answered. Nothing is stubbed,
            # so a wrong verdict here is a real product verdict.
            verdict = real_is_push_rejection(stderr)
            calls.append((stderr, verdict))
            return verdict

        conn = connect(config.state.db)
        try:
            job = enqueue_job(conn, task="a", branch="feature/a")
            ttl = config.queue.lock_ttl_minutes
            claimed = claim_deploy_batch(conn, owner=DEAD_OWNER, ttl_minutes=ttl)
            self.assertEqual([claim.id for claim in claimed], [job.id])
            with patch.object(atomic_push_module, "is_push_rejection", spy):
                GitRunner(config).process_batch(
                    conn, claimed, deploy=True, owner=DEAD_OWNER, ttl_minutes=ttl
                )
            parked = get_job(conn, job.id)
            pending_blockers = deploy_reconcile_pending(conn)
        except BaseException:
            conn.close()
            raise
        return config, repo, remote, job.id, parked, calls, pending_blockers, conn

    def _assert_parked_ambiguous(self, repo: Path, parked, calls) -> None:
        """The coupled invariant: classifier verdict + parking + durable marker.

        These three live in one assertion block on purpose. `is_push_rejection`
        returning False is the *reason* the job may park ``needs_reconcile``; if
        a future git (or a looser regex) made the timeout stderr look like a
        definitive rejection, the parking decision would flip to terminal
        ``blocked``/``failed`` and this block fails at the first assert, naming
        the cause.
        """

        # The push classifier ran exactly once, on the timeout stderr and not on
        # some earlier unrelated failure.
        self.assertEqual(len(calls), 1, f"unexpected classifier calls: {calls}")
        stderr, verdict = calls[0]
        self.assertIn(TIMEOUT_STDERR_MARKER, stderr)
        self.assertFalse(
            verdict,
            f"timeout stderr was classified as a definitive rejection: {stderr!r}",
        )
        # Re-run the public classifier on the captured bytes, so the assertion
        # also holds for anyone reading is_push_rejection in isolation.
        self.assertFalse(is_push_rejection(stderr))

        # Never a terminal verdict: the refs may be on the remote.
        self.assertNotEqual(parked.status, "failed")
        self.assertNotEqual(parked.status, "deployed")
        self.assertEqual(parked.status, "needs_reconcile")
        self.assertNotEqual(parked.push_status, "failed")
        self.assertNotEqual(parked.push_status, "succeeded")
        self.assertEqual(parked.push_status, "pending")
        self.assertEqual(parked.verify_status, "not_run")

        # The durable marker survived the failure, and the pin ref still resolves
        # to it — without both, a later reconcile has nothing to ask the remote
        # about and would have to guess.
        self.assertNotEqual(parked.pending_deploy_sha, "")
        self.assertEqual(
            git(repo, "rev-parse", pending_ref_name(parked.id)),
            parked.pending_deploy_sha,
        )

        # The operator-facing note must carry the real cause, not just "failed".
        self.assertIn("ambiguous", parked.note.lower())
        self.assertIn(TIMEOUT_STDERR_MARKER, parked.note)

    def test_pre_receive_timeout_parks_then_reconcile_requeues(self) -> None:
        # pre-receive never exits 0, so receive-pack applies no ref update: the
        # deploy truly did not land. The runner cannot know that (its push was
        # killed mid-flight), so it must still park needs_reconcile and let
        # reconcile ask the remote — requeueing for a fresh, single deploy.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (
                config,
                repo,
                remote,
                job_id,
                parked,
                calls,
                pending_blockers,
                conn,
            ) = self._deploy_against_wedged_remote(root, "pre-receive")
            try:
                self._assert_parked_ambiguous(repo, parked, calls)
                self.assertGreater(pending_blockers, 0)

                # Ground truth: nothing landed. Stated positively (git must
                # SUCCEED and report a tree without `a.txt`) rather than as "git
                # show raises", which would also be satisfied by an unrelated
                # git failure — a wrong path, a corrupt remote — and would let
                # this case pass while proving nothing about the remote.
                before = git(remote, "rev-parse", "main")
                remote_files = git(remote, "ls-tree", "--name-only", "main").splitlines()
                self.assertIn("app.txt", remote_files)  # the pre-deploy base
                self.assertNotIn("a.txt", remote_files)  # the merge never landed
                self.assertNotEqual(before, parked.pending_deploy_sha)

                outcome = reconcile(config, conn, apply=True)
                healed = get_job(conn, job_id)
                after = git(remote, "rev-parse", "main")
                cleared_blockers = deploy_reconcile_pending(conn)
            finally:
                conn.close()

            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(outcome.summary["requeued"], 1)
            self.assertEqual(healed.status, "queued")
            # Marker and pin are cleared with the requeue, so the next deploy
            # starts from a clean slate instead of re-reconciling a dead sha.
            self.assertEqual(healed.pending_deploy_sha, "")
            # ...and the requeued attempt carries no residue of the killed one:
            # a leftover push_status='pending' on a queued job would report an
            # in-flight push that no longer exists.
            self.assertEqual(healed.push_status, "not_run")
            # The deploy gate reopens: healing a parked job must clear the
            # blocker it created, or the queue would be wedged forever.
            self.assertEqual(cleared_blockers, 0)
            self.assertEqual(
                git(
                    repo,
                    "for-each-ref",
                    "--format=%(refname)",
                    "refs/mergetrain/pending/",
                ),
                "",
            )
            # reconcile is read-only against the remote; it must never push.
            self.assertEqual(after, before)

    def test_post_receive_timeout_parks_then_reconcile_deploys(self) -> None:
        # post-receive runs AFTER the ref update is committed, so this timeout
        # kills a push that already landed. The dangerous outcome would be
        # calling it failed/blocked and letting a later deploy re-push over a
        # live remote; reconcile must instead finalize `deployed` at the exact
        # recorded sha, with no second push (exactly-once).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (
                config,
                repo,
                remote,
                job_id,
                parked,
                calls,
                pending_blockers,
                conn,
            ) = self._deploy_against_wedged_remote(root, "post-receive")
            try:
                self._assert_parked_ambiguous(repo, parked, calls)
                self.assertGreater(pending_blockers, 0)

                # Ground truth: the refs ARE applied even though the client saw
                # a timeout. This is what makes the ambiguity real rather than
                # theoretical.
                self.assertEqual(git(remote, "show", "main:a.txt"), "a")
                before = git(remote, "rev-parse", "main")
                self.assertEqual(before, parked.pending_deploy_sha)

                outcome = reconcile(config, conn, apply=True)
                healed = get_job(conn, job_id)
                after = git(remote, "rev-parse", "main")
                cleared_blockers = deploy_reconcile_pending(conn)
            finally:
                conn.close()

            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(outcome.summary["reconciled_deployed"], 1)
            self.assertEqual(healed.status, "deployed")
            self.assertEqual(healed.push_status, "succeeded")
            self.assertEqual(healed.deploy_sha, parked.pending_deploy_sha)
            # The refs landed, but the verify hooks never ran — the push was
            # killed first. `deployed` here must therefore NOT claim a verified
            # deploy; 'unknown' is the only honest value, and reporting
            # 'succeeded'/'not_configured' would be the same class of lie as
            # reporting a failed deploy as deployed.
            self.assertEqual(healed.verify_status, "unknown")
            # The parked job stops blocking deploys once it is resolved.
            self.assertEqual(cleared_blockers, 0)
            self.assertEqual(after, before)  # reconcile never re-pushed
            self.assertEqual(
                git(
                    repo,
                    "for-each-ref",
                    "--format=%(refname)",
                    "refs/mergetrain/pending/",
                ),
                "",
            )

    def test_timeout_stderr_is_not_a_definitive_rejection(self) -> None:
        # The unit-level companion to the two end-to-end variants: the literal
        # line command_runner._run_managed appends on a timeout must never satisfy
        # is_push_rejection. Cheap enough to keep as a canary that pins the
        # message and the classifier together, so a reworded timeout line or a
        # widened rejection regex is caught even if the slow-hook fixture is
        # skipped or broken on some platform.
        self.assertFalse(is_push_rejection(f"{TIMEOUT_STDERR_MARKER}\n"))
        self.assertFalse(
            is_push_rejection(
                "Enumerating objects: 4, done.\n"
                "remote: waiting for pre-receive hook\n"
                f"{TIMEOUT_STDERR_MARKER}\n"
            )
        )
        # ...while a real rejection record still classifies, so the assertion
        # above cannot pass merely because the classifier stopped working.
        self.assertTrue(
            is_push_rejection(" ! [remote rejected] HEAD -> main (pre-receive hook declined)\n")
        )


if __name__ == "__main__":  # pragma: no cover - manual run convenience
    unittest.main()
