"""SQLite storage and runner lock management."""

from __future__ import annotations

import getpass
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .errors import LockHeld, QueueError
from .models import ACTIVE_STATUSES, ALL_STATUSES, Job, RunnerLock, TERMINAL_STATUSES

RUNNER_LOCK_NAME = "runner"


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


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    # WAL is not available for in-memory DBs, but SQLite quietly returns memory.
    conn.execute("PRAGMA journal_mode = WAL")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
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
          auto_deploy INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS locks (
          name TEXT PRIMARY KEY,
          owner TEXT NOT NULL,
          worktree_path TEXT NOT NULL DEFAULT '',
          head_sha TEXT NOT NULL DEFAULT '',
          acquired_at TEXT NOT NULL,
          expires_at TEXT NOT NULL
        );
        """
    )
    try:
        conn.execute("ALTER TABLE deploy_queue ADD COLUMN auto_deploy INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def default_owner() -> str:
    return f"{getpass.getuser()}:{os.getpid()}"


def owner_liveness(owner: str) -> str:
    try:
        pid_text = owner.rsplit(":", 1)[1]
        pid = int(pid_text)
    except Exception:
        return Liveness.UNKNOWN
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
            raise QueueError(f"branch already has an active job: {branch}")
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
        job_id = int(cur.lastrowid)
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
    result = {status: 0 for status in ALL_STATUSES}
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
    return result


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


def _in_progress_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM deploy_queue WHERE status = 'in_progress'"
    ).fetchone()
    return int(row["n"])


def _requeue_orphans(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE deploy_queue
        SET status = 'queued', started_at = '', note = 're-queued by trainyard (previous runner gone)'
        WHERE status = 'in_progress'
        """
    )


def acquire_runner_lock(
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
    with immediate(conn):
        row = conn.execute("SELECT * FROM locks WHERE name = ?", (name,)).fetchone()
        if row is not None:
            current_owner = str(row["owner"])
            live = owner_liveness(current_owner)
            expired = _parse_utc(str(row["expires_at"])) <= datetime.now(timezone.utc)
            if live == Liveness.DEAD:
                _delete_lock(conn, name=name)
            elif live == Liveness.ALIVE:
                raise LockHeld(f"runner lock is held by alive owner: {current_owner}")
            elif not expired:
                raise LockHeld(f"runner lock is held by unknown owner: {current_owner}")
            elif _in_progress_count(conn) > 0:
                raise LockHeld("expired runner lock has unknown owner and in-progress jobs")
            else:
                _delete_lock(conn, name=name)
        else:
            if _in_progress_count(conn) > 0:
                _requeue_orphans(conn)
        conn.execute(
            """
            INSERT INTO locks (name, owner, worktree_path, head_sha, acquired_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, owner, worktree_path, head_sha, now, expires),
        )
    lock = get_lock(conn, name=name)
    assert lock is not None
    return lock


def refresh_runner_lock(
    conn: sqlite3.Connection,
    *,
    owner: str,
    ttl_minutes: int = 30,
    name: str = RUNNER_LOCK_NAME,
    worktree_path: str = "",
    head_sha: str = "",
) -> None:
    with immediate(conn):
        conn.execute(
            """
            UPDATE locks
            SET expires_at = ?, worktree_path = ?, head_sha = ?
            WHERE name = ? AND owner = ?
            """,
            (_plus_minutes(ttl_minutes), worktree_path, head_sha, name, owner),
        )


def release_runner_lock(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    name: str = RUNNER_LOCK_NAME,
) -> bool:
    with immediate(conn):
        if owner is None:
            cur = conn.execute("DELETE FROM locks WHERE name = ?", (name,))
        else:
            cur = conn.execute("DELETE FROM locks WHERE name = ? AND owner = ?", (name, owner))
    return cur.rowcount > 0


def claim_next_job(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    ttl_minutes: int = 30,
) -> Job | None:
    owner = owner or default_owner()
    acquire_runner_lock(conn, owner=owner, ttl_minutes=ttl_minutes)
    with immediate(conn):
        row = conn.execute(
            "SELECT * FROM deploy_queue WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute("DELETE FROM locks WHERE name = ? AND owner = ?", (RUNNER_LOCK_NAME, owner))
            return None
        job_id = int(row["id"])
        conn.execute(
            "UPDATE deploy_queue SET status = 'in_progress', started_at = ?, note = ? WHERE id = ?",
            (utc_now(), "claimed by trainyard runner", job_id),
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
    acquire_runner_lock(conn, owner=owner, ttl_minutes=ttl_minutes)
    jobs = list_jobs_fifo(conn, status="queued", auto_only=auto_only)
    if not jobs:
        release_runner_lock(conn, owner=owner)
    return jobs


def mark_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    status: str,
    deploy_sha: str = "",
    log_path: str = "",
    note: str = "",
) -> Job:
    if status not in ALL_STATUSES:
        raise QueueError(f"unknown job status: {status}")
    finished_at = utc_now() if status in TERMINAL_STATUSES or status in {"blocked", "failed"} else ""
    with immediate(conn):
        conn.execute(
            """
            UPDATE deploy_queue
            SET status = ?, deploy_sha = COALESCE(NULLIF(?, ''), deploy_sha),
                log_path = COALESCE(NULLIF(?, ''), log_path), note = ?, finished_at = ?
            WHERE id = ?
            """,
            (status, deploy_sha, log_path, note, finished_at, job_id),
        )
    return get_job(conn, job_id)


def cancel_job(conn: sqlite3.Connection, job_id: int, *, note: str = "") -> Job:
    job = get_job(conn, job_id)
    if job.status in TERMINAL_STATUSES:
        raise QueueError(f"terminal job cannot be canceled: {job_id}")
    return mark_job(conn, job_id, status="canceled", note=note or "canceled by user")


def terminal_branch_candidates(conn: sqlite3.Connection) -> list[dict[str, str]]:
    placeholders = _status_placeholders(TERMINAL_STATUSES)
    rows = conn.execute(
        f"""
        SELECT branch, MAX(id) AS job_id, MAX(status) AS status
        FROM deploy_queue
        WHERE status IN ({placeholders})
        GROUP BY branch
        ORDER BY MAX(id) ASC
        """,
        TERMINAL_STATUSES,
    ).fetchall()
    return [{"branch": str(row["branch"]), "job_id": str(row["job_id"]), "status": str(row["status"])} for row in rows]
