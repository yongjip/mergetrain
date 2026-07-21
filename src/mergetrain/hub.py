"""Aggregate every registered repo into one machine-wide read-only snapshot.

The hub owns no correctness-critical state (RFC #23): each repo entry here is
built by loading that repo's own config and opening its own SQLite database
read-only. A repo that is missing, unreadable, or on a different schema is
reported as an isolated error card — one broken repo never breaks the page,
and observing a repo never creates directories, queue databases, rows, or
schema migrations inside it. (Honest limit: a WAL reader may create or
refresh SQLite's sidecar ``-shm``/``-wal`` files next to the database.)

The dashboard rebuilds this payload once per second per connected client, and
almost every rebuild reads unchanged files. ``HubSnapshotCache`` turns those
rebuilds into a handful of ``stat`` calls: a repo's entry is reused while its
config file and queue database (including the SQLite ``-wal``, which every
commit touches) have identical mtime/size fingerprints. Registry-derived
fields like the daemon flag are attached outside the cache, so flipping them
never serves a stale value.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from .config import load_config
from .registry import DEFAULT_CONFIG_NAME
from .snapshot import build_dashboard_snapshot
from .store import utc_now


def display_path(path: str) -> str:
    """Home-relative display form; the hub identifies repos, so their location
    is the payload's subject rather than incidental leakage."""

    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return path


def _fingerprint(*paths: str | Path) -> tuple[Any, ...]:
    parts: list[Any] = []
    for item in paths:
        try:
            stat = os.stat(item)
            parts.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            parts.append(None)
    return tuple(parts)


class HubSnapshotCache:
    """Reuse per-repo entries while their on-disk fingerprints are unchanged.

    Only successful entries (live snapshot or "no queue yet") are cached;
    error entries are cheap to rebuild and their causes are transient. The
    cache is shared across dashboard handler threads, hence the lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}

    def get(self, raw_path: str, config_fp: tuple[Any, ...]) -> dict[str, Any] | None:
        with self._lock:
            cached = self._entries.get(raw_path)
        if cached is None or cached["config_fp"] != config_fp:
            return None
        if _fingerprint(*cached["db_paths"]) != cached["db_fp"]:
            return None
        return dict(cached["entry"])

    def put(
        self,
        raw_path: str,
        *,
        config_fp: tuple[Any, ...],
        db_paths: tuple[str, ...],
        entry: dict[str, Any],
    ) -> None:
        with self._lock:
            self._entries[raw_path] = {
                "config_fp": config_fp,
                "db_paths": db_paths,
                "db_fp": _fingerprint(*db_paths),
                "entry": dict(entry),
            }


def _repo_entry(raw_path: str, cache: HubSnapshotCache | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": display_path(raw_path)}
    # Isolation is the point: any failure in one repo becomes that repo's
    # error card instead of a hub-wide crash, so the catch is deliberately broad.
    try:
        repo = Path(raw_path)
        if not repo.is_dir():
            entry.update(ok=False, error="repo directory is missing")
            return entry
        config_fp = _fingerprint(repo / DEFAULT_CONFIG_NAME)
        if cache is not None:
            cached = cache.get(raw_path, config_fp)
            if cached is not None:
                return cached
        config = load_config(repo=repo)
        entry["name"] = config.project.name
        if not config.config_exists:
            entry.update(ok=False, error="no .mergetrain.yaml in this repo")
            return entry
        db = Path(config.state.db)
        db_paths = (str(db), f"{db}-wal")
        if not db.is_file():
            # A registered repo with no queue yet is a normal state, not an
            # error — and the hub must not create the database to find out.
            entry.update(
                {
                    "ok": True,
                    "empty": True,
                    "project": {
                        "name": config.project.name,
                        "integration_ref": config.git.integration_ref,
                        "remote": config.git.remote,
                        "push_refs": list(config.git.push_refs),
                        "terminology": config.terminology.to_dict(),
                    },
                }
            )
        else:
            entry.update(
                {
                    "ok": True,
                    "snapshot": build_dashboard_snapshot(config, read_only=True),
                }
            )
        if cache is not None:
            cache.put(raw_path, config_fp=config_fp, db_paths=db_paths, entry=entry)
        return entry
    except Exception as exc:  # noqa: BLE001 - per-repo isolation is the contract
        entry.update(ok=False, error=str(exc) or exc.__class__.__name__)
        return entry


def build_hub_snapshot(
    registered: list[dict[str, Any]],
    *,
    cache: HubSnapshotCache | None = None,
) -> dict[str, Any]:
    repos = []
    for item in registered:
        entry = _repo_entry(str(item.get("path") or ""), cache)
        # Registry-derived, not repo-derived: attach after the cache so a
        # flag flip is visible on the very next snapshot.
        entry["daemon"] = bool(item.get("daemon", True))
        repos.append(entry)
    return {
        "ok": True,
        "hub": True,
        "generated_at": utc_now(),
        "repo_count": len(repos),
        "repos": repos,
    }


def build_hub_snapshot_safe(
    registry: str | None = None,
    *,
    cache: HubSnapshotCache | None = None,
) -> dict[str, Any]:
    """Hub snapshot that degrades instead of dying when the roster is broken.

    The per-repo isolation contract must extend to the registry file itself:
    a corrupt or unreadable roster becomes a visible ``registry_error`` on an
    otherwise-empty payload, never a dead ``/api/snapshot`` and a silently
    frozen page.
    """

    from .registry import load_registry

    try:
        registered = load_registry(registry)
    except Exception as exc:  # noqa: BLE001 - degrade, never kill the board
        return {
            "ok": True,
            "hub": True,
            "generated_at": utc_now(),
            "repo_count": 0,
            "repos": [],
            "registry_error": str(exc) or exc.__class__.__name__,
        }
    return build_hub_snapshot(registered, cache=cache)
