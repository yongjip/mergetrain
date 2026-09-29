"""SQLite schema definition, forward-safety, and upgrades of older databases."""

from __future__ import annotations

import sqlite3

from ..errors import QueueError
from .transactions import immediate, utc_now

# Bump whenever a column, index, or backfill is added, so an older binary
# refuses a database it cannot fully understand instead of acting on it.
SCHEMA_VERSION = 15

# Each column of the two evolving tables, defined once. A fresh database is
# created from these lists; an older one gains whichever columns it lacks, so
# every added column must carry a DEFAULT. Columns without one (the base
# columns) existed from the first schema and can never be added later.
_DEPLOY_QUEUE_COLUMNS = (
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("task", "TEXT NOT NULL"),
    ("branch", "TEXT NOT NULL"),
    ("worktree_path", "TEXT NOT NULL DEFAULT ''"),
    ("status", "TEXT NOT NULL DEFAULT 'queued'"),
    ("base_sha", "TEXT NOT NULL DEFAULT ''"),
    ("head_sha", "TEXT NOT NULL DEFAULT ''"),
    ("deploy_sha", "TEXT NOT NULL DEFAULT ''"),
    ("requested_at", "TEXT NOT NULL"),
    ("started_at", "TEXT NOT NULL DEFAULT ''"),
    ("finished_at", "TEXT NOT NULL DEFAULT ''"),
    ("log_path", "TEXT NOT NULL DEFAULT ''"),
    ("note", "TEXT NOT NULL DEFAULT ''"),
    ("push_status", "TEXT NOT NULL DEFAULT 'not_run'"),
    ("verify_status", "TEXT NOT NULL DEFAULT 'not_run'"),
    ("auto_deploy", "INTEGER NOT NULL DEFAULT 0"),
    ("approval_destination_sha", "TEXT NOT NULL DEFAULT ''"),
    ("approval_execution_policy_sha", "TEXT NOT NULL DEFAULT ''"),
    ("train_id", "TEXT NOT NULL DEFAULT ''"),
    ("train_size", "INTEGER NOT NULL DEFAULT 0"),
    ("validated_at", "TEXT NOT NULL DEFAULT ''"),
    ("validation_base_sha", "TEXT NOT NULL DEFAULT ''"),
    ("validation_sha", "TEXT NOT NULL DEFAULT ''"),
    ("validated_head_sha", "TEXT NOT NULL DEFAULT ''"),
    ("validation_tree_sha", "TEXT NOT NULL DEFAULT ''"),
    ("validation_gate_policy_sha", "TEXT NOT NULL DEFAULT ''"),
    ("validation_environment_sha", "TEXT NOT NULL DEFAULT ''"),
    ("validation_train_sha", "TEXT NOT NULL DEFAULT ''"),
    ("reused_validation_sha", "TEXT NOT NULL DEFAULT ''"),
    ("claim_token", "TEXT NOT NULL DEFAULT ''"),
    ("cancel_requested_at", "TEXT NOT NULL DEFAULT ''"),
    ("pending_deploy_sha", "TEXT NOT NULL DEFAULT ''"),
    ("conflict_with", "TEXT NOT NULL DEFAULT ''"),
    ("pending_deploy_remote", "TEXT NOT NULL DEFAULT ''"),
    ("pending_deploy_refs", "TEXT NOT NULL DEFAULT ''"),
    ("pending_deploy_destination_sha", "TEXT NOT NULL DEFAULT ''"),
    ("deployment_id", "TEXT NOT NULL DEFAULT ''"),
    ("deployment_destination_sha", "TEXT NOT NULL DEFAULT ''"),
    ("verification_policy_sha", "TEXT NOT NULL DEFAULT ''"),
    ("supersession_id", "TEXT NOT NULL DEFAULT ''"),
    ("supersedes_train_id", "TEXT NOT NULL DEFAULT ''"),
)
_LOCKS_COLUMNS = (
    ("name", "TEXT PRIMARY KEY"),
    ("owner", "TEXT NOT NULL"),
    ("worktree_path", "TEXT NOT NULL DEFAULT ''"),
    ("head_sha", "TEXT NOT NULL DEFAULT ''"),
    ("acquired_at", "TEXT NOT NULL"),
    ("heartbeat_at", "TEXT NOT NULL DEFAULT ''"),
    ("expires_at", "TEXT NOT NULL"),
    ("token", "TEXT NOT NULL DEFAULT ''"),
)
_TABLES = (("deploy_queue", _DEPLOY_QUEUE_COLUMNS), ("locks", _LOCKS_COLUMNS))

