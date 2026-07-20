"""Aggregate every registered repo into one machine-wide read-only snapshot.

The hub owns no correctness-critical state (RFC #23): each repo entry here is
built by loading that repo's own config and opening its own SQLite database
read-only. A repo that is missing, unreadable, or on a different schema is
reported as an isolated error card — one broken repo never breaks the page,
and observing a repo never creates or migrates anything inside it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_config
from .snapshot import build_dashboard_snapshot
from .store import utc_now


def _display_path(path: str) -> str:
    """Home-relative display form; the hub identifies repos, so their location
    is the payload's subject rather than incidental leakage."""

    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return path


def _repo_entry(registered: dict[str, Any]) -> dict[str, Any]:
    raw_path = str(registered.get("path") or "")
    entry: dict[str, Any] = {"path": _display_path(raw_path), "ok": False}
    # Isolation is the point: any failure in one repo becomes that repo's
    # error card instead of a hub-wide crash, so the catch is deliberately broad.
    try:
        repo = Path(raw_path)
        if not repo.is_dir():
            entry["error"] = "repo directory is missing"
            return entry
        config = load_config(repo=repo)
        entry["name"] = config.project.name
        if not config.config_exists:
            entry["error"] = "no .mergetrain.yaml in this repo"
            return entry
        if not Path(config.state.db).is_file():
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
            return entry
        entry.update(
            {
                "ok": True,
                "snapshot": build_dashboard_snapshot(config, read_only=True),
            }
        )
        return entry
    except Exception as exc:  # noqa: BLE001 - per-repo isolation is the contract
        entry["error"] = str(exc) or exc.__class__.__name__
        return entry


def build_hub_snapshot(registered: list[dict[str, Any]]) -> dict[str, Any]:
    repos = [_repo_entry(item) for item in registered]
    return {
        "ok": True,
        "hub": True,
        "generated_at": utc_now(),
        "repo_count": len(repos),
        "repos": repos,
    }
