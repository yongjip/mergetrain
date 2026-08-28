"""Runner liveness, token-fenced leases, and orphan recovery persistence."""

from __future__ import annotations

import getpass
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from ..errors import CancellationRequested, LockHeld, LostLease
from ..models import RunnerLock
from .transactions import _parse_utc, _plus_minutes, immediate, utc_now

RUNNER_LOCK_NAME = "runner"


class Liveness:
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


def default_owner() -> str:
    return f"{getpass.getuser()}:{os.getpid()}"


def _windows_liveness(pid: int) -> str:
    # os.kill(pid, 0) is the POSIX existence-check idiom, but on Windows
    # signal 0 IS signal.CTRL_C_EVENT: os.kill(pid, 0) sends a real Ctrl-C to
    # that pid's console group instead of probing it, which surfaces as a
    # KeyboardInterrupt (issue #33). Probe with OpenProcess/GetExitCodeProcess
    # instead — no signal is ever delivered.
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            # No such pid → DEAD; exists but not openable (access denied) →
            # ALIVE; anything else is inconclusive.
            return (
                Liveness.ALIVE
                if ctypes.get_last_error() == ERROR_ACCESS_DENIED  # type: ignore[attr-defined]
                else Liveness.DEAD
            )
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return Liveness.UNKNOWN
            return Liveness.ALIVE if exit_code.value == STILL_ACTIVE else Liveness.DEAD
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return Liveness.UNKNOWN