# Created after every column exists: an index on a column that an older
# database still lacks would fail the upgrade.
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS run_events_created_at_idx ON run_events(created_at, id)",
    "CREATE INDEX IF NOT EXISTS run_events_claim_idx ON run_events(claim_token, id)",
    # Queue reads overwhelmingly filter by status or by one branch. Include id
    # so FIFO/status history queries keep their natural order without a second
    # sort; auto claims get their own covering prefix because they sit on the
    # write-lock hot path.
    "CREATE INDEX IF NOT EXISTS deploy_queue_status_id_idx ON deploy_queue(status, id)",
    "CREATE INDEX IF NOT EXISTS deploy_queue_status_auto_id_idx "
    "ON deploy_queue(status, auto_deploy, id)",
    "CREATE INDEX IF NOT EXISTS deploy_queue_branch_status_id_idx "
    "ON deploy_queue(branch, status, id)",
    "CREATE INDEX IF NOT EXISTS deploy_queue_supersession_id_idx "
    "ON deploy_queue(supersession_id, id)",
    "CREATE INDEX IF NOT EXISTS recovery_operation_invocation_idx "
    "ON recovery_operation_events(invocation_id, id)",
    "CREATE INDEX IF NOT EXISTS deploy_queue_deployment_id_idx "
    "ON deploy_queue(deployment_id, id)",
)


def _ensure_columns(
    conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]
) -> None:
    body = ",\n  ".join(f"{name} {definition}" for name, definition in columns)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (\n  {body}\n)")
    present = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns:
        if name in present:
            continue
        if "DEFAULT" not in definition:
            raise QueueError(
                f"queue database table {table} lacks its base column {name}; "
                "it was not created by mergetrain"
            )
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise QueueError(
            f"queue schema version {version} is newer than supported version {SCHEMA_VERSION}"
        )
    if version == SCHEMA_VERSION:
        return

    with immediate(conn):
        # Another process may have migrated the database while this connection
        # waited for the write lock.  Re-read under BEGIN IMMEDIATE so a stale
        # binary can never act on its pre-lock observation and stamp a newer
        # schema back down.
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise QueueError(
                f"queue schema version {version} is newer than supported version "
                f"{SCHEMA_VERSION}"
            )
        if version == SCHEMA_VERSION:
            return

        had_existing_history = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name IN ('deploy_queue', 'run_events')
            LIMIT 1
            """
        ).fetchone() is not None

        for table, columns in _TABLES:
            _ensure_columns(conn, table, columns)
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
            """
            CREATE TABLE IF NOT EXISTS recovery_operation_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              invocation_id TEXT NOT NULL DEFAULT '',
              operation TEXT NOT NULL,
              state TEXT NOT NULL,
              applied INTEGER NOT NULL DEFAULT 0,
              detail TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            )
            """
        )
        for index in _INDEXES:
            conn.execute(index)

        if version < 4:
            # Before version 4 a deployed row had no push/verify outcome; the
            # push had landed, and a verify warning lived only in the note.
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
        if version < 11:
            # Recovery operations are tracked from version 11 on. Record where
            # tracking began, and whether earlier history exists without it.
            baseline = conn.execute(
                """
                SELECT 1 FROM recovery_operation_events
                WHERE operation = 'tracking' LIMIT 1
                """
            ).fetchone()
            if baseline is None:
                detail = f"schema_version=11;history_complete={int(not had_existing_history)}"
                conn.execute(
                    """
                    INSERT INTO recovery_operation_events (
                      invocation_id, operation, state, applied, detail, created_at
                    ) VALUES ('', 'tracking', 'started', 0, ?, ?)
                    """,
                    (detail, utc_now()),
                )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
