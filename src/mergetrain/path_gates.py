"""Deterministic path matching for path-aware pre-push gates."""

from __future__ import annotations

import re
from collections.abc import Iterable
from fnmatch import fnmatchcase
from functools import cache

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:/")
_NAME_STATUS = re.compile(r"^[A-Z][0-9]*$")


def validate_gate_path_pattern(pattern: str) -> str:
    """Validate one repository-relative POSIX glob and return it unchanged.

    Patterns are deliberately independent from the host platform. ``*``, ``?``,
    and character classes match within one path segment; a segment containing
    only ``**`` matches zero or more complete segments.
    """

    if not pattern:
        raise ValueError("must not be empty")
    if "\0" in pattern:
        raise ValueError("must not contain NUL")
    if "\\" in pattern:
        raise ValueError("must use POSIX '/' separators")
    if pattern.startswith("/") or _WINDOWS_ABSOLUTE.match(pattern):
        raise ValueError("must be repository-relative")
    parts = pattern.split("/")
    if any(part == "" for part in parts):
        raise ValueError("must not contain empty path segments")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("must not contain '.' or '..' path segments")
    if any("**" in part and part != "**" for part in parts):
        raise ValueError("'**' must occupy a complete path segment")
    return pattern


def path_matches(pattern: str, path: str) -> bool:
    """Return whether a repository-relative path matches a validated pattern."""

    pattern_parts = tuple(pattern.split("/"))
    path_parts = tuple(path.split("/"))

    @cache
    def match(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        part = pattern_parts[pattern_index]
        if part == "**":
            return match(pattern_index + 1, path_index) or (
                path_index < len(path_parts)
                and match(pattern_index, path_index + 1)
            )
        return (
            path_index < len(path_parts)
            and fnmatchcase(path_parts[path_index], part)
            and match(pattern_index + 1, path_index + 1)
        )

    return match(0, 0)


def any_path_matches(patterns: Iterable[str], paths: Iterable[str]) -> bool:
    """Return true when any changed path matches any configured pattern."""

    pattern_list = tuple(patterns)
    return any(
        path_matches(pattern, path)
        for path in paths
        for pattern in pattern_list
    )


def parse_name_status_z(output: str) -> tuple[str, ...]:
    """Parse ``git diff --name-status -z`` and retain both rename endpoints."""

    if not output:
        return ()
    fields = output.split("\0")
    if fields[-1] != "":
        raise ValueError("name-status output is not NUL terminated")
    fields.pop()

    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not _NAME_STATUS.fullmatch(status):
            raise ValueError("name-status output contains an invalid status")
        path_count = 2 if status[0] in {"R", "C"} else 1
        if index + path_count > len(fields):
            raise ValueError("name-status output ended before its path fields")
        for path in fields[index : index + path_count]:
            if not path or "\0" in path:
                raise ValueError("name-status output contains an invalid path")
            paths.append(path)
        index += path_count

    return tuple(dict.fromkeys(paths))
