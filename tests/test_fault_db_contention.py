"""Fault case: SQLite writer contention on the queue DB during a deploy.

The 1.0 gate is "never lie about deployed/failed". ``failed`` is the one status
that tells an agent *the branch is at fault* — rebase it on the integration ref
and enqueue a new job. Writer contention is not the branch's fault: it means a
second process (a concurrent ``enqueue``, a hub write, another repo's runner
sharing the state dir) held SQLite's single write lock longer than
``PRAGMA busy_timeout`` (``store.connect`` sets 5000 ms). Nothing crashed and
nothing is wrong with the code being shipped, so the honest outcome is a
*retryable* one: raise a ``QueueError`` the CLI maps to ``error.code`` /
``lock_held`` / ``lost_lease``, or park the job somewhere non-terminal that a
later run or the lease reaper picks up.

The contended write raises ``sqlite3.OperationalError``, which is **not** a
``MergetrainError``, so it falls through every classified ``except`` clause in
``process_batch`` down to the defensive ``except Exception`` boundary. This
module pins what that boundary is allowed to write.

Two injection points, differing only in *when* the lock is held:

* pre-push — contention on the opening ``mark_job(status='in_progress')``,
  before the remote is touched at all.
* post-push — contention on the terminal ``mark_job(status='deployed')`` after a
  real push landed on the bare remote. This proves the
  ``push_status == 'succeeded'`` guard in ``finish_active_after_error`` converts
  the same ``OperationalError`` into an honest ``deployed`` + warning instead of
  claiming the code never shipped.

One of the four tests is an ``@unittest.expectedFailure`` record of an open defect.
That marker absorbs *every* exception in the test it decorates, so an
expected-failing test can never be trusted to police its own fault injection: if
the injection silently stopped working, the test would still report green. So
each injection point also has an **always-run** test that owns the sentinel —
``note`` must name the lock error — plus the safety invariants that do hold
today. Break the injection and those go red. Keep new assertions on the side of
the always-run test wherever the product is already honest.

The contention is injected deterministically — a holder thread takes the write
lock immediately before the targeted ``mark_job`` and releases it immediately
after that call returns — so there is no sleep-based race and the only wall time
added is one ``busy_timeout`` per run.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch

# Reuse the real bare-remote fixture from the sibling module. `unittest discover
# -s tests` puts this dir on sys.path; add it explicitly so a single-module run
# (python -m unittest tests.test_fault_db_contention) resolves the import too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_runner import git, make_demo_repo

import mergetrain.git_runner as git_runner_module
from mergetrain.config import load_config
from mergetrain.errors import LockHeld, LostLease
from mergetrain.git_runner import GitRunner
from mergetrain.store import (
    claim_deploy_batch,
    connect,
    enqueue_job,
    get_job,
    release_runner_lock,
)

# Short enough that a contended write fails fast, long enough that the holder
# thread has genuinely parked on the lock rather than racing the runner. The
# production value is 5000 ms (store.connect); the defect under test is how the
# resulting OperationalError is *classified*, not how long it waits.
CONTENDED_BUSY_TIMEOUT_MS = 250

# The message SQLite puts in OperationalError when busy_timeout expires. Used as
# the sentinel that the injected fault really reached the error boundary.
LOCK_ERROR_TEXT = "database is locked"


class _WriteLockHolder:
    """A second connection holding SQLite's single write lock, on its own thread.

    Models a concurrent mergetrain process, not an in-process quirk: the runner's
    connection must really park on ``PRAGMA busy_timeout`` and time out. The
    holder never commits — it rolls back — so the blocker leaves no row behind
    and every assertion is about the runner's own writes.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)
        self._acquired = threading.Event()
        self._release = threading.Event()
        self._released = threading.Event()
        self._error: list[BaseException] = []
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            # BEGIN IMMEDIATE takes the write lock at once (verified in WAL
            # mode); no dummy UPDATE is needed to make the block real.
            conn.execute("BEGIN IMMEDIATE")
            self._acquired.set()
            self._release.wait(20)
            conn.rollback()
        except BaseException as exc:  # surfaced by assert_clean() below
            self._error.append(exc)
        finally:
            self._acquired.set()
            conn.close()
            self._released.set()

    def start_holding(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._acquired.wait(20):
            raise AssertionError("write-lock holder never acquired the lock")
        self.assert_clean()

    def stop_holding(self) -> None:
        self._release.set()
        if not self._released.wait(20):
            raise AssertionError("write-lock holder never released the lock")
        assert self._thread is not None
        self._thread.join(20)
        self.assert_clean()

    def assert_clean(self) -> None:
        if self._error:
            raise AssertionError(f"write-lock holder failed: {self._error[0]!r}")


def _contend_on_first(holder: _WriteLockHolder, target_status: str):
    """Wrap ``store.mark_job`` so exactly one call runs under a held write lock.

    The lock is taken right before the first ``mark_job`` whose ``status`` equals
    ``target_status`` and released right after it returns or raises. That makes
    the failure deterministic and, crucially, frees the lock before
    ``process_batch``'s error boundary tries its own write — so the test observes
    the status the boundary *chose*, not a second cascading lock error.
    """

    real_mark_job = git_runner_module.mark_job
    state = {"tripped": False}

    def wrapper(conn, job_id, **kwargs):
        if not state["tripped"] and kwargs.get("status") == target_status:
            state["tripped"] = True
            holder.start_holding()
            try:
                return real_mark_job(conn, job_id, **kwargs)
            finally:
                holder.stop_holding()
        return real_mark_job(conn, job_id, **kwargs)

    return wrapper, state


class _Outcome(NamedTuple):
    """What one contended deploy produced."""

    job: object  # the final Job row
    raised: Exception | None  # a retryable QueueError, if the runner surfaced one
    repo: Path
    remote_before: str  # remote 'main' sha captured before process_batch ran


class DeployUnderWriterContentionTests(unittest.TestCase):
    """What process_batch's ``except Exception`` boundary may write."""

    def _run_contended_deploy(self, root: Path, *, target_status: str) -> _Outcome:
        """Claim and deploy one job with the write lock held across one mark_job."""

        repo, _marker = make_demo_repo(root)
        config = load_config(repo=repo)
        owner = f"runner:{os.getpid()}"
        conn = connect(config.state.db)
        # Keep the contended wait short; see CONTENDED_BUSY_TIMEOUT_MS.
        conn.execute(f"PRAGMA busy_timeout = {CONTENDED_BUSY_TIMEOUT_MS}")
        token = ""
        raised: Exception | None = None
        remote_before = git(root / "remote.git", "rev-parse", "main")
        try:
            job = enqueue_job(conn, task="a", branch="feature/a")
            # Claim through the real deploy entrypoint: the CLI never hands
            # process_batch an unclaimed job, and the claim is what makes the
            # error boundary's ownership check (in_progress + matching token)
            # true — i.e. what lets it write a terminal status at all.
            claimed = claim_deploy_batch(conn, owner=owner)
            self.assertEqual([item.id for item in claimed], [job.id])
            token = claimed[0].claim_token
            holder = _WriteLockHolder(config.state.db)
            wrapper, state = _contend_on_first(holder, target_status)
            with patch("mergetrain.git_runner.mark_job", wrapper):
                try:
                    GitRunner(config).process_batch(
                        conn, claimed, deploy=True, owner=owner
                    )
                except (LockHeld, LostLease) as exc:
                    # Acceptable: a classified, retryable QueueError. The CLI
                    # maps these to error.code lock_held / lost_lease and the
                    # job stays claimable instead of being blamed.
                    raised = exc
            self.assertTrue(
                state["tripped"],
                f"no mark_job(status={target_status!r}) was ever contended; "
                "the injection point moved and this case is no longer testing "
                "writer contention",
            )
            holder.assert_clean()
            final = get_job(conn, job.id)
        finally:
            if token:
                # Best-effort: the lock lives in the temp DB that is about to be
                # deleted, so a failure here cannot affect any assertion.
                try:
                    release_runner_lock(conn, owner=owner, token=token)
                except Exception:
                    pass
            conn.close()
        return _Outcome(job=final, raised=raised, repo=repo, remote_before=remote_before)

    # ------------------------------------------------------------------ pre-push

    def test_prepush_writer_contention_leaves_the_remote_untouched(self) -> None:
        """Always-run half of the pre-push case: the fault lands, the remote does not.

        This test owns the sentinel for the whole pre-push injection. Its sibling
        below is @expectedFailure, which swallows every exception it sees, so the
        sibling cannot detect that the fault injection stopped working — verified
        by mutation: neuter _WriteLockHolder and the sibling still reports
        'expected failure' while this test goes red on the note assertion.
        """

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outcome = self._run_contended_deploy(root, target_status="in_progress")
            final = outcome.job

            # SENTINEL: the contended write really raised, and the resulting
            # sqlite3.OperationalError really reached process_batch's error
            # boundary. Without this the pre-push pair could silently degrade
            # into a plain successful deploy that asserts nothing.
            self.assertIn(
                LOCK_ERROR_TEXT,
                final.note,
                f"the injected write-lock contention never reached the error "
                f"boundary (note={final.note!r}, status={final.status!r}); the "
                "fault injection is broken, so the sibling expectedFailure case "
                "is no longer testing anything",
            )

            # Nothing was pushed: the contention hit before the write-ahead
            # marker, so the remote must be byte-for-byte where it started.
            # Asserting the sha (not merely "git show fails") keeps an unrelated
            # git error from being mistaken for an untouched remote.
            self.assertEqual(
                git(root / "remote.git", "rev-parse", "main"),
                outcome.remote_before,
                "a pre-push failure advanced the remote's integration ref",
            )
            self.assertEqual(final.push_status, "not_run")
            self.assertEqual(final.deploy_sha, "")

            # No recovery markers may be left behind for a deploy that never
            # touched the remote: a phantom pin or pending sha would make every
            # later deploy entrypoint refuse, and would send recovery.reconcile()
            # asking the remote about a push that was never attempted.
            self.assertEqual(final.pending_deploy_sha, "")
            self.assertEqual(
                git(
                    outcome.repo,
                    "for-each-ref",
                    "--format=%(refname)",
                    "refs/mergetrain/pending/",
                ),
                "",
            )

    # OPEN DEFECT (expectedFailure): pure pre-push writer contention is written
    # as terminal 'failed'. src/mergetrain/git_runner.py:2386 — process_batch's
    # `except Exception` boundary catches the sqlite3.OperationalError raised by
    # the opening _mark_job(status='in_progress') (git_runner.py:1998) and calls
    # finish_active_after_error(status='failed', note='unexpected error: database
    # is locked'). sqlite3.OperationalError is not a MergetrainError, so it never
    # reaches the `except MergetrainError` -> 'blocked' clause, and there is no
    # clause that recognises lock contention as retryable at all. process_one has
    # the identical boundary at git_runner.py:1564-1565.
    #
    # Why it matters: 'failed' is the queue's "the branch is at fault, rebase and
    # re-enqueue" signal (see PushRejected's docstring in errors.py, which parks
    # 'blocked' precisely to avoid this confusion). Here nothing crashed, no ref
    # was touched, and the branch is fine — a second process just held the write
    # lock for longer than busy_timeout. The job is retired terminally (store.py
    # :1601 stamps finished_at and the claim token is cleared) and an agent is
    # sent to rewrite innocent code.
    #
    # Do not "fix" this by weakening the assertion. The fix belongs in src/:
    # classify sqlite3.OperationalError('database is locked') as a retryable
    # QueueError (LockHeld) before the defensive boundary sees it. When that
    # lands, this test starts passing, unittest reports an unexpected success
    # (which fails the run), and the marker should be deleted — not re-added.
    @unittest.expectedFailure
    def test_prepush_writer_contention_is_not_the_branch_fault(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outcome = self._run_contended_deploy(root, target_status="in_progress")
            final = outcome.job

            # The gate: contention must never be reported as the branch's fault.
            self.assertNotEqual(
                final.status,
                "failed",
                f"writer contention was retired as terminal 'failed' "
                f"(note={final.note!r}); 'failed' tells an agent to rebase and "
                "re-enqueue a branch that is not at fault",
            )
            self.assertIn(
                final.status,
                ("queued", "in_progress", "needs_reconcile"),
                f"expected a retryable parking, got {final.status!r} "
                f"(note={final.note!r}, raised={outcome.raised!r})",
            )
            # And the note must not read like a code failure.
            self.assertNotIn("unexpected error", final.note)

    # ----------------------------------------------------------------- post-push

    def test_postpush_writer_contention_still_reports_the_landed_deploy(self) -> None:
        # The dangerous half: the push has already landed on the remote when the
        # terminal write is contended. If this ever regressed to 'failed', the
        # queue would claim the code did not ship while main already carries it —
        # and a re-enqueue would re-push over an advanced ref, breaking
        # exactly-once (guarantee #4). The push_status == 'succeeded' guard in
        # finish_active_after_error (git_runner.py:1964) is what holds the line;
        # deleting that guard makes this test fail on the assertion below
        # (verified by mutation).
        #
        # Distinct from tests/test_git_runner.py:583, which crashes a verify hook
        # after the push: here the failing operation is the terminal DB write
        # itself, so this also pins that the boundary can re-do the write it was
        # handed a lock error on.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outcome = self._run_contended_deploy(root, target_status="deployed")
            final = outcome.job

            # Ground truth first: the remote really carries the branch's commit,
            # and really moved off where it started.
            self.assertEqual(git(root / "remote.git", "show", "main:a.txt"), "a")
            self.assertNotEqual(
                git(root / "remote.git", "rev-parse", "main"), outcome.remote_before
            )
            # SENTINEL: the contention reached the boundary rather than the
            # deploy simply succeeding.
            self.assertIn(LOCK_ERROR_TEXT, final.note)

            self.assertIsNone(outcome.raised)
            self.assertNotEqual(
                final.status,
                "failed",
                f"a landed push was reported as 'failed' (note={final.note!r}); "
                "the remote already advanced, so this is a lie about whether "
                "code shipped",
            )
            self.assertIn(final.status, ("deployed", "needs_reconcile"))
            if final.status == "deployed":
                # Honest 'deployed': push_status records the landing, the sha is
                # the one that landed, and the note keeps the contention visible
                # instead of silently swallowing it.
                self.assertEqual(final.push_status, "succeeded")
                self.assertEqual(
                    final.deploy_sha, git(root / "remote.git", "rev-parse", "main")
                )
                self.assertIn("post-push completion warning", final.note)
                # A finalized deploy drops its recovery markers, otherwise every
                # later deploy entrypoint would refuse on a phantom reconcile.
                self.assertEqual(final.pending_deploy_sha, "")
                self.assertEqual(
                    git(
                        outcome.repo,
                        "for-each-ref",
                        "--format=%(refname)",
                        "refs/mergetrain/pending/",
                    ),
                    "",
                )
            else:
                # The other acceptable answer: park for reconcile with the
                # marker intact, so recovery asks the remote for truth.
                self.assertEqual(final.push_status, "pending")
                self.assertTrue(final.pending_deploy_sha)

    # WAS AN OPEN DEFECT, fixed: the post-push guard overwrote verify_status
    # with 'failed' unconditionally — src/mergetrain/git_runner.py:1967 (and the
    # identical line in process_one, git_runner.py:1389) set
    # post_push_verify_status = 'failed' without regard for what the push path
    # already established. make_demo_repo configures `verify: []`, so
    # _push_and_verify had set state.verify_status = 'not_configured'
    # (git_runner.py:1255); an uncontended deploy on this same fixture finishes
    # with verify_status='not_configured'. After the contention it reads 'failed'.
    #
    # Why it matters: verify_status is a contract field surfaced by
    # `status --json` and the dashboard, so this reports a *failed verification*
    # on a repo that has none — sending an operator hunting a hook that does not
    # exist. The vocabulary already has the honest answer: models.py:14 lists
    # 'unknown', and store.py:646 has a reconcile query keyed on
    # `status='deployed' AND verify_status='unknown'` for exactly the "we could
    # not determine it" case. 'not_configured' (unchanged) or 'unknown' are both
    # honest here; 'failed' is not.
    #
    # The overall outcome stays honest (deployed + a visible warning), which is
    # why the test above passes — this was the narrower lie inside it, now fixed
    # by _post_push_verify_status (git_runner.py): not_configured and succeeded
    # are preserved, and anything indeterminate becomes 'unknown', the value
    # doctor turns into next_action verify_reconciled_deploy.
    def test_postpush_contention_does_not_invent_a_failed_verification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outcome = self._run_contended_deploy(root, target_status="deployed")
            final = outcome.job

            self.assertIn(
                final.verify_status,
                ("not_configured", "unknown"),
                f"verify_status={final.verify_status!r} on a repo that configures "
                "no verify hooks; a verification that never ran cannot have failed",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
