"""Shared time helpers and the fail-honest SQLite write transaction policy."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from ..errors import QueueBusy


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


def _busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database is busy" in text


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[None]:
    """Open the one write transaction every queue mutation goes through.

    Contention is translated here rather than at each call site: a raw
    ``sqlite3.OperationalError`` is not a ``MergetrainError``, so it used to
    reach the runner's defensive boundary and finalize the job ``failed`` -- the
    status that means the branch is at fault. ``QueueBusy`` is retryable and
    typed, so every caller can tell "the database was busy" from "this work is
    broken".
    """

    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if _busy(exc):
            raise QueueBusy(
                f"queue database is busy; another process held the write lock: {exc}"
            ) from exc
        raise
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        try:
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            if _busy(exc):
                raise QueueBusy(
                    f"queue database is busy; the commit could not complete: {exc}"
                ) from exc
            raise
