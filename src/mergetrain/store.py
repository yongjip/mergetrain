"""SQLite storage and runner lock management."""

from __future__ import annotations

import getpass
import os
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .errors import (
    CancellationRequested,
    DuplicateActiveBranch,
    LockHeld,
    LostLease,
    QueueError,
)
from .models import (
    ACTIVE_STATUSES,
    ALL_STATUSES,
    PUSH_STATUSES,
    TERMINAL_STATUSES,
    VERIFY_STATUSES,
    Job,
    RunEvent,
    RunnerLock,
)

RUNNER_LOCK_NAME = "runner"
SCHEMA_VERSION = 8


class Liveness:
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, timezone.utc)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _plus_minutes(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _self_ignore(state_dir: Path, *, db_name: str, dedicated: bool) -> None:
    """Keep mergetrain's own state out of Git's view without ever hiding the repo.

    The queue DB, logs, and worktrees live in-repo (``.mergetrain/`` by
    default). Without an ignore, the first command that opens the DB leaves
    that state untracked, so the *next* ``enqueue`` fails the clean-worktree
    check — the tool's own state breaks its own precondition.

    When mergetrain created the directory itself (the default dedicated
    ``.mergetrain/``), a ``*`` wildcard cleanly covers the DB, logs, and
    worktrees. But ``state.db`` can point anywhere, including the repo root: a
    ``*`` there would ignore every untracked project file and make the
    clean-worktree guard return a *false* clean. So a directory mergetrain did
    not create only ever ignores the exact queue artifacts it holds — the DB
    and its WAL/SHM/journal sidecars — never ``*`` (#84, defect 7).

    Never clobbers a ``.gitignore`` that is already present (it may be the
    user's).
    """

    marker = state_dir / ".gitignore"
    if marker.exists():
        return
    if dedicated:
        body = "# Managed by mergetrain — local queue state.\n*\n"
    else:
        artifacts = "\n".join(
            db_name + suffix for suffix in ("", "-wal", "-shm", "-journal")
        )
        body = (
            "# Managed by mergetrain — local queue state.\n"
            "# state.db is not in a mergetrain-owned directory, so only the exact\n"
            "# queue artifacts are ignored — never a wildcard, which would hide\n"
            "# the whole directory and fake a clean worktree.\n"
            f"{artifacts}\n"
        )
    try:
        marker.write_text(body, encoding="utf-8")
    except OSError:
        pass  # best-effort; never fail a connect over an ignore file


def connect(db_path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if read_only:
        # Observer path (the hub): never create directories, never migrate
        # another repo's state, never write a row. A repo whose schema
        # differs from this CLI is reported, not upgraded — sovereignty over
        # repo state stays with a runner invoked inside that repo.
        #
        # Honest limit of mode=ro on a WAL database: SQLite readers still
        # participate in the wal-index, so observing may create/refresh the
        # sidecar -shm (and an empty -wal) next to the database. No queue
        # data is ever written; a repo directory the observer cannot write
        # to surfaces as a clear QueueError below instead of a crash.
        if path == Path(":memory:") or not path.is_file():
            raise QueueError(f"queue database does not exist: {path}")
        # Percent-escape the filesystem path: an unescaped '?' or '#' would
        # truncate the URI filename AND silently drop mode=ro (falling back
        # to a writable connection), and a literal '%XX' would be decoded
        # into a different path.
        conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise QueueError(
                    f"queue schema version {version} does not match supported version "
                    f"{SCHEMA_VERSION}; run mergetrain inside that repo to migrate"
                )
        except sqlite3.OperationalError as exc:
            conn.close()
            if "readonly" in str(exc).lower():
                raise QueueError(
                    f"cannot observe {path}: the database directory is not "
                    "writable, and a WAL reader needs to maintain the -shm "
                    f"sidecar file ({exc})"
                ) from exc
            raise
        except Exception:
            conn.close()
            raise
        return conn
    if path != Path(":memory:"):
        state_dir = path.parent
        # A directory mergetrain has to create is its own and safe to blanket
        # ignore; a pre-existing one (e.g. the repo root, when state.db points
        # there) is shared and must never be hidden behind '*'.
        dedicated = not state_dir.exists()
        state_dir.mkdir(parents=True, exist_ok=True)
        _self_ignore(state_dir, db_name=path.name, dedicated=dedicated)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    # WAL is not available for in-memory DBs, but SQLite quietly returns memory.
    conn.execute("PRAGMA journal_mode = WAL")
    # Durability (0.3.0 recovery): fsync each commit so the pre-push
    # pending_deploy_sha marker cannot be lost to power loss after the remote
    # was already mutated. Deploys are infrequent, so the per-commit fsync cost
    # is negligible; see docs/proposals/0.3.0-recovery.md decision Q3.
    conn.execute("PRAGMA synchronous = FULL")
    try:
        ensure_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise QueueError(
            f"queue schema version {version} is newer than supported version {SCHEMA_VERSION}"
        )
    if version == SCHEMA_VERSION:
        return

    with immediate(conn):
        conn.execute(
            """
        CREATE TABLE IF NOT EXISTS deploy_queue (
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
          note TEXT NOT NULL DEFAULT '',
          push_status TEXT NOT NULL DEFAULT 'not_run',
          verify_status TEXT NOT NULL DEFAULT 'not_run',
          auto_deploy INTEGER NOT NULL DEFAULT 0,
          train_id TEXT NOT NULL DEFAULT '',
          train_size INTEGER NOT NULL DEFAULT 0,
          validated_at TEXT NOT NULL DEFAULT '',
          validation_base_sha TEXT NOT NULL DEFAULT '',
          validation_sha TEXT NOT NULL DEFAULT '',
          validated_head_sha TEXT NOT NULL DEFAULT '',
          validation_tree_sha TEXT NOT NULL DEFAULT '',
          validation_gate_policy_sha TEXT NOT NULL DEFAULT '',
          validation_environment_sha TEXT NOT NULL DEFAULT '',
          validation_train_sha TEXT NOT NULL DEFAULT '',
          reused_validation_sha TEXT NOT NULL DEFAULT '',
          claim_token TEXT NOT NULL DEFAULT '',
          cancel_requested_at TEXT NOT NULL DEFAULT '',
          pending_deploy_sha TEXT NOT NULL DEFAULT '',
          conflict_with TEXT NOT NULL DEFAULT '',
          pending_deploy_remote TEXT NOT NULL DEFAULT '',
          pending_deploy_refs TEXT NOT NULL DEFAULT ''
        )
        """
        )
        conn.execute(
            """
        CREATE TABLE IF NOT EXISTS locks (
          name TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          worktree_path TEXT NOT NULL DEFAULT '',
          head_sha TEXT NOT NULL DEFAULT '',
          acquired_at TEXT NOT NULL,
          heartbeat_at TEXT NOT NULL DEFAULT '',
          expires_at TEXT NOT NULL,
          token TEXT NOT NULL DEFAULT ''
        )
        """
        )
        conn.execute(
            """
        CREATE TABLE IF NOT EXISTS run_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          claim_token TEXT NOT NULL DEFAULT '',
          job_id INTEGER,
          phase TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'info',
          message TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          FOREIGN KEY(job_id) REFERENCES deploy_queue(id)
        )
        """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS run_events_created_at_idx ON run_events(created_at, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS run_events_claim_idx ON run_events(claim_token, id)"
        )

        migrations = {
            1: (
                ("deploy_queue", "auto_deploy", "INTEGER NOT NULL DEFAULT 0"),
                ("deploy_queue", "train_id", "TEXT NOT NULL DEFAULT ''"),
                ("deploy_queue", "train_size", "INTEGER NOT NULL DEFAULT 0"),
                ("deploy_queue", "validated_at", "TEXT NOT NULL DEFAULT ''"),
                ("deploy_queue", "validation_base_sha", "TEXT NOT NULL DEFAULT ''"),
                ("deploy_queue", "validation_sha", "TEXT NOT NULL DEFAULT ''"),
                ("deploy_queue", "validated_head_sha", "TEXT NOT NULL DEFAULT ''"),
            ),
            2: (
                ("deploy_queue", "claim_token", "TEXT NOT NULL DEFAULT ''"),
                ("deploy_queue", "cancel_requested_at", "TEXT NOT NULL DEFAULT ''"),
                ("locks", "token", "TEXT NOT NULL DEFAULT ''"),
            ),
            3: (
                ("locks", "heartbeat_at", "TEXT NOT NULL DEFAULT ''"),
            ),
            4: (
                ("deploy_queue", "push_status", "TEXT NOT NULL DEFAULT 'not_run'"),
                ("deploy_queue", "verify_status", "TEXT NOT NULL DEFAULT 'not_run'"),
            ),
            5: (
                ("deploy_queue", "validation_tree_sha", "TEXT NOT NULL DEFAULT ''"),
                (
                    "deploy_queue",
                    "validation_gate_policy_sha",
                    "TEXT NOT NULL DEFAULT ''",
                ),
                (
                    "deploy_queue",
                    "validation_environment_sha",
                    "TEXT NOT NULL DEFAULT ''",
                ),
                ("deploy_queue", "validation_train_sha", "TEXT NOT NULL DEFAULT ''"),
                ("deploy_queue", "reused_validation_sha", "TEXT NOT NULL DEFAULT ''"),
            ),
            6: (
                ("deploy_queue", "pending_deploy_sha", "TEXT NOT NULL DEFAULT ''"),
            ),
            7: (
                ("deploy_queue", "conflict_with", "TEXT NOT NULL DEFAULT ''"),
            ),
            8: (
                ("deploy_queue", "pending_deploy_remote", "TEXT NOT NULL DEFAULT ''"),
                ("deploy_queue", "pending_deploy_refs", "TEXT NOT NULL DEFAULT ''"),
            ),
        }
        for next_version in range(version + 1, SCHEMA_VERSION + 1):
            for table, column, definition in migrations[next_version]:
                columns = {
                    str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            if next_version == 4:
                conn.execute(
                    "UPDATE deploy_queue SET push_status = 'succeeded' WHERE status = 'deployed'"
                )
                conn.execute(
                    """
                    UPDATE deploy_queue
                    SET verify_status = 'failed'
                    WHERE status = 'deployed' AND note LIKE 'post-push verify warning:%'
                    """
                )
            conn.execute(f"PRAGMA user_version = {next_version}")


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


def _status_placeholders(statuses: tuple[str, ...] | list[str]) -> str:
    return ",".join("?" for _ in statuses)


def _active_branch_count(conn: sqlite3.Connection, branch: str) -> int:
    placeholders = _status_placeholders(ACTIVE_STATUSES)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM deploy_queue WHERE branch = ? AND status IN ({placeholders})",
        (branch, *ACTIVE_STATUSES),
    ).fetchone()
    return int(row["n"])


def enqueue_job(
    conn: sqlite3.Connection,
    *,
    task: str,
    branch: str,
    worktree_path: str = "",
    base_sha: str = "",
    head_sha: str = "",
    note: str = "",
    allow_duplicate: bool = False,
    auto_deploy: bool = False,
) -> Job:
    task = task.strip()
    branch = branch.strip()
    if not task:
        raise QueueError("--task is required")
    if not branch:
        raise QueueError("--branch is required")
    with immediate(conn):
        if not allow_duplicate and _active_branch_count(conn, branch):
            raise DuplicateActiveBranch(
                f"branch '{branch}' already has an active job. If a job on it is "
                "blocked/failed, dismiss it first (mergetrain dismiss <id>, "
                "non-destructive) then enqueue the fix, or re-enqueue with "
                "--allow-duplicate."
            )
        now = utc_now()
        cur = conn.execute(
            """
            INSERT INTO deploy_queue (
              task, branch, worktree_path, status, base_sha, head_sha,
              requested_at, note, auto_deploy
            ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (task, branch, worktree_path, base_sha, head_sha, now, note, 1 if auto_deploy else 0),
        )
        job_id = cur.lastrowid
        assert job_id is not None  # an INSERT always assigns a rowid
    return get_job(conn, job_id)


def get_job(conn: sqlite3.Connection, job_id: int) -> Job:
    row = conn.execute("SELECT * FROM deploy_queue WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise QueueError(f"job not found: {job_id}")
    return Job.from_row(row)


def list_jobs(conn: sqlite3.Connection, *, limit: int = 50) -> list[Job]:
    rows = conn.execute(
        "SELECT * FROM deploy_queue ORDER BY id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def list_dismissable_jobs(conn: sqlite3.Connection) -> list[Job]:
    """Return every blocked/failed job, without the status display cap."""

    rows = conn.execute(
        "SELECT * FROM deploy_queue WHERE status IN ('blocked', 'failed') "
        "ORDER BY id ASC"
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def list_verify_unknown_jobs(conn: sqlite3.Connection) -> list[Job]:
    """Deployed jobs whose post-push verify never resolved (crash recovery)."""
    rows = conn.execute(
        "SELECT * FROM deploy_queue WHERE status = 'deployed' AND verify_status = 'unknown' "
        "ORDER BY id ASC"
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def resolve_verify_status(
    conn: sqlite3.Connection, job_id: int, *, verify_status: str, note: str = ""
) -> Job:
    """Discharge a deployed job's unresolved post-push verify (``mergetrain
    verify``). Only moves a deployed+unknown job to succeeded/failed — never
    reopens a terminal job or touches its deployed status."""
    if verify_status not in {"succeeded", "failed"}:
        raise QueueError(f"verify_status must be 'succeeded' or 'failed', got {verify_status!r}")
    with immediate(conn):
        row = conn.execute(
            "SELECT status, verify_status FROM deploy_queue WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise QueueError(f"job not found: {job_id}")
        if str(row["status"]) != "deployed" or str(row["verify_status"]) != "unknown":
            raise QueueError(
                f"job {job_id} is not an unresolved verify (status={row['status']}, "
                f"verify_status={row['verify_status']})"
            )
        conn.execute(
            "UPDATE deploy_queue SET verify_status = ?, note = COALESCE(NULLIF(?, ''), note) "
            "WHERE id = ?",
            (verify_status, note, job_id),
        )
    return get_job(conn, job_id)


def list_train_jobs(conn: sqlite3.Connection, train_id: str) -> list[Job]:
    if not train_id:
        return []
    rows = conn.execute(
        "SELECT * FROM deploy_queue WHERE train_id = ? ORDER BY id ASC",
        (train_id,),
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def list_jobs_fifo(conn: sqlite3.Connection, *, status: str = "queued", auto_only: bool = False) -> list[Job]:
    if auto_only:
        rows = conn.execute(
            "SELECT * FROM deploy_queue WHERE status = ? AND auto_deploy = 1 ORDER BY id ASC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM deploy_queue WHERE status = ? ORDER BY id ASC",
            (status,),
        ).fetchall()
    return [Job.from_row(row) for row in rows]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    result = dict.fromkeys(ALL_STATUSES, 0)
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM deploy_queue GROUP BY status").fetchall()
    for row in rows:
        result[str(row["status"])] = int(row["n"])
    result["auto_queued"] = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM deploy_queue WHERE status = 'queued' AND auto_deploy = 1"
        ).fetchone()["n"]
    )
    result["manual_queued"] = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM deploy_queue WHERE status = 'queued' AND auto_deploy = 0"
        ).fetchone()["n"]
    )
    # Marker/verify-derived signals for doctor next_action (0.3.0 Phase 2, DB-only).
    result["in_progress_with_marker"] = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM deploy_queue "
            "WHERE status = 'in_progress' AND pending_deploy_sha != ''"
        ).fetchone()["n"]
    )
    result["blocked_with_marker"] = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM deploy_queue "
            "WHERE status = 'blocked' AND pending_deploy_sha != ''"
        ).fetchone()["n"]
    )
    result["deployed_verify_unknown"] = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM deploy_queue "
            "WHERE status = 'deployed' AND verify_status = 'unknown'"
        ).fetchone()["n"]
    )
    return result


def deploy_reconcile_pending(conn: sqlite3.Connection) -> int:
    """Count jobs that make a deploy unsafe: parked reconciles plus not-yet-split
    marker-bearing orphans. A deploy targets the same push refs, so every deploy
    entrypoint (``run-batch``, ``run-next``, and the daemon) must refuse while
    this is non-zero (0.3.0 Phase 2, decision Q4)."""
    data = counts(conn)
    return data.get("needs_reconcile", 0) + data.get("in_progress_with_marker", 0)


def has_queued_auto(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM deploy_queue WHERE status = 'queued' AND auto_deploy = 1 LIMIT 1"
    ).fetchone()
    return row is not None


def get_lock(conn: sqlite3.Connection, *, name: str = RUNNER_LOCK_NAME) -> RunnerLock | None:
    row = conn.execute("SELECT * FROM locks WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    return RunnerLock.from_row(row, liveness=owner_liveness(str(row["owner"])))


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
    conn.execute(
        """
        UPDATE deploy_queue
        SET status = 'queued', started_at = '', claim_token = '', cancel_requested_at = '',
            note = 're-queued by mergetrain (previous runner gone)'
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
    worktree_path: str = "",
    head_sha: str = "",
    check_cancel: bool = True,
) -> None:
    if not token:
        raise LostLease("runner lease token is missing")
    with immediate(conn):
        cur = conn.execute(
            """
            UPDATE locks
            SET heartbeat_at = ?, expires_at = ?, worktree_path = ?, head_sha = ?
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


def claim_next_job(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    ttl_minutes: int = 30,
    deploy: bool = False,
) -> Job | None:
    owner = owner or default_owner()
    with immediate(conn):
        lock = _acquire_runner_lock(conn, owner=owner, ttl_minutes=ttl_minutes)
        if deploy and deploy_reconcile_pending(conn):
            # Lock acquisition can park a marker-bearing orphan in this same
            # transaction. Refuse the new deploy claim after that state change,
            # just like the daemon and batch claim paths do.
            _release_lock_token(conn, owner=owner, token=lock.token)
            return None
        row = conn.execute(
            "SELECT * FROM deploy_queue WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            _release_lock_token(conn, owner=owner, token=lock.token)
            return None
        job_id = int(row["id"])
        conn.execute(
            """
            UPDATE deploy_queue
            SET status = 'in_progress', started_at = ?, note = ?, claim_token = ?,
                cancel_requested_at = ''
            WHERE id = ? AND status = 'queued'
            """,
            (utc_now(), "claimed by mergetrain runner", lock.token, job_id),
        )
        _record_run_event(
            conn,
            claim_token=lock.token,
            job_id=job_id,
            phase="claiming",
            state="active",
            message="Runner claimed 1 job",
        )
    return get_job(conn, job_id)


def claim_all_queued(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    ttl_minutes: int = 30,
    auto_only: bool = False,
) -> list[Job]:
    owner = owner or default_owner()
    with immediate(conn):
        lock = _acquire_runner_lock(conn, owner=owner, ttl_minutes=ttl_minutes)
        if auto_only and deploy_reconcile_pending(conn):
            # Acquiring the lock can itself park marker-bearing orphans as
            # needs_reconcile (dead-owner requeue). An unattended deploy must
            # observe that inside the same claim transaction — checking only
            # before the claim leaves a TOCTOU window where the daemon pushes
            # over a pending reconcile.
            _release_lock_token(conn, owner=owner, token=lock.token)
            return []
        if auto_only:
            rows = conn.execute(
                "SELECT id FROM deploy_queue WHERE status = 'queued' AND auto_deploy = 1 ORDER BY id ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM deploy_queue WHERE status = 'queued' ORDER BY id ASC"
            ).fetchall()
        job_ids = [int(row["id"]) for row in rows]
        if not job_ids:
            _release_lock_token(conn, owner=owner, token=lock.token)
            return []
        placeholders = ",".join("?" for _ in job_ids)
        conn.execute(
            f"""
            UPDATE deploy_queue
            SET status = 'in_progress', started_at = ?, note = ?, claim_token = ?,
                cancel_requested_at = ''
            WHERE id IN ({placeholders}) AND status = 'queued'
            """,
            (utc_now(), "claimed by mergetrain batch runner", lock.token, *job_ids),
        )
        _record_run_event(
            conn,
            claim_token=lock.token,
            phase="claiming",
            state="active",
            message=f"Runner claimed {len(job_ids)} job(s)",
        )
    return [get_job(conn, job_id) for job_id in job_ids]


def validated_train_summaries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Describe pending validated trains and whether their identity is complete."""

    rows = conn.execute(
        "SELECT * FROM deploy_queue WHERE status = 'validated' ORDER BY id ASC"
    ).fetchall()
    groups: dict[str, list[Job]] = {}
    for row in rows:
        job = Job.from_row(row)
        key = job.train_id or f"legacy-job-{job.id}"
        groups.setdefault(key, []).append(job)

    summaries: list[dict[str, Any]] = []
    for jobs in groups.values():
        first = jobs[0]
        train_sizes = {job.train_size for job in jobs}
        validated_times = {job.validated_at for job in jobs}
        base_shas = {job.validation_base_sha for job in jobs}
        validation_shas = {job.validation_sha for job in jobs}
        validation_tree_shas = {job.validation_tree_sha for job in jobs}
        gate_policy_shas = {job.validation_gate_policy_sha for job in jobs}
        environment_shas = {job.validation_environment_sha for job in jobs}
        train_identity_shas = {job.validation_train_sha for job in jobs}
        expected_size = first.train_size
        complete = bool(
            first.train_id
            and expected_size == len(jobs)
            and len(train_sizes) == 1
            and len(validated_times) == 1
            and len(base_shas) == 1
            and len(validation_shas) == 1
            and first.validated_at
            and first.validation_base_sha
            and first.validation_sha
            and all(job.validated_head_sha for job in jobs)
        )
        reuse_identity_complete = bool(
            complete
            and len(validation_tree_shas) == 1
            and len(gate_policy_shas) == 1
            and len(environment_shas) == 1
            and len(train_identity_shas) == 1
            and first.validation_tree_sha
            and first.validation_gate_policy_sha
            and first.validation_environment_sha
            and first.validation_train_sha
        )
        summaries.append(
            {
                "train_id": first.train_id or None,
                "train_size": expected_size,
                "job_ids": [job.id for job in jobs],
                "branches": [
                    {
                        "job_id": job.id,
                        "branch": job.branch,
                        "validated_head_sha": job.validated_head_sha,
                    }
                    for job in jobs
                ],
                "validated_at": first.validated_at,
                "validation_base_sha": first.validation_base_sha,
                "validation_sha": first.validation_sha,
                "deploy_eligible": complete,
                "reuse_identity_complete": reuse_identity_complete,
                "validation_tree_sha": first.validation_tree_sha,
                "validation_gate_policy_sha": first.validation_gate_policy_sha,
                "validation_environment_sha": first.validation_environment_sha,
                "validation_train_sha": first.validation_train_sha,
            }
        )
    return summaries


def select_validated_train(
    conn: sqlite3.Connection,
    *,
    train_id: str = "",
) -> tuple[dict[str, Any] | None, list[Job]]:
    """Select one complete validated train without claiming or mutating it."""

    summaries = validated_train_summaries(conn)
    if train_id:
        matches = [summary for summary in summaries if summary["train_id"] == train_id]
        if not matches:
            raise QueueError(f"validated train not found: {train_id}")
        selected: dict[str, Any] | None = matches[0]
    else:
        deployable = [summary for summary in summaries if summary["deploy_eligible"]]
        if len(deployable) > 1:
            ids = ", ".join(str(summary["train_id"]) for summary in deployable)
            raise QueueError(
                f"multiple validated trains are ready; pass --train-id with one of: {ids}"
            )
        selected = deployable[0] if deployable else None
        if selected is None and summaries:
            raise QueueError(
                "validated jobs lack complete train identity; cancel and enqueue a fresh train"
            )
    if selected is None:
        return None, []
    if not selected["deploy_eligible"]:
        raise QueueError(
            f"validated train has incomplete identity: {selected['train_id']}"
        )
    jobs = list_jobs_fifo(conn, status="validated")
    return selected, [job for job in jobs if job.train_id == selected["train_id"]]


def claim_deploy_batch(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    ttl_minutes: int = 30,
    train_id: str = "",
    operation_label: str = "deploy",
) -> list[Job]:
    """Claim one exact validated train, or queued jobs when none is pending."""

    owner = owner or default_owner()
    with immediate(conn):
        lock = _acquire_runner_lock(conn, owner=owner, ttl_minutes=ttl_minutes)
        # Acquiring the lock can reap a dead owner and park a marker-bearing
        # orphan as needs_reconcile *inside this same transaction*. A deploy
        # targets the same push refs, so re-check here — not only in the CLI
        # pre-check — and refuse fail-closed if a reconcile is now pending
        # (mirrors claim_all_queued's guard, closing the claim/reconcile TOCTOU).
        if deploy_reconcile_pending(conn):
            _release_lock_token(conn, owner=owner, token=lock.token)
            return []
        selected, validated_jobs = select_validated_train(conn, train_id=train_id)
        if selected is not None:
            jobs = validated_jobs
        else:
            jobs = list_jobs_fifo(conn, status="queued")
        if not jobs:
            _release_lock_token(conn, owner=owner, token=lock.token)
            return []
        job_ids = [job.id for job in jobs]
        expected_status = "validated" if selected is not None else "queued"
        placeholders = ",".join("?" for _ in job_ids)
        cur = conn.execute(
            f"""
            UPDATE deploy_queue
            SET status = 'in_progress', started_at = ?, note = ?, claim_token = ?,
                cancel_requested_at = ''
            WHERE id IN ({placeholders}) AND status = ?
            """,
            (
                utc_now(),
                f"claimed by mergetrain {operation_label} runner",
                lock.token,
                *job_ids,
                expected_status,
            ),
        )
        if cur.rowcount != len(job_ids):
            raise QueueError("validated train changed while it was being claimed")
        _record_run_event(
            conn,
            claim_token=lock.token,
            phase="claiming",
            state="active",
            message=f"{operation_label.capitalize()} runner claimed {len(job_ids)} job(s)",
        )
    return [get_job(conn, job_id) for job_id in job_ids]


def _record_run_event(
    conn: sqlite3.Connection,
    *,
    phase: str,
    state: str,
    message: str,
    claim_token: str = "",
    job_id: int | None = None,
    detail: str = "",
) -> RunEvent:
    cur = conn.execute(
        """
        INSERT INTO run_events (
          claim_token, job_id, phase, state, message, detail, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (claim_token, job_id, phase, state, message, detail, utc_now()),
    )
    conn.execute(
        """
        DELETE FROM run_events
        WHERE id <= (SELECT COALESCE(MAX(id), 0) - 5000 FROM run_events)
        """
    )
    row = conn.execute("SELECT * FROM run_events WHERE id = ?", (cur.lastrowid,)).fetchone()
    assert row is not None
    return RunEvent.from_row(row)


def record_run_event(
    conn: sqlite3.Connection,
    *,
    phase: str,
    state: str,
    message: str,
    claim_token: str = "",
    job_id: int | None = None,
    detail: str = "",
) -> RunEvent:
    """Append a structured runner event without exposing the lease token."""

    if conn.in_transaction:
        return _record_run_event(
            conn,
            phase=phase,
            state=state,
            message=message,
            claim_token=claim_token,
            job_id=job_id,
            detail=detail,
        )
    with immediate(conn):
        return _record_run_event(
            conn,
            phase=phase,
            state=state,
            message=message,
            claim_token=claim_token,
            job_id=job_id,
            detail=detail,
        )


def list_run_events(
    conn: sqlite3.Connection,
    *,
    limit: int = 40,
    claim_token: str | None = None,
    after_id: int | None = None,
    job_ids: Sequence[int] | None = None,
) -> list[RunEvent]:
    limit = max(1, min(int(limit), 200))
    resume_requested = after_id is not None
    after_id = max(0, int(after_id or 0))
    if claim_token is not None and job_ids is not None:
        raise QueueError("claim_token and job_ids event filters are mutually exclusive")

    conditions: list[str] = []
    values: list[Any] = []
    if resume_requested:
        conditions.append("id > ?")
        values.append(after_id)
    if claim_token is not None:
        conditions.append("claim_token = ?")
        values.append(claim_token)
    elif job_ids is not None:
        normalized_ids = tuple(dict.fromkeys(int(job_id) for job_id in job_ids))
        if not normalized_ids:
            return []
        id_placeholders = ",".join("?" for _ in normalized_ids)
        token_rows = conn.execute(
            f"""
            SELECT DISTINCT claim_token FROM run_events
            WHERE job_id IN ({id_placeholders}) AND claim_token != ''
            UNION
            SELECT DISTINCT claim_token FROM deploy_queue
            WHERE id IN ({id_placeholders}) AND claim_token != ''
            """,
            (*normalized_ids, *normalized_ids),
        ).fetchall()
        tokens = tuple(str(row["claim_token"]) for row in token_rows)
        scope = [f"job_id IN ({id_placeholders})"]
        scope_values: list[Any] = list(normalized_ids)
        if tokens:
            token_placeholders = ",".join("?" for _ in tokens)
            scope.append(
                f"(job_id IS NULL AND claim_token IN ({token_placeholders}))"
            )
            scope_values.extend(tokens)
        conditions.append(f"({' OR '.join(scope)})")
        values.extend(scope_values)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    if resume_requested:
        rows = conn.execute(
            f"SELECT * FROM run_events {where} ORDER BY id ASC LIMIT ?",
            (*values, limit),
        ).fetchall()
        return [RunEvent.from_row(row) for row in rows]
    rows = conn.execute(
        f"SELECT * FROM run_events {where} ORDER BY id DESC LIMIT ?",
        (*values, limit),
    ).fetchall()
    return [RunEvent.from_row(row) for row in reversed(rows)]


def pack_push_refs(push_refs: Sequence[str]) -> str:
    """Normalize a push-ref set into the durable marker's newline-joined form."""
    return "\n".join(str(ref) for ref in push_refs)


def unpack_push_refs(packed: str) -> list[str]:
    """Inverse of :func:`pack_push_refs`; ``[]`` for an empty/legacy marker."""
    return [ref for ref in packed.split("\n") if ref] if packed else []


def record_pending_push(
    conn: sqlite3.Connection,
    *,
    job_ids: Sequence[int],
    deploy_sha: str,
    claim_token: str,
    remote: str = "",
    push_refs: Sequence[str] = (),
) -> None:
    """Durably record intent to push ``deploy_sha`` before the remote is touched.

    Writes ``pending_deploy_sha`` and ``push_status='pending'`` for exactly the
    in-progress jobs this runner owns, in one IMMEDIATE transaction. With
    ``PRAGMA synchronous=FULL`` the commit is fsync-durable before ``git push``,
    so a later crash can prove a push was attempted for this sha (0.3.0 Phase 1;
    see docs/proposals/0.3.0-recovery.md).

    The push *target* — the remote and the normalized push-ref set — is recorded
    alongside the sha so a later ``reconcile`` evaluates the refs the interrupted
    push actually targeted, not whatever the current config now says (#84,
    defect 3).
    """
    ids = [int(job_id) for job_id in job_ids]
    if not ids or not deploy_sha or not claim_token:
        return
    placeholders = ",".join("?" for _ in ids)
    with immediate(conn):
        conn.execute(
            f"""
            UPDATE deploy_queue
            SET pending_deploy_sha = ?, push_status = 'pending',
                pending_deploy_remote = ?, pending_deploy_refs = ?
            WHERE id IN ({placeholders})
              AND status = 'in_progress'
              AND claim_token = ?
            """,
            (deploy_sha, remote, pack_push_refs(push_refs), *ids, claim_token),
        )


def clear_rejected_push(
    conn: sqlite3.Connection,
    *,
    job_ids: Sequence[int],
    claim_token: str,
) -> None:
    """Clear a pending marker after an unambiguous remote rejection.

    A protected-branch or permission rejection proves that no ref update landed,
    so retaining the write-ahead marker would misclassify the eventual ``blocked``
    row as a reconcile conflict.  Fence the cleanup to the runner that recorded
    the marker; a stale owner must never erase another runner's recovery evidence.
    """

    ids = [int(job_id) for job_id in job_ids]
    if not ids or not claim_token:
        return
    placeholders = ",".join("?" for _ in ids)
    with immediate(conn):
        cur = conn.execute(
            f"""
            UPDATE deploy_queue
            SET pending_deploy_sha = '', pending_deploy_remote = '',
                pending_deploy_refs = '', push_status = 'failed'
            WHERE id IN ({placeholders})
              AND status = 'in_progress'
              AND claim_token = ?
            """,
            (*ids, claim_token),
        )
        if cur.rowcount != len(ids):
            raise LostLease("pending push is no longer owned by this runner")


def mark_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    status: str,
    deploy_sha: str = "",
    log_path: str = "",
    note: str = "",
    push_status: str = "",
    verify_status: str = "",
    train_id: str = "",
    train_size: int = 0,
    validated_at: str = "",
    validation_base_sha: str = "",
    validation_sha: str = "",
    validated_head_sha: str = "",
    validation_tree_sha: str = "",
    validation_gate_policy_sha: str = "",
    validation_environment_sha: str = "",
    validation_train_sha: str = "",
    reused_validation_sha: str = "",
    conflict_with: str = "",
    expected_claim_token: str | None = None,
    expected_status: str = "",
) -> Job:
    if status not in ALL_STATUSES:
        raise QueueError(f"unknown job status: {status}")
    if push_status and push_status not in PUSH_STATUSES:
        raise QueueError(f"unknown push status: {push_status}")
    if verify_status and verify_status not in VERIFY_STATUSES:
        raise QueueError(f"unknown verify status: {verify_status}")
    finished_at = utc_now() if status in TERMINAL_STATUSES or status in {"blocked", "failed"} else ""
    where = "id = ?"
    where_values: list[Any] = [job_id]
    if expected_claim_token is not None:
        if not expected_claim_token:
            raise LostLease("job claim token is missing")
        where += " AND status = 'in_progress' AND claim_token = ?"
        where_values.append(expected_claim_token)
        # A marker-bearing ambiguous push must park for reconcile even when a
        # cancel arrived during remote I/O.  Reconcile, not the cancel request,
        # decides whether the push landed.  Preserve the request below so an
        # unlanded push can still honor it.
        if status not in {"canceled", "deployed", "needs_reconcile"}:
            where += " AND cancel_requested_at = ''"
    if expected_status:
        # Compare-and-swap on the source status, so a concurrent transition (e.g.
        # a cancel landing during reconcile's multi-second remote I/O) is never
        # silently overwritten by a stale recovery decision.
        where += " AND status = ?"
        where_values.append(expected_status)
    with immediate(conn):
        cur = conn.execute(
            f"""
            UPDATE deploy_queue
            SET status = ?, deploy_sha = COALESCE(NULLIF(?, ''), deploy_sha),
                log_path = COALESCE(NULLIF(?, ''), log_path), note = ?, finished_at = ?,
                push_status = COALESCE(NULLIF(?, ''), push_status),
                verify_status = COALESCE(NULLIF(?, ''), verify_status),
                train_id = COALESCE(NULLIF(?, ''), train_id),
                train_size = COALESCE(NULLIF(?, 0), train_size),
                validated_at = COALESCE(NULLIF(?, ''), validated_at),
                validation_base_sha = COALESCE(NULLIF(?, ''), validation_base_sha),
                validation_sha = COALESCE(NULLIF(?, ''), validation_sha),
                validated_head_sha = COALESCE(NULLIF(?, ''), validated_head_sha),
                validation_tree_sha = COALESCE(NULLIF(?, ''), validation_tree_sha),
                validation_gate_policy_sha = COALESCE(NULLIF(?, ''), validation_gate_policy_sha),
                validation_environment_sha = COALESCE(NULLIF(?, ''), validation_environment_sha),
                validation_train_sha = COALESCE(NULLIF(?, ''), validation_train_sha),
                reused_validation_sha = COALESCE(NULLIF(?, ''), reused_validation_sha),
                conflict_with = ?,
                claim_token = CASE WHEN ? = 'in_progress' THEN claim_token ELSE '' END,
                cancel_requested_at = CASE
                    WHEN ? IN ('in_progress', 'canceled', 'needs_reconcile')
                    THEN cancel_requested_at
                    ELSE ''
                END,
                pending_deploy_sha = CASE
                    WHEN ? IN ('deployed', 'canceled', 'queued') THEN ''
                    ELSE pending_deploy_sha
                END,
                pending_deploy_remote = CASE
                    WHEN ? IN ('deployed', 'canceled', 'queued') THEN ''
                    ELSE pending_deploy_remote
                END,
                pending_deploy_refs = CASE
                    WHEN ? IN ('deployed', 'canceled', 'queued') THEN ''
                    ELSE pending_deploy_refs
                END
            WHERE {where}
            """,
            (
                status,
                deploy_sha,
                log_path,
                note,
                finished_at,
                push_status,
                verify_status,
                train_id,
                train_size,
                validated_at,
                validation_base_sha,
                validation_sha,
                validated_head_sha,
                validation_tree_sha,
                validation_gate_policy_sha,
                validation_environment_sha,
                validation_train_sha,
                reused_validation_sha,
                conflict_with,
                status,
                status,
                status,
                status,
                status,
                *where_values,
            ),
        )
        if cur.rowcount != 1:
            row = conn.execute(
                "SELECT status, claim_token, cancel_requested_at FROM deploy_queue WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is not None and str(row["cancel_requested_at"] or ""):
                raise CancellationRequested(f"cancellation requested for job {job_id}")
            if expected_status and expected_claim_token is None:
                raise QueueError(
                    f"job {job_id} left '{expected_status}' before the write "
                    "(raced by a concurrent transition)"
                )
            raise LostLease(f"job {job_id} is no longer owned by this runner")
    return get_job(conn, job_id)


def cancel_job(conn: sqlite3.Connection, job_id: int, *, note: str = "") -> Job:
    job = get_job(conn, job_id)
    if job.status in TERMINAL_STATUSES:
        raise QueueError(f"terminal job cannot be canceled: {job_id}")
    if job.status == "needs_reconcile":
        raise QueueError(
            f"job {job_id} has an unresolved push; run 'mergetrain reconcile --apply' "
            "before canceling"
        )
    if job.status == "in_progress":
        requested_at = utc_now()
        cancel_note = note or "cancellation requested by user"
        with immediate(conn):
            if job.claim_token:
                conn.execute(
                    """
                    UPDATE deploy_queue
                    SET cancel_requested_at = ?, note = ?
                    WHERE status = 'in_progress' AND claim_token = ?
                    """,
                    (requested_at, cancel_note, job.claim_token),
                )
            else:
                conn.execute(
                    """
                    UPDATE deploy_queue
                    SET cancel_requested_at = ?, note = ?
                    WHERE id = ? AND status = 'in_progress'
                    """,
                    (requested_at, cancel_note, job_id),
                )
        return get_job(conn, job_id)
    if job.status == "validated" and job.train_id:
        cancel_note = note or f"validated train {job.train_id} canceled by user"
        with immediate(conn):
            conn.execute(
                """
                UPDATE deploy_queue
                SET status = 'canceled', note = ?, finished_at = ?
                WHERE status = 'validated' AND train_id = ?
                """,
                (cancel_note, utc_now(), job.train_id),
            )
        return get_job(conn, job_id)
    return mark_job(
        conn,
        job_id,
        status="canceled",
        note=note or "canceled by user",
        expected_status=job.status,
    )


def dismiss_job(conn: sqlite3.Connection, job_id: int, *, note: str = "") -> Job:
    """Non-destructively clear a blocked/failed job that has been superseded.

    A blocked/failed job never lands and never self-clears, yet it keeps
    ``doctor``'s ``next_action`` pinned to ``fix_blocked_job`` — hiding a
    ready validated train — and blocks re-enqueue of its branch. Once its work
    is fixed (and enqueued afresh, or abandoned), dismissing it moves it to the
    terminal ``canceled`` state so the queue reflects reality. Unlike
    ``cancel``, this only ever touches an already-failed outcome — never queued
    or in-progress work — so it is safe for an agent to run unattended.
    """

    job = get_job(conn, job_id)
    if job.status not in {"blocked", "failed"}:
        raise QueueError(
            f"only a blocked or failed job can be dismissed (job {job_id} is "
            f"{job.status}); use cancel for queued/in-progress work"
        )
    return mark_job(
        conn,
        job_id,
        status="canceled",
        note=note or f"dismissed superseded {job.status} job",
    )


def terminal_branch_candidates(conn: sqlite3.Connection) -> list[dict[str, str]]:
    terminal_placeholders = _status_placeholders(TERMINAL_STATUSES)
    active_placeholders = _status_placeholders(ACTIVE_STATUSES)
    rows = conn.execute(
        f"""
        SELECT terminal.branch, terminal.id AS job_id, terminal.status
        FROM deploy_queue AS terminal
        WHERE terminal.status IN ({terminal_placeholders})
          AND terminal.id = (
            SELECT MAX(latest.id)
            FROM deploy_queue AS latest
            WHERE latest.branch = terminal.branch
              AND latest.status IN ({terminal_placeholders})
          )
          AND NOT EXISTS (
            SELECT 1
            FROM deploy_queue AS active
            WHERE active.branch = terminal.branch
              AND active.status IN ({active_placeholders})
          )
        ORDER BY terminal.id ASC
        """,
        (*TERMINAL_STATUSES, *TERMINAL_STATUSES, *ACTIVE_STATUSES),
    ).fetchall()
    return [{"branch": str(row["branch"]), "job_id": str(row["job_id"]), "status": str(row["status"])} for row in rows]
