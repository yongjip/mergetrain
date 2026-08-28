"""Append-only evidence for operator-invoked recovery commands."""

from __future__ import annotations

import sqlite3
from uuid import uuid4

from ..errors import QueueError
from ..models import RecoveryOperationEvent
from .transactions import immediate, utc_now

RECOVERY_OPERATIONS = ("reconcile", "recover")
RECOVERY_OPERATION_STATES = (
    "started",
    "success",
    "conflict",
    "lock_held",
    "remote_unreachable",
    "error",
)


def _insert_operation_event(
    conn: sqlite3.Connection,
    *,
    invocation_id: str,
    operation: str,
    state: str,
    applied: bool,
    detail: str,
) -> RecoveryOperationEvent:
    cur = conn.execute(
        """
        INSERT INTO recovery_operation_events (
          invocation_id, operation, state, applied, detail, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (invocation_id, operation, state, int(applied), detail, utc_now()),
    )
    row = conn.execute(
        "SELECT * FROM recovery_operation_events WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
    assert row is not None
    return RecoveryOperationEvent.from_row(row)


def start_recovery_operation(
    conn: sqlite3.Connection, *, operation: str, applied: bool
) -> RecoveryOperationEvent:
    """Durably record an invocation before recovery touches remote state."""

    if operation not in RECOVERY_OPERATIONS:
        raise QueueError(f"unsupported recovery operation: {operation}")
    invocation_id = uuid4().hex
    with immediate(conn):
        return _insert_operation_event(
            conn,
            invocation_id=invocation_id,
            operation=operation,
            state="started",
            applied=applied,
            detail="",
        )


def finish_recovery_operation(
    conn: sqlite3.Connection,
    invocation_id: str,
    *,
    state: str,
    detail: str = "",
) -> RecoveryOperationEvent:
    """Append the single terminal event for a started invocation."""

    if state not in RECOVERY_OPERATION_STATES or state == "started":
        raise QueueError(f"invalid recovery operation terminal state: {state}")
    with immediate(conn):
        started = conn.execute(
            """
            SELECT operation, applied FROM recovery_operation_events
            WHERE invocation_id = ? AND state = 'started'
            ORDER BY id ASC LIMIT 1
            """,
            (invocation_id,),
        ).fetchone()
        if started is None:
            raise QueueError(
                f"recovery operation invocation is not started: {invocation_id}"
            )
        terminal = conn.execute(
            """
            SELECT 1 FROM recovery_operation_events
            WHERE invocation_id = ? AND state != 'started'
            LIMIT 1
            """,
            (invocation_id,),
        ).fetchone()
        if terminal is not None:
            raise QueueError(
                f"recovery operation invocation is already finished: {invocation_id}"
            )
        return _insert_operation_event(
            conn,
            invocation_id=invocation_id,
            operation=str(started["operation"]),
            state=state,
            applied=bool(started["applied"]),
            detail=detail,
        )


def list_recovery_operation_events(
    conn: sqlite3.Connection, *, since: str = ""
) -> list[RecoveryOperationEvent]:
    """Read the tracking baseline and invocations that began in the window."""

    if since:
        rows = conn.execute(
            """
            SELECT * FROM recovery_operation_events
            WHERE operation = 'tracking'
               OR invocation_id IN (
                 SELECT invocation_id FROM recovery_operation_events
                 WHERE state = 'started' AND operation != 'tracking'
                   AND created_at >= ?
               )
            ORDER BY id ASC
            """,
            (since,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM recovery_operation_events ORDER BY id ASC"
        ).fetchall()
    return [RecoveryOperationEvent.from_row(row) for row in rows]
