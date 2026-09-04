"""Lifetime-owned, read-only change detection for dashboard snapshots."""

from __future__ import annotations

import sqlite3
import stat
import threading
from pathlib import Path

from .store import connect


class QueueChangeMonitor:
    """Compare SQLite data_version on one connection, never across connections.

    WAL space can be recycled without changing its size or the main file.
    A persistent observer detects those commits without reading job history or
    holding a read transaction open. One monitor belongs to a cached repository,
    not a browser. Its lock permits HTTP handler threads to share the connection.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._identity: tuple[Path, int, int] | None = None
        self._generation = object()

    def _reset(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn = None
        self._identity = None
        self._generation = object()

    def token(self, db: str | Path) -> tuple[object, int | None]:
        path = Path(db).expanduser().absolute()
        with self._lock:
            try:
                try:
                    info = path.stat()
                except FileNotFoundError:
                    info = None
                if info is None or not stat.S_ISREG(info.st_mode):
                    if self._conn is not None:
                        self._reset()
                    return (self._generation, None)
                identity = (path, info.st_dev, info.st_ino)
                if self._conn is None or self._identity != identity:
                    self._reset()
                    self._conn = connect(path, read_only=True, check_same_thread=False)
                    self._identity = identity
                row = self._conn.execute('PRAGMA data_version').fetchone()
                assert row is not None
                return (self._generation, int(row[0]))
            except Exception:
                # A failed observer must not leave old evidence usable on the
                # next request. A fresh connection gets a new local generation.
                self._reset()
                raise

    def close(self) -> None:
        with self._lock:
            self._reset()
