"""Append-only run-event writes, retention, and scoped event reads."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from ..errors import QueueError
from ..models import RunEvent
from .transactions import immediate, utc_now

RUN_EVENT_RETENTION = 5000


def list_history_events(
    conn: sqlite3.Connection, *, since: str = ""
) -> list[RunEvent]:
    where = "WHERE created_at >= ?" if since else ""
    values: tuple[Any, ...] = (since,) if since else ()
    rows = conn.execute(
        f"SELECT * FROM run_events {where} ORDER BY id ASC",
        values,
    ).fetchall()
    return [RunEvent.from_row(row) for row in rows]


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
        WHERE id <= (
          SELECT COALESCE(MAX(id), 0) - ? FROM run_events
        )
        """,
        (RUN_EVENT_RETENTION,),
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
