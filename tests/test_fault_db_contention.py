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

Both defects this module recorded are now fixed, so nothing here is
``@unittest.expectedFailure`` any more. The structure that caught them stays:
each injection point has a test that owns the **sentinel** — ``note`` must name
the lock error — so if the injection silently stopped working the pair goes red
instead of reporting a vacuous green. Keep new assertions on the side of
the always-run test wherever the product is already honest.

The contention is injected deterministically — a holder thread takes the write
lock immediately before the targeted ``mark_job`` and releases it immediately
after that call returns — so there is no sleep-based race and the only wall time
added is one ``busy_timeout`` per run.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import NamedTuple
from unittest.mock import patch

# Reuse the real bare-remote fixture from the sibling module. `unittest discover
# -s tests` puts this dir on sys.path; add it explicitly so a single-module run
# (python -m unittest tests.test_fault_db_contention) resolves the import too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_runner import git, make_demo_repo

import mergetrain.atomic_push as atomic_push_module
import mergetrain.cli as cli_module
import mergetrain.git_runner as git_runner_module
from mergetrain.cli import main
from mergetrain.config import load_config
from mergetrain.errors import LockHeld, LostLease, QueueBusy
from mergetrain.git_runner import GitRunner
from mergetrain.recovery import reconcile
from mergetrain.store import (
    claim_deploy_batch,
    claim_next_job,
    connect,
    enqueue_job,
    get_job,
    immediate,
    record_pending_push,
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
                except (LockHeld, LostLease, QueueBusy) as exc:
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

        This test owns the sentinel for the whole pre-push injection, so if the
        injection ever stops working its sibling cannot silently pass on a plain
        successful deploy.
        """

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outcome = self._run_contended_deploy(root, target_status="in_progress")
            final = outcome.job

            # SENTINEL: the contended write really raised, and the resulting
            # error really reached the runner. It has to be asserted on the
            # RAISED error rather than on the job's note, because writing a note
            # is exactly what the fix stops doing -- see the sibling below.
            self.assertIsInstance(outcome.raised, QueueBusy)
            self.assertIn(LOCK_ERROR_TEXT, str(outcome.raised))

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

    # WAS AN OPEN DEFECT, fixed: pure pre-push writer contention used to be
    # written as terminal 'failed'. sqlite3.OperationalError is not a
    # MergetrainError, so it fell past every classified clause to
    # process_batch's defensive `except Exception` and was retired with
    # note='unexpected error: database is locked'. 'failed' is the queue's "the
    # branch is at fault, rebase and re-enqueue" signal (see PushRejected's
    # docstring, which parks 'blocked' precisely to avoid this confusion), so an
    # agent was sent to rewrite innocent code over a second process holding the
    # write lock past busy_timeout.
    #
    # store.immediate() now translates contention into QueueBusy, a retryable
    # QueueError, and both runner ladders park on it instead of blaming the
    # branch: 'deployed' when the refs already landed, 'needs_reconcile' when the
    # durable marker was written and the outcome is unknown, otherwise back to
    # 'queued'. Contention on the opening mark_job -- before the runner's own
    # ladder exists -- surfaces the retryable error to the caller with the row
    # untouched, which is what this case exercises.
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
        # finish_active_after_error is what holds the line;
        # deleting that guard makes this test fail on the assertion below
        # (verified by mutation).
        #
        # Distinct from the verify-hook crash regression in test_git_runner,
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
    # with 'failed' unconditionally — the exception boundaries in both the
    # batch and single-job orchestration paths set
    # post_push_verify_status = 'failed' without regard for what the push path
    # already established. make_demo_repo configures `verify: []`, so
    # AtomicPush.deploy_and_verify had set state.verify_status to
    # 'not_configured' after the landed push; an uncontended deploy on this same
    # fixture finishes with verify_status='not_configured'. After the contention
    # it reads 'failed'.
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


class ContentionNeverDestroysEvidenceTests(unittest.TestCase):
    """The regressions an adversarial review found in the first version of this fix.

    v1 parked the job on contention, choosing the status from the in-memory
    ``_PushVerifyState``. Both halves of that were wrong, and both were caught
    with real reproductions:

    * The state can belong to a **different frame**. ``_process_isolated_jobs``
      runs ``process_one`` inside ``process_batch``'s own try, so a nested job's
      contention was graded against the batch's ``push_status`` -- ``not_run``
      even when the nested job's refs had just landed -- and requeued it. The
      requeue then ran ``mark_job``, which CLEARS ``pending_deploy_sha`` and the
      deploy sha, so reconcile had nothing left to find and a retry re-ran the
      verify hooks for an already-shipped commit.
    * The state can be **optimistic**. ``push_status`` is set to ``pending``
      before the marker write it describes, so contention on that very write
      parked ``needs_reconcile`` with no marker -- a row that hard-blocks every
      deploy entrypoint while ``reconcile --apply`` reports an unresolvable pin
      that never existed.

    The fix writes nothing unless this frame's own push is known to have landed,
    so these assert on durable evidence surviving, not on a chosen status.
    """

    def test_contention_never_clears_a_durable_marker(self) -> None:
        # If a marker was written, contention must leave it: it is the only
        # record that lets reconcile ask the remote what happened.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                claimed = claim_deploy_batch(conn, owner=f"runner:{os.getpid()}")
                # Stand in for "the marker write succeeded, a later write did
                # not": record_pending_push is the only writer of the marker.
                record_pending_push(
                    conn,
                    job_ids=[claimed[0].id],
                    deploy_sha="a" * 40,
                    claim_token=claimed[0].claim_token,
                    remote="origin",
                    push_refs=["main"],
                )
                before = get_job(conn, job.id)
                self.assertEqual(before.pending_deploy_sha, "a" * 40)

                holder = _WriteLockHolder(config.state.db)
                real_mark_job = git_runner_module.mark_job

                def wrapper(conn_arg, job_id, **kwargs):
                    holder.start_holding()
                    try:
                        return real_mark_job(conn_arg, job_id, **kwargs)
                    finally:
                        holder.stop_holding()

                with patch("mergetrain.git_runner.mark_job", wrapper):
                    with self.assertRaises(QueueBusy):
                        GitRunner(config).process_batch(
                            conn, claimed, deploy=True, owner=f"runner:{os.getpid()}"
                        )
                holder.assert_clean()
                after = get_job(conn, job.id)
            finally:
                conn.close()

            # The evidence survives, and nothing terminal was written.
            self.assertEqual(after.pending_deploy_sha, "a" * 40)
            self.assertNotEqual(after.status, "failed")
            self.assertNotIn(after.status, ("deployed", "canceled"))

    def test_a_markerless_row_is_never_parked_needs_reconcile(self) -> None:
        # needs_reconcile with no marker hard-blocks every deploy entrypoint and
        # makes reconcile report an unresolvable pin that never existed. Only
        # recover_orphans may make that call, and only from durable state.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outcome = self._run_marker_write_contended(root)
            self.assertIsInstance(outcome[1], QueueBusy)
            final = outcome[0]
            self.assertNotEqual(
                (final.status, final.pending_deploy_sha),
                ("needs_reconcile", ""),
                "parked needs_reconcile with no durable marker",
            )
            self.assertNotEqual(final.status, "failed")

    def _run_marker_write_contended(self, root: Path):
        """Contend the write-ahead marker itself (record_pending_push)."""

        repo, _marker = make_demo_repo(root)
        config = load_config(repo=repo)
        conn = connect(config.state.db)
        conn.execute(f"PRAGMA busy_timeout = {CONTENDED_BUSY_TIMEOUT_MS}")
        raised: Exception | None = None
        try:
            job = enqueue_job(conn, task="a", branch="feature/a")
            claimed = claim_deploy_batch(conn, owner=f"runner:{os.getpid()}")
            holder = _WriteLockHolder(config.state.db)
            real_record = atomic_push_module.record_pending_push

            def wrapper(*args, **kwargs):
                holder.start_holding()
                try:
                    return real_record(*args, **kwargs)
                finally:
                    holder.stop_holding()

            with patch("mergetrain.atomic_push.record_pending_push", wrapper):
                try:
                    GitRunner(config).process_batch(
                        conn, claimed, deploy=True, owner=f"runner:{os.getpid()}"
                    )
                except QueueBusy as exc:
                    raised = exc
            holder.assert_clean()
            return get_job(conn, job.id), raised
        finally:
            conn.close()


class _CommitRaises:
    """A connection whose COMMIT fails. sqlite3.Connection.commit is read-only,
    and ``immediate()`` only ever calls execute / commit / rollback, so a narrow
    stand-in is enough to reach its commit branch."""

    def __init__(self, conn, exc: BaseException) -> None:
        self._conn = conn
        self._exc = exc
        self.commits = 0
        self.rollbacks = 0

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self) -> None:
        self.commits += 1
        raise self._exc

    def rollback(self) -> None:
        self.rollbacks += 1
        self._conn.rollback()


class ContentionTranslationTests(unittest.TestCase):
    """Unit coverage for the pieces the end-to-end cases cannot reach.

    An adversarial review found each of these deletable with a green suite,
    which for a translation layer means it was not tested at all: the whole
    point is that a raw sqlite3.OperationalError must never escape into code
    that treats it as an unexpected crash.
    """

    def _db(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return Path(td.name) / "queue.sqlite"

    def test_begin_translates_contention(self) -> None:
        db = self._db()
        conn = connect(db)
        conn.execute(f"PRAGMA busy_timeout = {CONTENDED_BUSY_TIMEOUT_MS}")
        holder = _WriteLockHolder(db)
        holder.start_holding()
        try:
            with self.assertRaises(QueueBusy) as raised:
                with immediate(conn):
                    conn.execute("SELECT 1")
        finally:
            holder.stop_holding()
            conn.close()
        self.assertIn(LOCK_ERROR_TEXT, str(raised.exception))

    def test_commit_translates_contention(self) -> None:
        # The COMMIT branch is unreachable end-to-end (a WAL commit does not
        # block once BEGIN IMMEDIATE succeeded), so drive it directly: without it
        # a contended commit escapes as a raw OperationalError to the defensive
        # boundary -- the exact class of bug this module exists for.
        conn = connect(self._db())
        failing = _CommitRaises(conn, sqlite3.OperationalError("database is locked"))
        with self.assertRaises(QueueBusy) as raised:
            with immediate(failing):
                failing.execute("SELECT 1")
        self.assertEqual(failing.commits, 1)
        self.assertIn(LOCK_ERROR_TEXT, str(raised.exception))
        # The transaction must not be left open for the next writer.
        self.assertEqual(failing.rollbacks, 1)
        self.assertFalse(conn.in_transaction)
        conn.close()

    def test_a_non_contention_operational_error_is_not_relabelled(self) -> None:
        # Only busy/locked becomes QueueBusy. Anything else keeps its identity so
        # a genuine disk or schema fault is not reported as "retry me".
        conn = connect(self._db())
        failing = _CommitRaises(conn, sqlite3.OperationalError("disk I/O error"))
        with self.assertRaises(sqlite3.OperationalError) as raised:
            with immediate(failing):
                failing.execute("SELECT 1")
        self.assertNotIsInstance(raised.exception, QueueBusy)
        conn.close()

    def test_reconcile_apply_never_reports_an_unwritten_decision_as_applied(
        self,
    ) -> None:
        # recovery._apply's CAS guard treats a QueueError as "a concurrent op won
        # the race, leave the newer state alone" and returns quietly. Contention
        # is not that: nothing was written, so swallowing it makes reconcile
        # report applied: true / result: success for work it did not do.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                head = git(repo, "rev-parse", "HEAD")
                conn.execute(
                    "UPDATE deploy_queue SET status='needs_reconcile', "
                    "pending_deploy_sha=?, pending_deploy_remote='origin', "
                    "pending_deploy_refs='main', push_status='pending' WHERE id=?",
                    (head, job.id),
                )
                conn.commit()
            finally:
                conn.close()

            # Windows will not remove the temp dir while a connection is open,
            # so close it explicitly rather than leaning on refcounting.
            conn = connect(config.state.db)
            try:
                with patch(
                    "mergetrain.recovery.mark_job",
                    side_effect=QueueBusy("queue database is busy: database is locked"),
                ):
                    with self.assertRaises(QueueBusy):
                        reconcile(config, conn, apply=True)
            finally:
                conn.close()


class ProcessOneContentionTests(unittest.TestCase):
    """process_one has its own QueueBusy clause, and it is the one run-next uses.

    Everything else in this module drives process_batch, so the two clauses could
    drift apart -- an adversarial review found process_one's entered by no test at
    all, meaning a landed single-job deploy could have been reported as a bare
    retryable failure without anything going red.
    """

    def test_prepush_contention_raises_instead_of_blaming_the_branch(self) -> None:
        # The clause's real job is the NOT-landed case. Without it, QueueBusy
        # reaches `except MergetrainError` and parks 'blocked' -- "fix the branch
        # before this can merge" -- for a database that was merely busy. (The
        # landed case cannot prove the clause: finish_after_error's landed-push
        # guard overrides any status to 'deployed' there anyway.)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            conn.execute(f"PRAGMA busy_timeout = {CONTENDED_BUSY_TIMEOUT_MS}")
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                claimed = claim_next_job(
                    conn, owner=f"runner:{os.getpid()}", deploy=True
                )
                self.assertIsNotNone(claimed)
                holder = _WriteLockHolder(config.state.db)
                real_mark_job = git_runner_module.mark_job
                contended = {"n": 0}

                def wrapper(conn_arg, job_id, **kwargs):
                    # Contend the opening write, i.e. before anything is pushed.
                    if kwargs.get("status") == "in_progress" and not contended["n"]:
                        contended["n"] += 1
                        holder.start_holding()
                        try:
                            return real_mark_job(conn_arg, job_id, **kwargs)
                        finally:
                            holder.stop_holding()
                    return real_mark_job(conn_arg, job_id, **kwargs)

                with patch("mergetrain.git_runner.mark_job", wrapper):
                    with self.assertRaises(QueueBusy):
                        GitRunner(config).process_one(
                            conn, claimed, deploy=True, owner=f"runner:{os.getpid()}"
                        )
                holder.assert_clean()
                self.assertEqual(contended["n"], 1)
                final = get_job(conn, job.id)
            finally:
                conn.close()

            self.assertNotIn(final.status, ("blocked", "failed"))
            self.assertEqual(final.push_status, "not_run")
            self.assertEqual(
                git(root / "remote.git", "rev-parse", "main"),
                git(repo, "rev-parse", "origin/main"),
            )


class ContentionContractTests(unittest.TestCase):
    """What an agent driving the CLI sees when the queue database is contended.

    The fingerprint gate nulls every value, so the error.code string and the
    retryable flag an agent branches on live outside it. Pin them here, because
    this is the whole point of the fix: "retry me" must be distinguishable from
    "your branch is broken".
    """

    def test_cli_reports_contention_as_a_retryable_queue_busy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            remote_before = git(root / "remote.git", "rev-parse", "main")
            conn = connect(config.state.db)
            try:
                enqueue_job(conn, task="a", branch="feature/a")
            finally:
                conn.close()

            holder = _WriteLockHolder(config.state.db)
            real_mark_job = git_runner_module.mark_job
            contended = {"done": False}

            def wrapper(conn_arg, job_id, **kwargs):
                if not contended["done"]:
                    contended["done"] = True
                    holder.start_holding()
                    try:
                        return real_mark_job(conn_arg, job_id, **kwargs)
                    finally:
                        holder.stop_holding()
                return real_mark_job(conn_arg, job_id, **kwargs)

            real_connect = cli_module.connect

            def fast_connect(*args, **kwargs):
                # Only shortens the wait; the defect is how the resulting error is
                # classified, not how long SQLite blocks first.
                opened = real_connect(*args, **kwargs)
                opened.execute(f"PRAGMA busy_timeout = {CONTENDED_BUSY_TIMEOUT_MS}")
                return opened

            out = io.StringIO()
            with (
                patch("mergetrain.git_runner.mark_job", wrapper),
                patch.object(cli_module, "connect", fast_connect),
                redirect_stdout(out),
            ):
                code = main(["--repo", str(repo), "run-batch", "--deploy", "--json"])
            holder.assert_clean()

            payload = json.loads(out.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            # queue_busy, not queue_error, and never a graded 'failed' run: the
            # code says "the database was busy", retryable says "call again".
            self.assertEqual(payload["error"]["code"], "queue_busy")
            self.assertTrue(payload["error"]["retryable"])
            self.assertIn("busy", payload["error"]["message"])

            # And the branch is not blamed: nothing shipped, nothing is terminal.
            conn = connect(config.state.db)
            try:
                final = get_job(conn, 1)
            finally:
                conn.close()
            self.assertNotEqual(final.status, "failed")
            self.assertEqual(final.push_status, "not_run")
            self.assertEqual(
                git(root / "remote.git", "rev-parse", "main"), remote_before
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
