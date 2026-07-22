from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import mergetrain.store as store_module
from mergetrain.errors import (
    CancellationRequested,
    DuplicateActiveBranch,
    LockHeld,
    LostLease,
    QueueError,
)
from mergetrain.store import (
    SCHEMA_VERSION,
    Liveness,
    acquire_runner_lock,
    cancel_job,
    claim_all_queued,
    claim_deploy_batch,
    connect,
    counts,
    default_owner,
    deploy_reconcile_pending,
    dismiss_job,
    enqueue_job,
    get_job,
    get_lock,
    list_run_events,
    list_train_jobs,
    live_worktree_path,
    mark_job,
    owner_liveness,
    record_pending_push,
    record_run_event,
    refresh_runner_lock,
    release_runner_lock,
    terminal_branch_candidates,
    unpack_push_refs,
    validated_train_summaries,
)


class OwnerLivenessTests(unittest.TestCase):
    def test_current_process_is_alive_without_signalling_itself(self) -> None:
        # Regression for #33: on Windows os.kill(pid, 0) sends CTRL_C_EVENT
        # (signal 0 == CTRL_C_EVENT) instead of probing, raising an
        # asynchronous KeyboardInterrupt. A liveness probe must never signal.
        try:
            self.assertEqual(owner_liveness(default_owner()), Liveness.ALIVE)
        except KeyboardInterrupt:  # pragma: no cover - the bug this pins
            self.fail("owner_liveness signalled the current process")

    def test_absent_pid_is_dead(self) -> None:
        # PID 1 is init/System and always exists; an astronomically high pid
        # does not. Probe the latter for a stable DEAD.
        self.assertEqual(owner_liveness("runner:2147481000"), Liveness.DEAD)

    def test_unparseable_or_nonpositive_owner_is_unknown(self) -> None:
        self.assertEqual(owner_liveness("no-pid-here"), Liveness.UNKNOWN)
        self.assertEqual(owner_liveness("runner:0"), Liveness.UNKNOWN)
        self.assertEqual(owner_liveness("runner:-4"), Liveness.UNKNOWN)


