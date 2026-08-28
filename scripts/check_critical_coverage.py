#!/usr/bin/env python3
"""Fail when correctness-critical modules fall below their coverage floors."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CRITICAL_MINIMUMS = {
    "src/mergetrain/atomic_push.py": 94.0,
    "src/mergetrain/command_runner.py": 85.0,
    "src/mergetrain/gate_runner.py": 94.0,
    "src/mergetrain/git_ops.py": 85.0,
    "src/mergetrain/git_runner.py": 91.0,
    "src/mergetrain/persistence/claims.py": 90.0,
    "src/mergetrain/persistence/connection.py": 87.0,
    "src/mergetrain/persistence/events.py": 94.0,
    "src/mergetrain/persistence/jobs.py": 90.0,
    "src/mergetrain/persistence/leases.py": 82.0,
    "src/mergetrain/persistence/operations.py": 94.0,
    "src/mergetrain/persistence/recovery.py": 86.0,
    "src/mergetrain/persistence/schema.py": 95.0,
    "src/mergetrain/persistence/transactions.py": 90.0,
    "src/mergetrain/recovery.py": 93.0,
    "src/mergetrain/reuse.py": 94.0,
    "src/mergetrain/validation_reuse.py": 83.0,
    "src/mergetrain/worktree_manager.py": 91.0,
}


@dataclass(frozen=True, slots=True)
class CoverageResult:
    path: str
    minimum: float
    actual: float | None

    @property
    def passed(self) -> bool:
        return self.actual is not None and self.actual >= self.minimum


def _normalized(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _percent(summary: Mapping[str, Any]) -> float:
    statements = int(summary["num_statements"])
    covered = int(summary["covered_lines"])
    if statements <= 0 or covered < 0 or covered > statements:
        raise ValueError("invalid coverage summary counts")
    return covered / statements * 100.0


def assess_critical_coverage(payload: Mapping[str, Any]) -> list[CoverageResult]:
    raw_files = payload.get("files")
    if not isinstance(raw_files, Mapping):
        raise ValueError("coverage JSON has no files object")

    summaries: list[tuple[str, Mapping[str, Any]]] = []
    for raw_path, raw_entry in raw_files.items():
        if not isinstance(raw_path, str) or not isinstance(raw_entry, Mapping):
            raise ValueError("coverage JSON contains an invalid file entry")
        summary = raw_entry.get("summary")
        if not isinstance(summary, Mapping):
            raise ValueError(f"coverage JSON has no summary for {raw_path}")
        summaries.append((_normalized(raw_path), summary))

    results: list[CoverageResult] = []
    for expected, minimum in CRITICAL_MINIMUMS.items():
        matches = [
            summary
            for path, summary in summaries
            if path == expected or path.endswith(f"/{expected}")
        ]
        if len(matches) > 1:
            raise ValueError(f"coverage JSON has multiple entries for {expected}")
        actual = _percent(matches[0]) if matches else None
        results.append(CoverageResult(path=expected, minimum=minimum, actual=actual))
    return results


def _load(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("coverage JSON root must be an object")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(
            "usage: check_critical_coverage.py COVERAGE_JSON",
            file=sys.stderr,
        )
        return 2

    path = Path(args[0])
    try:
        results = assess_critical_coverage(_load(path))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"critical coverage check failed: {exc}", file=sys.stderr)
        return 2

    failed = False
    for result in results:
        actual = "missing" if result.actual is None else f"{result.actual:.2f}%"
        state = "PASS" if result.passed else "FAIL"
        print(f"{state} {result.path}: {actual} (minimum {result.minimum:.1f}%)")
        failed = failed or not result.passed
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
