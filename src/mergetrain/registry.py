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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:  # POSIX advisory locking; Windows support is tracked in issue #33.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platform
    fcntl = None  # type: ignore[assignment]

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


def same_repo(stored: str, candidate: str | Path) -> bool:
    """True when two paths name the same physical directory.

    Registry identity must be the directory, not the path string: on
    case-insensitive filesystems (macOS APFS) and through symlinks, two
    different strings reach one repo, and a policy flag such as
    ``daemon: false`` must follow the repo, not the spelling.
    """

    candidate_str = str(candidate)
    if stored == candidate_str:
        return True
    try:
        return os.path.samefile(stored, candidate_str)
    except OSError:
        return False


@contextmanager
def _mutation_lock(target: Path) -> Iterator[None]:
    """Serialize registry read-modify-write cycles across processes.

    ``save_registry``'s atomic replace prevents torn files but not lost
    updates; without this lock a concurrent ``hub add`` could write back a
    stale roster and silently resurrect ``daemon: true`` on an excluded
    repo.
    """

    if fcntl is None:  # pragma: no cover - non-POSIX platform
        yield
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
            raw_daemon = item.get("daemon", True)
            entries.append(
                {
                    "path": item["path"],
                    "added_at": str(item.get("added_at") or ""),
                    # Pre-flag rosters default to daemon-eligible, matching the
                    # behavior those entries already had. A hand-edited
                    # non-boolean value (e.g. the string "false") fails SAFE to
                    # excluded rather than being truthy-coerced into eligible.
                    "daemon": raw_daemon if isinstance(raw_daemon, bool) else False,
                }
            )
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


def add_repo(
    repo: str | Path,
    path: str | Path | None = None,
    *,
    daemon: bool | None = None,
) -> dict[str, Any]:
    """Register one repo. Requires its config so typos fail loudly, not quietly.

    ``daemon`` upserts the hub-daemon eligibility flag: ``False`` excludes the
    repo from every ``hub daemon`` sweep (policy-level opt-out for repos that
    must never see unattended deploys), ``True`` re-enables it, and ``None``
    leaves an existing entry unchanged (new entries default to eligible).
    Re-running ``add`` on a registered repo is the supported way to flip the
    flag.
    """

    resolved = _normalize(repo)
    if not resolved.is_dir():
        raise QueueError(f"not a directory: {resolved}")
    if not (resolved / DEFAULT_CONFIG_NAME).is_file():
        raise QueueError(
            f"no {DEFAULT_CONFIG_NAME} in {resolved}; run `mergetrain init` there first"
        )
    target = Path(path) if path else registry_path()
    with _mutation_lock(target):
        entries = load_registry(target)
        for entry in entries:
            if same_repo(entry["path"], resolved):
                if daemon is not None and entry.get("daemon", True) != daemon:
                    entry["daemon"] = daemon
                    save_registry(entries, target)
                return entry
        entry = {
            "path": str(resolved),
            "added_at": utc_now(),
            "daemon": True if daemon is None else daemon,
        }
        entries.append(entry)
        save_registry(entries, target)
        return entry


def remove_repo(repo: str | Path, path: str | Path | None = None) -> bool:
    """Deregister one repo; the repo's own state is untouched."""

    resolved = _normalize(repo)
    target = Path(path) if path else registry_path()
    with _mutation_lock(target):
        entries = load_registry(target)
        remaining = [entry for entry in entries if not same_repo(entry["path"], resolved)]
        if len(remaining) == len(entries):
            return False
        save_registry(remaining, target)
        return True