class StoreTests(unittest.TestCase):
    def make_conn(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        conn = connect(Path(td.name) / "queue.sqlite")
        self.addCleanup(conn.close)
        return conn

    def test_pending_marker_is_owned_scoped_and_clears_on_deploy(self) -> None:
        conn = self.make_conn()
        token = "runner:123"
        a = enqueue_job(conn, task="a", branch="a")
        b = enqueue_job(conn, task="b", branch="b")
        other = enqueue_job(conn, task="c", branch="c")
        # a, b are claimed by this runner; other is left queued (unowned).
        conn.execute(
            "UPDATE deploy_queue SET status='in_progress', claim_token=? WHERE id IN (?, ?)",
            (token, a.id, b.id),
        )
        conn.commit()

        with self.assertRaises(LostLease):
            record_pending_push(
                conn,
                job_ids=[a.id, b.id, other.id],
                deploy_sha="deadbeef",
                claim_token=token,
            )
        # A partial ownership match rolls back the whole marker write.
        self.assertEqual(get_job(conn, a.id).pending_deploy_sha, "")
        self.assertEqual(get_job(conn, b.id).pending_deploy_sha, "")
        record_pending_push(
            conn, job_ids=[a.id, b.id], deploy_sha="deadbeef", claim_token=token
        )
        self.assertEqual(get_job(conn, a.id).pending_deploy_sha, "deadbeef")
        self.assertEqual(get_job(conn, a.id).push_status, "pending")
        self.assertEqual(get_job(conn, b.id).pending_deploy_sha, "deadbeef")
        self.assertEqual(get_job(conn, other.id).pending_deploy_sha, "")
        self.assertEqual(get_job(conn, other.id).push_status, "not_run")

        with self.assertRaises(LostLease):
            record_pending_push(
                conn, job_ids=[a.id], deploy_sha="deadbeef", claim_token=""
            )

        # A landed deploy clears the marker; a failure preserves it for forensics.
        mark_job(conn, a.id, status="deployed", expected_claim_token=token)
        mark_job(conn, b.id, status="failed", expected_claim_token=token)
        self.assertEqual(get_job(conn, a.id).pending_deploy_sha, "")
        self.assertEqual(get_job(conn, b.id).pending_deploy_sha, "deadbeef")

    def test_pending_marker_records_the_push_target(self) -> None:
        # #84 defect 3: the marker persists the remote + normalized push-ref set
        # so a later reconcile evaluates the target the push actually used.
        conn = self.make_conn()
        token = "runner:9"
        job = enqueue_job(conn, task="a", branch="a")
        conn.execute(
            "UPDATE deploy_queue SET status='in_progress', claim_token=? WHERE id=?",
            (token, job.id),
        )
        conn.commit()
        record_pending_push(
            conn,
            job_ids=[job.id],
            deploy_sha="deadbeef",
            claim_token=token,
            remote="upstream",
            push_refs=("main", "refs/deploy/prod"),
        )
        marked = get_job(conn, job.id)
        self.assertEqual(marked.pending_deploy_remote, "upstream")
        self.assertEqual(
            unpack_push_refs(marked.pending_deploy_refs), ["main", "refs/deploy/prod"]
        )

    def test_cancel_refuses_needs_reconcile_and_preserves_marker(self) -> None:
        conn = self.make_conn()
        job = enqueue_job(conn, task="a", branch="a")
        conn.execute(
            "UPDATE deploy_queue SET status='needs_reconcile', "
            "pending_deploy_sha=?, pending_deploy_remote='origin', "
            "pending_deploy_refs='main', push_status='pending' WHERE id=?",
            ("a" * 40, job.id),
        )
        conn.commit()

        with self.assertRaisesRegex(QueueError, "reconcile --apply"):
            cancel_job(conn, job.id)

        preserved = get_job(conn, job.id)
        self.assertEqual(preserved.status, "needs_reconcile")
        self.assertEqual(preserved.pending_deploy_sha, "a" * 40)
        self.assertEqual(preserved.pending_deploy_remote, "origin")
        self.assertEqual(preserved.pending_deploy_refs, "main")

    def test_cancel_queued_job_does_not_overwrite_concurrent_claim(self) -> None:
        conn = self.make_conn()
        job = enqueue_job(conn, task="a", branch="a")
        real_get_job = store_module.get_job
        raced = False

        def get_then_claim(current_conn, job_id):  # type: ignore[no-untyped-def]
            nonlocal raced
            snapshot = real_get_job(current_conn, job_id)
            if not raced:
                raced = True
                current_conn.execute(
                    "UPDATE deploy_queue SET status='in_progress', claim_token=? WHERE id=?",
                    ("new-owner-token", job_id),
                )
                current_conn.commit()
            return snapshot

        with patch("mergetrain.store.get_job", side_effect=get_then_claim):
            with self.assertRaisesRegex(QueueError, "raced by a concurrent transition"):
                cancel_job(conn, job.id)

        claimed = real_get_job(conn, job.id)
        self.assertEqual(claimed.status, "in_progress")
        self.assertEqual(claimed.claim_token, "new-owner-token")

    def test_cancel_in_progress_reports_a_concurrent_completion(self) -> None:
        conn = self.make_conn()
        job = enqueue_job(conn, task="a", branch="a")
        conn.execute(
            "UPDATE deploy_queue SET status='in_progress', claim_token='runner' "
            "WHERE id=?",
            (job.id,),
        )
        conn.commit()
        real_get_job = store_module.get_job
        raced = False

        def get_then_finish(current_conn, job_id):  # type: ignore[no-untyped-def]
            nonlocal raced
            snapshot = real_get_job(current_conn, job_id)
            if not raced:
                raced = True
                current_conn.execute(
                    "UPDATE deploy_queue SET status='deployed', claim_token='', "
                    "note='shipped' WHERE id=?",
                    (job_id,),
                )
                current_conn.commit()
            return snapshot

        with patch("mergetrain.store.get_job", side_effect=get_then_finish):
            with self.assertRaisesRegex(QueueError, "left 'in_progress'"):
                cancel_job(conn, job.id)

        finished = real_get_job(conn, job.id)
        self.assertEqual(finished.status, "deployed")
        self.assertEqual(finished.note, "shipped")
        self.assertEqual(finished.cancel_requested_at, "")

    def test_state_dir_self_ignores(self) -> None:
        # First DB open drops a .gitignore of '*' inside the dedicated state
        # dir mergetrain creates, so it never trips the enqueue clean-worktree
        # check on the next command.
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = Path(td.name) / ".mergetrain" / "queue.sqlite"
        conn = connect(db)
        conn.close()
        ignore = db.parent / ".gitignore"
        self.assertTrue(ignore.is_file())
        self.assertIn("*", ignore.read_text(encoding="utf-8"))

    def test_state_db_at_repo_root_never_hides_the_repo(self) -> None:
        # #84 defect 7: state.db pointing at a pre-existing (shared) directory —
        # e.g. the repo root — must NOT drop a '*' .gitignore. A '*' there would
        # ignore every untracked project file and make the clean-worktree guard
        # return a false clean. Only the exact queue artifacts are ignored.
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
        conn = connect(root / "queue.sqlite")
        conn.close()
        ignore = root / ".gitignore"
        self.assertTrue(ignore.is_file())
        text = ignore.read_text(encoding="utf-8")
        self.assertNotIn("*", text)
        patterns = {
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.startswith("#")
        }
        # Exactly the DB and its sidecars — nothing that could hide app.py.
        self.assertEqual(
            patterns,
            {
                "queue.sqlite",
                "queue.sqlite-wal",
                "queue.sqlite-shm",
                "queue.sqlite-journal",
            },
        )

    def test_self_ignore_never_clobbers_an_existing_gitignore(self) -> None:
        # A user's own root .gitignore is never overwritten (defensively: even
        # the safe scoped form is skipped when a marker already exists).
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        existing = "node_modules/\n"
        (root / ".gitignore").write_text(existing, encoding="utf-8")
        conn = connect(root / "queue.sqlite")
        conn.close()
        self.assertEqual(
            (root / ".gitignore").read_text(encoding="utf-8"), existing
        )

    def test_live_worktree_path_reports_only_a_live_runner(self) -> None:
        # GC re-reads this immediately before each deletion (#84 defect 5): it
        # must return a worktree only while a live owner holds the lock.
        conn = self.make_conn()

        def put_lock(owner: str, worktree: str) -> None:
            conn.execute("DELETE FROM locks")
            conn.execute(
                """
                INSERT INTO locks (name, owner, worktree_path, acquired_at,
                                   heartbeat_at, expires_at, token)
                VALUES ('runner', ?, ?, '2999-01-01T00:00:00Z',
                        '2999-01-01T00:00:00Z', '2999-01-01T00:00:00Z', 'tok')
                """,
                (owner, worktree),
            )
            conn.commit()

        # No lock at all.
        self.assertIsNone(live_worktree_path(conn))
        # A dead owner's lock (impossible pid) is not protected, worktree or not.
        put_lock("ghost:2147481000", "/wt/dead")
        self.assertIsNone(live_worktree_path(conn))
        # A live owner (this process) → its worktree is protected.
        put_lock(f"host:{os.getpid()}", "/wt/live")
        self.assertEqual(live_worktree_path(conn), "/wt/live")

    def test_dismiss_clears_blocked_failed_but_refuses_live_work(self) -> None:
        conn = self.make_conn()
        blocked = enqueue_job(conn, task="a", branch="feature/a")
        mark_job(conn, blocked.id, status="blocked", note="gate failed")
        queued = enqueue_job(conn, task="b", branch="feature/b")
        # A blocked job dismisses to canceled (terminal, out of the count).
        result = dismiss_job(conn, blocked.id)
        self.assertEqual(result.status, "canceled")
        self.assertIn("dismissed", result.note)
        self.assertEqual(counts(conn).get("blocked", 0), 0)
        # Queued (live) work is refused — that needs cancel, not dismiss.
        with self.assertRaisesRegex(QueueError, "blocked or failed"):
            dismiss_job(conn, queued.id)

    def test_dismiss_does_not_overwrite_a_concurrent_claim(self) -> None:
        conn = self.make_conn()
        job = enqueue_job(conn, task="a", branch="feature/a")
        mark_job(conn, job.id, status="blocked", note="gate failed")
        real_get_job = store_module.get_job
        raced = False

        def get_then_claim(current_conn, job_id):  # type: ignore[no-untyped-def]
            nonlocal raced
            snapshot = real_get_job(current_conn, job_id)
            if not raced:
                raced = True
                current_conn.execute(
                    "UPDATE deploy_queue SET status='in_progress', claim_token=? "
                    "WHERE id=?",
                    ("new-owner-token", job_id),
                )
                current_conn.commit()
            return snapshot

        with patch("mergetrain.store.get_job", side_effect=get_then_claim):
            with self.assertRaisesRegex(QueueError, "raced by a concurrent transition"):
                dismiss_job(conn, job.id)

        claimed = real_get_job(conn, job.id)
        self.assertEqual(claimed.status, "in_progress")
        self.assertEqual(claimed.claim_token, "new-owner-token")

    def test_duplicate_active_branch_is_a_typed_error_naming_the_escape(self) -> None:
        conn = self.make_conn()
        enqueue_job(conn, task="a", branch="feature/a")
        with self.assertRaises(DuplicateActiveBranch) as raised:
            enqueue_job(conn, task="a2", branch="feature/a")
        msg = str(raised.exception)
        self.assertIn("dismiss", msg)
        self.assertIn("--allow-duplicate", msg)
        # --allow-duplicate still bypasses it.
        enqueue_job(conn, task="a3", branch="feature/a", allow_duplicate=True)

    def test_conflict_with_is_set_on_block_and_cleared_on_requeue(self) -> None:
        conn = self.make_conn()
        job = enqueue_job(conn, task="a", branch="a")
        blocked = mark_job(
            conn,
            job.id,
            status="blocked",
            note="semantic conflict: passes gates alone but fails combined",
            conflict_with="7,9",
        )
        self.assertEqual(blocked.conflict_with, "7,9")
        self.assertEqual(get_job(conn, job.id).conflict_with, "7,9")
        # Any later transition without an explicit value clears the stale claim.
        requeued = mark_job(conn, job.id, status="queued")
        self.assertEqual(requeued.conflict_with, "")

    def test_legacy_database_migrates_validation_train_columns(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = Path(td.name) / "legacy.sqlite"
        legacy = sqlite3.connect(db)
        legacy.execute(
            """
            CREATE TABLE deploy_queue (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task TEXT NOT NULL,
              branch TEXT NOT NULL,
              worktree_path TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'queued',
              base_sha TEXT NOT NULL DEFAULT '',
              head_sha TEXT NOT NULL DEFAULT '',
              deploy_sha TEXT NOT NULL DEFAULT '',
              requested_at TEXT NOT NULL,
              started_at TEXT NOT NULL DEFAULT '',
              finished_at TEXT NOT NULL DEFAULT '',
              log_path TEXT NOT NULL DEFAULT '',
              note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        legacy.execute(
            """
            INSERT INTO deploy_queue (task, branch, status, requested_at, note)
            VALUES (
              'old', 'feature/old', 'deployed', 'now',
              'post-push verify warning: legacy failure'
            )
            """
        )
        legacy.commit()
        legacy.close()

        conn = connect(db)
        try:
            migrated = get_job(conn, 1)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(deploy_queue)")}
        finally:
            conn.close()
        self.assertEqual(migrated.train_id, "")
        self.assertEqual(migrated.train_size, 0)
        self.assertEqual(migrated.push_status, "succeeded")
        self.assertEqual(migrated.verify_status, "failed")
        self.assertIn("validated_head_sha", columns)
        self.assertIn("claim_token", columns)
        self.assertIn("push_status", columns)
        self.assertIn("verify_status", columns)
        self.assertIn("validation_tree_sha", columns)
        self.assertIn("validation_gate_policy_sha", columns)
        self.assertIn("validation_environment_sha", columns)
        self.assertIn("validation_train_sha", columns)
        self.assertIn("reused_validation_sha", columns)
        self.assertIn("pending_deploy_sha", columns)
        self.assertIn("conflict_with", columns)
        self.assertIn("pending_deploy_remote", columns)
        self.assertIn("pending_deploy_refs", columns)
        self.assertEqual(migrated.pending_deploy_remote, "")
        self.assertEqual(migrated.pending_deploy_refs, "")
        migrated_db = sqlite3.connect(db)
        try:
            self.assertEqual(
                migrated_db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
            )
            event_columns = {
                row[1] for row in migrated_db.execute("PRAGMA table_info(run_events)")
            }
            self.assertIn("phase", event_columns)
            self.assertIn("heartbeat_at", {
                row[1] for row in migrated_db.execute("PRAGMA table_info(locks)")
            })
        finally:
            migrated_db.close()

    def test_partially_applied_v8_migration_adds_only_missing_column(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = Path(td.name) / "partial-v8.sqlite"
        conn = connect(db)
        try:
            job = enqueue_job(conn, task="a", branch="feature/a")
            conn.execute(
                "UPDATE deploy_queue SET pending_deploy_remote = 'upstream' "
                "WHERE id = ?",
                (job.id,),
            )
            conn.commit()
        finally:
            conn.close()

        partial = sqlite3.connect(db)
        partial.execute("ALTER TABLE deploy_queue DROP COLUMN pending_deploy_refs")
        partial.execute("PRAGMA user_version = 7")
        partial.commit()
        partial.close()

        migrated = connect(db)
        try:
            restored = get_job(migrated, job.id)
            columns = {
                row[1]
                for row in migrated.execute("PRAGMA table_info(deploy_queue)")
            }
            version = migrated.execute("PRAGMA user_version").fetchone()[0]
        finally:
            migrated.close()

        self.assertEqual(restored.pending_deploy_remote, "upstream")
        self.assertEqual(restored.pending_deploy_refs, "")
        self.assertIn("pending_deploy_refs", columns)
        self.assertEqual(version, SCHEMA_VERSION)

    def test_newer_schema_version_is_rejected(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = Path(td.name) / "future.sqlite"
        future = sqlite3.connect(db)
        future.execute("PRAGMA user_version = 999")
        future.close()
        with self.assertRaisesRegex(QueueError, "newer than supported"):
            connect(db)

    def test_duplicate_active_branch_is_blocked_until_terminal(self) -> None:
        conn = self.make_conn()
        first = enqueue_job(conn, task="a", branch="feature/a")
        self.assertEqual(first.id, 1)
        with self.assertRaises(QueueError):
            enqueue_job(conn, task="again", branch="feature/a")
        mark_job(conn, first.id, status="validated")
        with self.assertRaises(QueueError):
            enqueue_job(conn, task="again", branch="feature/a")
        mark_job(conn, first.id, status="deployed")
        second = enqueue_job(conn, task="again", branch="feature/a")
        self.assertEqual(second.id, 2)

    def test_claim_records_structured_event_without_exposing_token(self) -> None:
        conn = self.make_conn()
        enqueue_job(conn, task="a", branch="feature/a")
        claimed = claim_all_queued(conn, owner=f"owner:{os.getpid()}")
        events = list_run_events(conn)
        self.assertEqual(events[-1].phase, "claiming")
        self.assertEqual(events[-1].state, "active")
        self.assertEqual(events[-1].claim_token, claimed[0].claim_token)
        self.assertNotIn("claim_token", events[-1].to_dict())

    def test_event_resume_and_job_scope_include_shared_batch_events(self) -> None:
        conn = self.make_conn()
        first = enqueue_job(conn, task="a", branch="feature/a")
        second = enqueue_job(conn, task="b", branch="feature/b")
        claimed = claim_all_queued(conn, owner=f"owner:{os.getpid()}")
        token = claimed[0].claim_token
        first_event = list_run_events(conn)[0]
        shared = record_run_event(
            conn,
            claim_token=token,
            phase="gating",
            state="active",
            message="Running gate 1/1: tests",
        )
        own = record_run_event(
            conn,
            claim_token=token,
            job_id=first.id,
            phase="complete",
            state="success",
            message="first complete",
        )
        record_run_event(
            conn,
            claim_token=token,
            job_id=second.id,
            phase="complete",
            state="success",
            message="second complete",
        )

        resumed = list_run_events(
            conn,
            after_id=first_event.id,
            job_ids=[first.id],
            limit=20,
        )
        self.assertEqual([event.id for event in resumed], [shared.id, own.id])
        self.assertEqual(list_train_jobs(conn, "missing"), [])
        release_runner_lock(conn, owner=f"owner:{os.getpid()}", token=token)

    def test_validated_train_is_claimed_without_new_queued_jobs(self) -> None:
        conn = self.make_conn()
        first = enqueue_job(conn, task="a", branch="feature/a")
        second = enqueue_job(conn, task="b", branch="feature/b")
        for job, head in [(first, "a" * 40), (second, "b" * 40)]:
            mark_job(
                conn,
                job.id,
                status="validated",
                train_id="train-1",
                train_size=2,
                validated_at="2026-07-16T00:00:00Z",
                validation_base_sha="c" * 40,
                validation_sha="d" * 40,
                validated_head_sha=head,
                validation_tree_sha="e" * 40,
                validation_gate_policy_sha="f" * 64,
                validation_environment_sha="a" * 64,
                validation_train_sha="b" * 64,
            )
        queued = enqueue_job(conn, task="later", branch="feature/later")

        summaries = validated_train_summaries(conn)
        self.assertEqual(len(summaries), 1)
        self.assertTrue(summaries[0]["deploy_eligible"])
        self.assertTrue(summaries[0]["reuse_identity_complete"])
        claimed = claim_deploy_batch(conn, owner=f"owner:{os.getpid()}")
        self.assertEqual([job.id for job in claimed], [first.id, second.id])
        self.assertEqual({job.status for job in claimed}, {"in_progress"})
        self.assertEqual(len({job.claim_token for job in claimed}), 1)
        self.assertEqual(get_job(conn, queued.id).status, "queued")
        release_runner_lock(
            conn, owner=f"owner:{os.getpid()}", token=claimed[0].claim_token
        )

    def test_multiple_validated_trains_require_explicit_identity(self) -> None:
        conn = self.make_conn()
        for index, train_id in enumerate(["train-1", "train-2"], start=1):
            job = enqueue_job(conn, task=train_id, branch=f"feature/{index}")
            mark_job(
                conn,
                job.id,
                status="validated",
                train_id=train_id,
                train_size=1,
                validated_at="2026-07-16T00:00:00Z",
                validation_base_sha="a" * 40,
                validation_sha=str(index) * 40,
                validated_head_sha=str(index + 2) * 40,
            )
        with self.assertRaisesRegex(QueueError, "multiple validated trains"):
            claim_deploy_batch(conn, owner=f"owner:{os.getpid()}")
        self.assertIsNone(get_lock(conn))
        claimed = claim_deploy_batch(
            conn,
            owner=f"owner:{os.getpid()}",
            train_id="train-2",
        )
        self.assertEqual([job.train_id for job in claimed], ["train-2"])
        release_runner_lock(
            conn, owner=f"owner:{os.getpid()}", token=claimed[0].claim_token
        )

    def test_validated_job_is_not_a_gc_branch_candidate(self) -> None:
        conn = self.make_conn()
        previous = enqueue_job(conn, task="old", branch="feature/validated")
        deployed = enqueue_job(conn, task="b", branch="feature/deployed")
        mark_job(conn, previous.id, status="deployed")
        validated = enqueue_job(conn, task="a", branch="feature/validated")
        mark_job(conn, validated.id, status="validated")
        mark_job(conn, deployed.id, status="deployed")
        candidates = terminal_branch_candidates(conn)
        self.assertEqual([candidate["branch"] for candidate in candidates], ["feature/deployed"])

    def test_canceling_validated_job_cancels_whole_train(self) -> None:
        conn = self.make_conn()
        first = enqueue_job(conn, task="a", branch="feature/a")
        second = enqueue_job(conn, task="b", branch="feature/b")
        for job in [first, second]:
            mark_job(conn, job.id, status="validated", train_id="train-1", train_size=2)
        cancel_job(conn, first.id)
        self.assertEqual(get_job(conn, first.id).status, "canceled")
        self.assertEqual(get_job(conn, second.id).status, "canceled")

    def test_runner_lock_blocks_concurrent_owner(self) -> None:
        conn = self.make_conn()
        lock = acquire_runner_lock(conn, owner=f"user:{os.getpid()}")
        with self.assertRaises(LockHeld):
            acquire_runner_lock(conn, owner="other:999999")
        self.assertIsNotNone(get_lock(conn))
        self.assertNotIn("token", get_lock(conn).to_dict())
        release_runner_lock(conn, owner=f"user:{os.getpid()}", token=lock.token)
        self.assertIsNone(get_lock(conn))

    def test_expired_lease_is_reclaimable_even_if_pid_alive(self) -> None:
        conn = self.make_conn()
        # The current PID is alive, but the lease is already expired and nothing is
        # in flight. A healthy runner would have refreshed its lease; an abandoned
        # lock (or a recycled PID that merely looks alive) must not block forever.
        acquire_runner_lock(conn, owner=f"user:{os.getpid()}", ttl_minutes=-1)
        lock = acquire_runner_lock(conn, owner=f"newrunner:{os.getpid()}", ttl_minutes=30)
        self.assertEqual(lock.owner, f"newrunner:{os.getpid()}")

    def test_expired_lease_with_in_progress_jobs_is_not_stolen(self) -> None:
        conn = self.make_conn()
        # Acquire while nothing is in flight (so no orphan requeue), then leave a job
        # mid-flight. An expired lease with in_progress work is held back for operator
        # investigation rather than auto-reclaimed.
        acquire_runner_lock(conn, owner=f"user:{os.getpid()}", ttl_minutes=-1)
        job = enqueue_job(conn, task="a", branch="a")
        mark_job(conn, job.id, status="in_progress")
        with self.assertRaises(LockHeld):
            acquire_runner_lock(conn, owner=f"newrunner:{os.getpid()}")

    def test_batch_claim_fences_stale_owner_and_requests_cooperative_cancel(self) -> None:
        conn = self.make_conn()
        first = enqueue_job(conn, task="a", branch="a")
        second = enqueue_job(conn, task="b", branch="b")
        owner = f"runner:{os.getpid()}"
        claimed = claim_all_queued(conn, owner=owner, ttl_minutes=-1)
        token = claimed[0].claim_token
        conn.execute(
            "UPDATE deploy_queue SET note='preserve sibling note' WHERE id=?",
            (second.id,),
        )
        conn.commit()

        with self.assertRaises(LockHeld):
            acquire_runner_lock(conn, owner=f"other:{os.getpid()}")

        requested = cancel_job(conn, first.id, note="stop this train")
        self.assertEqual(requested.status, "in_progress")
        self.assertTrue(requested.cancel_requested_at)
        self.assertTrue(get_job(conn, second.id).cancel_requested_at)
        self.assertEqual(requested.note, "stop this train")
        self.assertEqual(get_job(conn, second.id).note, "preserve sibling note")
        with self.assertRaises(CancellationRequested):
            refresh_runner_lock(conn, owner=owner, token=token)
        with self.assertRaises(CancellationRequested):
            mark_job(
                conn,
                first.id,
                status="validated",
                expected_claim_token=token,
            )

    def test_replaced_lease_cannot_be_refreshed_or_release_new_owner(self) -> None:
        conn = self.make_conn()
        job = enqueue_job(conn, task="a", branch="a")
        owner = f"runner:{os.getpid()}"
        claimed = claim_all_queued(conn, owner=owner)
        stale_token = claimed[0].claim_token
        conn.execute(
            "UPDATE locks SET owner = ?, token = ? WHERE name = 'runner'",
            (f"replacement:{os.getpid()}", "replacement-token"),
        )
        conn.commit()
        with self.assertRaises(LostLease):
            refresh_runner_lock(conn, owner=owner, token=stale_token)
        self.assertFalse(
            release_runner_lock(conn, owner=owner, token=stale_token)
        )
        self.assertEqual(get_lock(conn).token, "replacement-token")
        self.assertEqual(get_job(conn, job.id).status, "in_progress")

    def test_refresh_runner_lock_preserves_omitted_metadata(self) -> None:
        conn = self.make_conn()
        owner = f"runner:{os.getpid()}"
        lock = acquire_runner_lock(
            conn,
            owner=owner,
            worktree_path="/worktrees/live",
            head_sha="a" * 40,
        )

        refresh_runner_lock(conn, owner=owner, token=lock.token)
        preserved = get_lock(conn)
        self.assertEqual(preserved.worktree_path, "/worktrees/live")
        self.assertEqual(preserved.head_sha, "a" * 40)

        refresh_runner_lock(
            conn,
            owner=owner,
            token=lock.token,
            worktree_path="/worktrees/moved",
            head_sha="b" * 40,
        )
        updated = get_lock(conn)
        self.assertEqual(updated.worktree_path, "/worktrees/moved")
        self.assertEqual(updated.head_sha, "b" * 40)

    def test_auto_only_batch_claim_skips_manual_jobs(self) -> None:
        conn = self.make_conn()
        manual = enqueue_job(conn, task="manual", branch="manual")
        auto = enqueue_job(conn, task="auto", branch="auto", auto_deploy=True)
        jobs = claim_all_queued(conn, owner="owner:999999", auto_only=True)
        self.assertEqual([job.id for job in jobs], [auto.id])
        self.assertEqual(jobs[0].status, "in_progress")
        release_runner_lock(conn, owner="owner:999999", token=jobs[0].claim_token)
        self.assertEqual(manual.status, "queued")

    def test_terminal_job_cannot_be_canceled(self) -> None:
        conn = self.make_conn()
        job = enqueue_job(conn, task="a", branch="a")
        mark_job(conn, job.id, status="deployed")
        with self.assertRaises(QueueError):
            cancel_job(conn, job.id)


class ConcurrencyAndTransitionTests(unittest.TestCase):
    """Real cross-thread contention on one DB file (guarantee #2, the lease
    fence) plus the actual behavior of the mark_job state machine."""

    def _db(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = Path(td.name) / "queue.sqlite"
        connect(db).close()  # create the schema once, up front
        return db

    def _race(self, db, callers):
        # Run each (name, fn) on its OWN connection, released together by a
        # Barrier so they hit BEGIN IMMEDIATE simultaneously — a genuine race,
        # unlike the other lock tests which drive one connection sequentially.
        barrier = threading.Barrier(len(callers))
        out: dict[str, tuple[str, object]] = {}

        def run(name, fn):
            try:
                conn = connect(db)
            except BaseException as exc:  # pragma: no cover - setup failure
                out[name] = ("error", repr(exc))
                return
            try:
                barrier.wait(timeout=10)
                out[name] = ("ok", fn(conn))
            except LockHeld:
                out[name] = ("fenced", None)
            except BaseException as exc:  # pragma: no cover - unexpected
                out[name] = ("error", repr(exc))
            finally:
                conn.close()

        threads = [threading.Thread(target=run, args=(n, f)) for n, f in callers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return out

    def test_two_runners_racing_the_runner_lock_exactly_one_wins(self) -> None:
        db = self._db()
        pid = os.getpid()  # a live pid, so the loser is FENCED and never steals
        owners = [f"runnerA:{pid}", f"runnerB:{pid}"]
        out = self._race(
            db, [(o, lambda c, o=o: acquire_runner_lock(c, owner=o)) for o in owners]
        )

        self.assertNotIn("error", [v[0] for v in out.values()], out)
        self.assertEqual(sorted(v[0] for v in out.values()), ["fenced", "ok"], out)
        winner = next(o for o, v in out.items() if v[0] == "ok")
        conn = connect(db)
        self.addCleanup(conn.close)
        lock = get_lock(conn)
        self.assertIsNotNone(lock)
        self.assertEqual(lock.owner, winner)
        self.assertEqual(lock.token, out[winner][1].token)

    def test_two_runners_racing_claim_all_queued_get_no_split_batch(self) -> None:
        db = self._db()
        seed = connect(db)
        try:
            for i in range(3):
                enqueue_job(seed, task=f"t{i}", branch=f"agent/{i}")
        finally:
            seed.close()
        pid = os.getpid()
        owners = [f"runnerA:{pid}", f"runnerB:{pid}"]
        out = self._race(
            db, [(o, lambda c, o=o: claim_all_queued(c, owner=o)) for o in owners]
        )

        self.assertNotIn("error", [v[0] for v in out.values()], out)
        self.assertEqual(sorted(v[0] for v in out.values()), ["fenced", "ok"], out)
        winner = next(o for o, v in out.items() if v[0] == "ok")
        claimed = out[winner][1]
        self.assertEqual(len(claimed), 3)  # the winner got the WHOLE batch
        conn = connect(db)
        self.addCleanup(conn.close)
        tokens = set()
        for job in claimed:
            row = get_job(conn, job.id)
            self.assertEqual(row.status, "in_progress")
            tokens.add(row.claim_token)
        self.assertEqual(len(tokens), 1)  # one token — no split-batch double claim

    def test_mark_job_rejects_unknown_status_and_stale_claim_token(self) -> None:
        conn = connect(self._db())
        self.addCleanup(conn.close)
        job = enqueue_job(conn, task="a", branch="agent/a")
        # an unknown status value is rejected before any row is touched
        with self.assertRaises(QueueError):
            mark_job(conn, job.id, status="bogus")
        # claim it for one runner, giving the row an in_progress claim token
        claim_all_queued(conn, owner=f"runnerA:{os.getpid()}")
        # a second runner presenting the WRONG token cannot mark this job
        with self.assertRaises(LostLease):
            mark_job(
                conn, job.id, status="validated", expected_claim_token="not-the-real-token"
            )
        # an empty claim token is rejected immediately
        with self.assertRaises(LostLease):
            mark_job(conn, job.id, status="validated", expected_claim_token="")

    def test_mark_job_has_no_transition_graph_documents_current_behavior(self) -> None:
        # DOCUMENTS CURRENT BEHAVIOR (not a desired invariant): mark_job enforces
        # no legal-transition graph and no terminal guard at the store layer —
        # the guards live in cancel_job/dismiss_job and the claim-token fence.
        # Pinned so a future change here is a conscious, reviewed edit.
        conn = connect(self._db())
        self.addCleanup(conn.close)
        job = enqueue_job(conn, task="a", branch="agent/a")
        mark_job(
            conn, job.id, status="deployed",
            deploy_sha="d" * 40, validated_head_sha="h" * 40, note="shipped",
        )
        self.assertEqual(get_job(conn, job.id).status, "deployed")

        # an unfenced mark_job REOPENS the terminal row (no store-level guard)...
        reopened = mark_job(conn, job.id, status="queued")
        self.assertEqual(reopened.status, "queued")
        # ...unconditionally WIPES the free-form note (a real footgun)...
        self.assertEqual(reopened.note, "")
        # ...but COALESCE-protected artifacts SURVIVE, so a reopened job still
        # carries stale terminal shas.
        self.assertEqual(reopened.deploy_sha, "d" * 40)
        self.assertEqual(reopened.validated_head_sha, "h" * 40)

        # By contrast the higher-level entrypoint DOES guard terminal state.
        mark_job(conn, job.id, status="deployed")
        with self.assertRaises(QueueError):
            cancel_job(conn, job.id)

    def test_claim_deploy_batch_refuses_while_a_reconcile_is_pending(self) -> None:
        conn = connect(self._db())
        self.addCleanup(conn.close)
        job = enqueue_job(conn, task="a", branch="agent/a")
        mark_job(conn, job.id, status="needs_reconcile", note="parked by a crash")
        self.assertGreater(deploy_reconcile_pending(conn), 0)
        # A deploy targets the same push refs, so it must refuse fail-closed
        # rather than push over the pending reconcile — even when the lock reap
        # inside the claim parks the orphan after the CLI pre-check (TOCTOU #4a).
        self.assertEqual(claim_deploy_batch(conn, owner=f"runner:{os.getpid()}"), [])
        self.assertIsNone(get_lock(conn))  # the lock was released, not left held

    def test_reconcile_write_is_a_compare_and_swap_on_source_status(self) -> None:
        conn = connect(self._db())
        self.addCleanup(conn.close)
        # A CAS that still matches the source status succeeds.
        job = enqueue_job(conn, task="a", branch="agent/a")
        mark_job(conn, job.id, status="needs_reconcile")
        mark_job(conn, job.id, status="queued", expected_status="needs_reconcile")
        self.assertEqual(get_job(conn, job.id).status, "queued")
        # A stale recovery decision cannot overwrite a job a concurrent op moved:
        # a cancel landing during reconcile's remote I/O must survive (#4b).
        other = enqueue_job(conn, task="b", branch="agent/b")
        mark_job(conn, other.id, status="needs_reconcile")
        mark_job(conn, other.id, status="canceled", note="user cancel during reconcile")
        with self.assertRaises(QueueError):
            mark_job(conn, other.id, status="queued", expected_status="needs_reconcile")
        self.assertEqual(get_job(conn, other.id).status, "canceled")


if __name__ == "__main__":
    unittest.main()
