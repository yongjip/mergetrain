"""Aggregate every registered repo into one machine-wide read-only snapshot.

The hub owns no correctness-critical state (RFC #23): each repo entry here is
built by loading that repo's own config and opening its own SQLite database
read-only. A repo that is missing, unreadable, or on a different schema is
reported as an isolated error entry — one broken repo never breaks the read,
and observing a repo never creates directories, queue databases, rows, or
schema migrations inside it. (Honest limit: a WAL reader may create or
refresh SQLite's sidecar ``-shm``/``-wal`` files next to the database.)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import MergetrainConfig, load_config
from .persistence.transactions import utc_now
from .snapshot import build_queue_summary, build_repo_snapshot

RepoView = Callable[[MergetrainConfig], dict[str, Any]]


def display_path(path: str) -> str:
    """Home-relative display form; the hub identifies repos, so their location
    is the payload's subject rather than incidental leakage."""

    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return path


def _repo_entry(raw_path: str, key: str, build: RepoView) -> dict[str, Any]:
    """Read one repo, with ``build``'s view of its queue under ``key``."""

    entry: dict[str, Any] = {"path": display_path(raw_path)}
    # Isolation is the point: any failure in one repo becomes that repo's
    # error card instead of a hub-wide crash, so the catch is deliberately broad.
    try:
        repo = Path(raw_path)
        if not repo.is_dir():
            entry.update(ok=False, error="repo directory is missing")
            return entry
        config = load_config(repo=repo)
        entry["name"] = config.project.name
        if not config.config_exists:
            entry.update(ok=False, error="no .mergetrain.yaml in this repo")
            return entry
        db = Path(config.state.db)
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
                    },
                }
            )
        else:
            entry.update({"ok": True, key: build(config)})
        return entry
    except Exception as exc:  # noqa: BLE001 - per-repo isolation is the contract
        entry.update(ok=False, error=str(exc) or exc.__class__.__name__)
        return entry


def _hub_payload(
    registered: list[dict[str, Any]], key: str, build: RepoView, **view: str
) -> dict[str, Any]:
    repos = []
    for item in registered:
        entry = _repo_entry(str(item.get("path") or ""), key, build)
        # Registry-derived, not repo-derived.
        entry["daemon"] = bool(item.get("daemon", True))
        repos.append(entry)
    return {
        "ok": True,
        "hub": True,
        **view,
        "generated_at": utc_now(),
        "repo_count": len(repos),
        "repos": repos,
    }


def build_hub_snapshot(registered: list[dict[str, Any]]) -> dict[str, Any]:
    return _hub_payload(registered, "snapshot", build_repo_snapshot)


def build_hub_summary(registered: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a compact machine-wide queue summary for routine agent reads.

    Each repo gets the queue summary, without the full snapshot's history.
    """

    return _hub_payload(registered, "summary", build_queue_summary, view="summary")