def owner_liveness(owner: str) -> str:
    try:
        pid_text = owner.rsplit(":", 1)[1]
        pid = int(pid_text)
    except Exception:
        return Liveness.UNKNOWN
    if pid <= 0:
        return Liveness.UNKNOWN
    if os.name == "nt":
        return _windows_liveness(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return Liveness.DEAD
    except PermissionError:
        return Liveness.ALIVE
    except OSError:
        return Liveness.UNKNOWN
    return Liveness.ALIVE


def get_lock(conn: sqlite3.Connection, *, name: str = RUNNER_LOCK_NAME) -> RunnerLock | None:
    row = conn.execute("SELECT * FROM locks WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    return RunnerLock.from_row(row, liveness=owner_liveness(str(row["owner"])))


def active_runner_lock(
    conn: sqlite3.Connection, *, name: str = RUNNER_LOCK_NAME
) -> RunnerLock | None:
    """Return the lock that still fences work as an active runner.

    A live local process remains authoritative even if its wall-clock lease is
    stale while it owns in-progress work. An owner whose liveness cannot be
    proved remains authoritative only until its heartbeat-derived expiry. This
    mirrors the lock-acquisition policy and prevents a healthy marker-bearing
    push from being misreported as crash evidence.
    """

    lock = get_lock(conn, name=name)
    if lock is None or not lock.token:
        return None
    expired = _parse_utc(lock.expires_at) <= datetime.now(timezone.utc)
    if lock.liveness == Liveness.ALIVE:
        return lock
    if lock.liveness == Liveness.UNKNOWN and not expired:
        return lock
    return None


def _delete_lock(conn: sqlite3.Connection, *, name: str = RUNNER_LOCK_NAME) -> None:
    conn.execute("DELETE FROM locks WHERE name = ?", (name,))


def _release_lock_token(
    conn: sqlite3.Connection,
    *,
    owner: str,
    token: str,
    name: str = RUNNER_LOCK_NAME,
) -> bool:
    """Release one exact lease inside the caller's current transaction."""

    cur = conn.execute(
        "DELETE FROM locks WHERE name = ? AND owner = ? AND token = ?",
        (name, owner, token),
    )
    return cur.rowcount > 0


def live_worktree_path(
    conn: sqlite3.Connection, *, name: str = RUNNER_LOCK_NAME
) -> str | None:
    """The integration worktree of the currently live runner, or ``None``.

    Read fresh from the lock table so GC can re-check it immediately before each
    deletion — a runner that acquired the lock after GC's protect snapshot was
    built is invisible to that snapshot but visible here (#84, defect 5)."""
    lock = get_lock(conn, name=name)
    if lock and lock.worktree_path and lock.liveness != Liveness.DEAD:
        return lock.worktree_path
    return None


def _in_progress_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM deploy_queue WHERE status = 'in_progress'"
    ).fetchone()
    return int(row["n"])


def has_in_progress(conn: sqlite3.Connection) -> bool:
    """Read-only probe: are any jobs currently ``in_progress``? A daemon tick
    uses this to notice stranded orphans before deciding it is idle (#84.1)."""
    return _in_progress_count(conn) > 0


def _requeue_orphans(conn: sqlite3.Connection) -> None:
    """Recover a previous runner's orphaned ``in_progress`` jobs, marker-aware.

    Three mutually exclusive buckets, ordered so an earlier statement never
    claims a row a later one owns (0.3.0 Phase 2, RFC §4):

    1. cancel requested **and no pending marker** — nothing was ever pushed, so
       the cancel is honored offline → ``canceled``.
    2. a pending-deploy marker is present (a push may have landed, incl. the
       cancel-raced P6 case) — the remote alone can tell truth, so the job is
       **parked** in ``needs_reconcile`` with ``pending_deploy_sha`` *and*
       ``cancel_requested_at`` preserved. It is never blindly re-pushed.
    3. everything else (clean orphan, no marker) — today's fast path →
       ``queued``.

    Runs inside the caller's IMMEDIATE transaction (never opens its own), and
    never touches the remote, so it is safe on the lock-acquisition path.
    """
    now = utc_now()
    conn.execute(
        """
        UPDATE deploy_queue
        SET status = 'canceled', finished_at = ?, claim_token = '',
            note = CASE WHEN note = '' THEN 'canceled after previous runner stopped' ELSE note END
        WHERE status = 'in_progress' AND cancel_requested_at != '' AND pending_deploy_sha = ''
        """,
        (now,),
    )
    conn.execute(
        """
        UPDATE deploy_queue
        SET status = 'needs_reconcile', claim_token = '',
            note = 'parked for reconcile after previous runner stopped'
        WHERE status = 'in_progress' AND pending_deploy_sha != ''
        """
    )
    # Clearing the validated-train identity is deliberate (#160): a requeued row
    # asserting a validation it no longer holds collateral-blocks unrelated auto
    # deploys. But dissolving an APPROVED train silently is its own hazard -- the
    # operator who retries a failed deploy gets whatever is queued now, gated
    # together as a new train, which may not be the set they confirmed. The note
    # is the one field that survives, so say it there. SQLite evaluates every SET
    # expression against the pre-update row, so the CASE below still sees the
    # train_id the later clause clears.
    conn.execute(
        """
        UPDATE deploy_queue
        SET status = 'queued', started_at = '', claim_token = '', cancel_requested_at = '',
            note = CASE
                WHEN train_id != '' THEN
                    're-queued by mergetrain (previous runner gone); validated train '
                    || train_id
                    || ' was dissolved - validate and re-approve before deploying'
                ELSE 're-queued by mergetrain (previous runner gone)'
            END,
            deploy_sha = '', push_status = 'not_run', verify_status = 'not_run',
            train_id = '', train_size = 0, validated_at = '',
            validation_base_sha = '', validation_sha = '', validated_head_sha = '',
            validation_tree_sha = '', validation_gate_policy_sha = '',
            validation_environment_sha = '', validation_train_sha = '',
            reused_validation_sha = ''
        WHERE status = 'in_progress'
        """
    )


def _acquire_runner_lock(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    ttl_minutes: int = 30,
    name: str = RUNNER_LOCK_NAME,
    worktree_path: str = "",
    head_sha: str = "",
) -> RunnerLock:
    owner = owner or default_owner()
    now = utc_now()
    expires = _plus_minutes(ttl_minutes)
    token = uuid.uuid4().hex
    row = conn.execute("SELECT * FROM locks WHERE name = ?", (name,)).fetchone()
    if row is not None:
        current_owner = str(row["owner"])
        live = owner_liveness(current_owner)
        expired = _parse_utc(str(row["expires_at"])) <= datetime.now(timezone.utc)
        if live == Liveness.DEAD:
            _delete_lock(conn, name=name)
            _requeue_orphans(conn)
        elif not expired:
            raise LockHeld(f"runner lock is held by {live} owner: {current_owner}")
        elif _in_progress_count(conn) > 0:
            raise LockHeld(
                f"expired runner lock ({live} owner {current_owner}) has in-progress jobs"
            )
        else:
            _delete_lock(conn, name=name)
    elif _in_progress_count(conn) > 0:
        _requeue_orphans(conn)
    conn.execute(
        """
        INSERT INTO locks (
          name, owner, worktree_path, head_sha, acquired_at, heartbeat_at, expires_at, token
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (name, owner, worktree_path, head_sha, now, now, expires, token),
    )
    row = conn.execute("SELECT * FROM locks WHERE name = ?", (name,)).fetchone()
    lock = RunnerLock.from_row(row, liveness=owner_liveness(owner)) if row is not None else None
    assert lock is not None
    return lock


def acquire_runner_lock(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    ttl_minutes: int = 30,
    name: str = RUNNER_LOCK_NAME,
    worktree_path: str = "",
    head_sha: str = "",
) -> RunnerLock:
    with immediate(conn):
        return _acquire_runner_lock(
            conn,
            owner=owner,
            ttl_minutes=ttl_minutes,
            name=name,
            worktree_path=worktree_path,
            head_sha=head_sha,
        )


def refresh_runner_lock(
    conn: sqlite3.Connection,
    *,
    owner: str,
    token: str,
    ttl_minutes: int = 30,
    name: str = RUNNER_LOCK_NAME,
    worktree_path: str | None = None,
    head_sha: str | None = None,
    check_cancel: bool = True,
) -> None:
    if not token:
        raise LostLease("runner lease token is missing")
    with immediate(conn):
        cur = conn.execute(
            """
            UPDATE locks
            SET heartbeat_at = ?, expires_at = ?,
                worktree_path = COALESCE(?, worktree_path),
                head_sha = COALESCE(?, head_sha)
            WHERE name = ? AND owner = ? AND token = ?
            """,
            (utc_now(), _plus_minutes(ttl_minutes), worktree_path, head_sha, name, owner, token),
        )
        if cur.rowcount != 1:
            raise LostLease(f"runner lease is no longer owned by {owner}")
        if check_cancel:
            canceled = conn.execute(
                """
                SELECT 1 FROM deploy_queue
                WHERE status = 'in_progress' AND claim_token = ? AND cancel_requested_at != ''
                LIMIT 1
                """,
                (token,),
            ).fetchone()
            if canceled is not None:
                raise CancellationRequested("cancellation requested for the active train")


def release_runner_lock(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    token: str | None = None,
    name: str = RUNNER_LOCK_NAME,
) -> bool:
    with immediate(conn):
        if owner is None:
            cur = conn.execute("DELETE FROM locks WHERE name = ?", (name,))
        elif token is not None:
            return _release_lock_token(
                conn, owner=owner, token=token, name=name
            )
        else:
            raise LostLease("runner lease token is required for owner-guarded release")
    return cur.rowcount > 0


def force_clear_lock_and_split(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    token: str | None = None,
    name: str = RUNNER_LOCK_NAME,
) -> bool:
    """Delete the runner lock and run the marker-aware orphan split, atomically.

    Used by ``unlock`` once it has decided the lock may be cleared (a dead/absent
    owner, or an operator-forced steal that has already confirmed the remote is
    reachable). It never itself writes ``deployed``/``failed`` — marker-bearing
    orphans are only parked in ``needs_reconcile`` here; the remote verdict comes
    from the subsequent ``reconcile`` (0.3.0 Phase 2).

    When ``owner`` and ``token`` are given the delete is **scoped** to that exact
    lock: if it matches nothing (the lock changed while unlock was probing the
    remote — e.g. the wedged runner finished and a fresh runner acquired it), the
    split is skipped and ``False`` is returned, so a healthy in-flight runner is
    never clobbered. Returns ``True`` when the lock was cleared and orphans split.
    """
    with immediate(conn):
        if owner is not None and token is not None:
            if not _release_lock_token(
                conn, owner=owner, token=token, name=name
            ):
                return False
        else:
            _delete_lock(conn, name=name)
        _requeue_orphans(conn)
        return True


def recover_orphans(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    ttl_minutes: int = 30,
    name: str = RUNNER_LOCK_NAME,
) -> int:
    """Heal a dead or absent runner's stranded ``in_progress`` jobs without
    claiming new work.

    A daemon tick that finds ``in_progress`` jobs but nothing queued must still
    recover a runner that crashed — or a batch that raised after its lease was
    already released — so the claimed rows reach ``queued`` / ``needs_reconcile``
    / ``canceled`` instead of stranding forever while every later tick reports
    idle (#84, defect 1).

    Reuses the claim path's liveness logic: acquiring the lock steals it only
    from a dead or absent owner and runs the marker-aware split as a side
    effect, while a live owner raises ``LockHeld`` and nothing is touched. The
    lock taken to drive the split is dropped again — this recovers, it does not
    claim. Returns how many jobs the split moved out of ``in_progress`` (0 when
    there is nothing to recover, or a live runner holds the lock)."""
    owner = owner or default_owner()
    with immediate(conn):
        before = _in_progress_count(conn)
        if before == 0:
            return 0
        try:
            lock = _acquire_runner_lock(
                conn, owner=owner, ttl_minutes=ttl_minutes, name=name
            )
        except LockHeld:
            # A live (or expired-but-not-dead) owner still holds the lock; its
            # in-progress train is not ours to reap. Leave it for its own
            # runner or an operator `unlock`.
            return 0
        _release_lock_token(conn, owner=owner, token=lock.token, name=name)
        return before - _in_progress_count(conn)
