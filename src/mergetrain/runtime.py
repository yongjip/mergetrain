"""Inspect the installed package provenance for local CLI diagnostics."""

from __future__ import annotations

import json
import subprocess
from importlib import metadata
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname


def _direct_url(distribution: metadata.Distribution) -> dict[str, Any]:
    try:
        value = distribution.read_text("direct_url.json")
    except (AttributeError, OSError):
        return {}
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _file_url_path(value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        return None
    try:
        return Path(url2pathname(unquote(parsed.path))).expanduser().resolve()
    except (OSError, ValueError):
        return None


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _installed_package_path(distribution: metadata.Distribution) -> Path | None:
    try:
        files = distribution.files or ()
    except (AttributeError, OSError):
        files = ()
    for entry in files:
        pure = PurePath(str(entry))
        if pure.parts[-2:] != ("mergetrain", "__init__.py"):
            continue
        try:
            return Path(distribution.locate_file(entry)).resolve().parent
        except (AttributeError, OSError, TypeError, ValueError):
            return None
    return None


def _matches_distribution(package_path: Path, distribution: metadata.Distribution) -> bool:
    installed_path = _installed_package_path(distribution)
    if installed_path is not None:
        return installed_path == package_path
    try:
        distribution_root = Path(distribution.locate_file("")).resolve()
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    return _is_within(package_path, distribution_root)


def _git_output(source_path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_path), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_provenance(source_path: Path) -> tuple[str | None, bool | None]:
    commit = _git_output(source_path, "rev-parse", "HEAD")
    if not commit:
        return None, None
    status = _git_output(source_path, "status", "--porcelain=v1", "--untracked-files=normal")
    return commit, None if status is None else bool(status)


def _source_checkout_path(package_path: Path) -> Path | None:
    root_value = _git_output(package_path, "rev-parse", "--show-toplevel")
    if not root_value:
        return None
    try:
        root = Path(root_value).resolve()
        package_init = (package_path / "__init__.py").resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    tracked = _git_output(root, "ls-files", "--error-unmatch", "--", package_init.as_posix())
    return root if tracked is not None else None


def runtime_provenance(*, package_path: Path | None = None) -> dict[str, Any]:
    """Return provenance for the package that this process actually imported.

    PEP 610 ``direct_url.json`` is authoritative for editable installs. A
    distribution is only described as a wheel when its installed files match
    the imported package, so an unrelated distribution on ``sys.path`` is not
    mistaken for the running code.
    """

    imported_path = (package_path or Path(__file__).parent).resolve()
    payload: dict[str, Any] = {
        "distribution_version": None,
        "package_path": str(imported_path),
        "install_mode": "unknown",
        "source_path": None,
        "source_commit": None,
        "source_dirty": None,
    }
    try:
        distribution: metadata.Distribution | None = metadata.distribution("mergetrain")
    except (metadata.PackageNotFoundError, OSError, ValueError):
        distribution = None

    if distribution is not None:
        version = getattr(distribution, "version", None)
        payload["distribution_version"] = str(version) if version else None
        direct_url = _direct_url(distribution)
        dir_info = direct_url.get("dir_info")
        editable = isinstance(dir_info, dict) and dir_info.get("editable") is True
        source_path = _file_url_path(direct_url.get("url")) if editable else None

        if editable and source_path is not None and _is_within(imported_path, source_path):
            payload["install_mode"] = "editable"
            payload["source_path"] = str(source_path)
        elif not editable and _matches_distribution(imported_path, distribution):
            payload["install_mode"] = "wheel"

        vcs_info = direct_url.get("vcs_info")
        if payload["install_mode"] != "unknown" and isinstance(vcs_info, dict):
            commit_id = vcs_info.get("commit_id")
            if isinstance(commit_id, str) and commit_id:
                payload["source_commit"] = commit_id

    source_path_value = payload["source_path"]
    if isinstance(source_path_value, str):
        source_checkout = Path(source_path_value)
    else:
        source_checkout = _source_checkout_path(imported_path)
        if source_checkout is not None:
            payload["source_path"] = str(source_checkout)

    if source_checkout is not None:
        commit, dirty = _git_provenance(source_checkout)
        payload["source_commit"] = commit or payload["source_commit"]
        payload["source_dirty"] = dirty

    return payload
