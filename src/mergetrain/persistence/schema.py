"""SQLite schema definition, forward-safety, and ordered migrations."""

from __future__ import annotations

import sqlite3

from ..errors import QueueError
from .transactions import immediate, utc_now

SCHEMA_VERSION = 14


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
          approval_destination_sha TEXT NOT NULL DEFAULT '',
          approval_execution_policy_sha TEXT NOT NULL DEFAULT '',
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
          pending_deploy_refs TEXT NOT NULL DEFAULT '',
          pending_deploy_destination_sha TEXT NOT NULL DEFAULT '',
          supersession_id TEXT NOT NULL DEFAULT '',
          supersedes_train_id TEXT NOT NULL DEFAULT ''
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
            9: (),
            10: (
                ("deploy_queue", "supersession_id", "TEXT NOT NULL DEFAULT ''"),
                (
                    "deploy_queue",
                    "supersedes_train_id",
                    "TEXT NOT NULL DEFAULT ''",
                ),
            ),
            11: (),
            12: (
                (
                    "deploy_queue",
                    "approval_destination_sha",
                    "TEXT NOT NULL DEFAULT ''",
                ),
            ),
            13: (
                (
                    "deploy_queue",
                    "pending_deploy_destination_sha",
                    "TEXT NOT NULL DEFAULT ''",
                ),
            ),
            14: (
                (
                    "deploy_queue",
                    "approval_execution_policy_sha",
                    "TEXT NOT NULL DEFAULT ''",
                ),
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
            if next_version == 9:
                # Queue reads overwhelmingly filter by status or by one branch.
                # Include id so FIFO/status history queries retain their natural
                # order without a second sort; auto claims get their own covering
                # prefix because they sit on the write-lock hot path.
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS deploy_queue_status_id_idx "
                    "ON deploy_queue(status, id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS deploy_queue_status_auto_id_idx "
                    "ON deploy_queue(status, auto_deploy, id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS deploy_queue_branch_status_id_idx "
                    "ON deploy_queue(branch, status, id)"
                )
            if next_version == 10:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS deploy_queue_supersession_id_idx "
                    "ON deploy_queue(supersession_id, id)"
                )
            if next_version == 11:
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
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS recovery_operation_invocation_idx "
                    "ON recovery_operation_events(invocation_id, id)"
                )
                baseline = conn.execute(
                    """
                    SELECT 1 FROM recovery_operation_events
                    WHERE operation = 'tracking' LIMIT 1
                    """
                ).fetchone()
                if baseline is None:
                    detail = (
                        "schema_version=11;history_complete="
                        f"{int(not had_existing_history)}"
                    )
                    conn.execute(
                        """
                        INSERT INTO recovery_operation_events (
                          invocation_id, operation, state, applied, detail, created_at
                        ) VALUES ('', 'tracking', 'started', 0, ?, ?)
                        """,
                        (detail, utc_now()),
                    )
            conn.execute(f"PRAGMA user_version = {next_version}")
