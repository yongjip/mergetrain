"""Machine-level hub registry: which repos the hub aggregates.

The registry is deliberately dumb — a JSON list of absolute repo paths in the
user's config directory. It carries no queue state; every registered repo
remains sovereign over its own SQLite database, lock, and crash-recovery
markers (RFC #23). Deleting this file loses nothing but the hub's roster.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import QueueError
from .store import utc_now

DEFAULT_CONFIG_NAME = ".mergetrain.yaml"
REGISTRY_VERSION = 1


def registry_path() -> Path:
    override = os.environ.get("MERGETRAIN_HUB_REGISTRY")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "mergetrain" / "repos.json"


def _normalize(repo: str | Path) -> Path:
    return Path(repo).expanduser().resolve()


def load_registry(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Return registered repo entries, oldest first. A missing file is empty."""

    target = Path(path) if path else registry_path()
    if not target.is_file():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueueError(f"hub registry is unreadable: {target}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("repos"), list):
        raise QueueError(f"hub registry has an unexpected shape: {target}")
    entries: list[dict[str, Any]] = []
    for item in data["repos"]:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"]:
            entries.append({"path": item["path"], "added_at": str(item.get("added_at") or "")})
    return entries


def save_registry(entries: list[dict[str, Any]], path: str | Path | None = None) -> Path:
    target = Path(path) if path else registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": REGISTRY_VERSION, "repos": entries}
    # Atomic replace so a crash mid-write can never truncate the roster.
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, prefix=".repos-", suffix=".tmp", delete=False
    )
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(handle.name, target)
    except Exception:
        Path(handle.name).unlink(missing_ok=True)
        raise
    return target


def add_repo(repo: str | Path, path: str | Path | None = None) -> dict[str, Any]:
    """Register one repo. Requires its config so typos fail loudly, not quietly."""

    resolved = _normalize(repo)
    if not resolved.is_dir():
        raise QueueError(f"not a directory: {resolved}")
    if not (resolved / DEFAULT_CONFIG_NAME).is_file():
        raise QueueError(
            f"no {DEFAULT_CONFIG_NAME} in {resolved}; run `mergetrain init` there first"
        )
    entries = load_registry(path)
    for entry in entries:
        if entry["path"] == str(resolved):
            return entry
    entry = {"path": str(resolved), "added_at": utc_now()}
    entries.append(entry)
    save_registry(entries, path)
    return entry


def remove_repo(repo: str | Path, path: str | Path | None = None) -> bool:
    """Deregister one repo; the repo's own state is untouched."""

    resolved = str(_normalize(repo))
    entries = load_registry(path)
    remaining = [entry for entry in entries if entry["path"] != resolved]
    if len(remaining) == len(entries):
        return False
    save_registry(remaining, path)
    return True
