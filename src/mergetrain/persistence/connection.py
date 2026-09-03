"""Writable and read-only SQLite connection policy."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from ..errors import QueueError
from .schema import SCHEMA_VERSION, ensure_schema


def _open_read_only(path: Path) -> sqlite3.Connection:
    """Open a strict read-only connection, bootstrapping idle WAL sidecars.

    SQLite cannot open a WAL-mode database with ``mode=ro`` when the last
    writer removed ``-wal``/``-shm`` and the read-only open cannot create them.
    This is common for an idle queue.  Briefly keep a ``mode=rw`` connection in
    ``query_only`` mode while opening the real ``mode=ro`` observer.  The
    bootstrap may create WAL bookkeeping files, but it cannot mutate queue
    rows; the connection returned to callers remains OS-level read-only.
    """

    uri = f"file:{quote(str(path))}"
    observer: sqlite3.Connection | None = None
    try:
        observer = sqlite3.connect(f"{uri}?mode=ro", uri=True)
        observer.execute("PRAGMA busy_timeout = 5000")
        # sqlite3_open() is lazy: an idle WAL failure can surface only when
        # the first database page is read, not while the handle is created.
        observer.execute("PRAGMA user_version").fetchone()
        return observer
    except sqlite3.OperationalError as initial_exc:
        if observer is not None:
            observer.close()
        if "unable to open database file" not in str(initial_exc).lower():
            raise

    bootstrap: sqlite3.Connection | None = None
    observer = None
    try:
        bootstrap = sqlite3.connect(f"{uri}?mode=rw", uri=True)
        bootstrap.execute("PRAGMA query_only = ON")
        bootstrap.execute("PRAGMA busy_timeout = 5000")
        # Force SQLite to initialize the WAL index before the strict observer
        # opens.  Keep bootstrap alive until that observer owns the sidecars.
        bootstrap.execute("PRAGMA user_version").fetchone()
        observer = sqlite3.connect(f"{uri}?mode=ro", uri=True)
        observer.execute("PRAGMA busy_timeout = 5000")
        observer.execute("PRAGMA user_version").fetchone()
        return observer
    except sqlite3.OperationalError as exc:
        if observer is not None:
            observer.close()
        raise QueueError(
            f"cannot observe {path}: an idle WAL database needs writable "
            "directory access to initialize its -wal/-shm sidecars, or "
            f"existing readable sidecars ({exc})"
        ) from exc
    finally:
        if bootstrap is not None:
            bootstrap.close()


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
        conn = _open_read_only(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise QueueError(
                    f"queue schema version {version} does not match supported version "
                    f"{SCHEMA_VERSION}; run 'mergetrain gc --json' inside that repo "
                    "to migrate local queue state"
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
