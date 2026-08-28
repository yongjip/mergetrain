from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TypedDict

from scripts.check_critical_coverage import (
    CRITICAL_MINIMUMS,
    assess_critical_coverage,
    main,
)


class CoverageSummary(TypedDict):
    covered_lines: int
    num_statements: int


class CoverageFile(TypedDict):
    summary: CoverageSummary


class CoveragePayload(TypedDict):
    files: dict[str, CoverageFile]


def summary(percent: float, *, statements: int = 1000) -> CoverageSummary:
    return {
        "covered_lines": round(statements * percent / 100),
        "num_statements": statements,
    }


def payload(
    *, overrides: dict[str, float] | None = None, windows_paths: bool = False
) -> CoveragePayload:
    overrides = overrides or {}
    files: dict[str, CoverageFile] = {}
    for path, minimum in CRITICAL_MINIMUMS.items():
        rendered = path.replace("/", "\\") if windows_paths else path
        files[rendered] = {"summary": summary(overrides.get(path, minimum))}
    return {"files": files}


class CriticalCoverageTests(unittest.TestCase):
    def test_thresholds_pass_at_the_floor_on_posix_and_windows_paths(self) -> None:
        for windows_paths in (False, True):
            with self.subTest(windows_paths=windows_paths):
                results = assess_critical_coverage(payload(windows_paths=windows_paths))
                self.assertTrue(all(result.passed for result in results))

    def test_below_floor_and_missing_module_fail(self) -> None:
        target = "src/mergetrain/recovery.py"
        below = payload(overrides={target: CRITICAL_MINIMUMS[target] - 1})
        below_results = assess_critical_coverage(below)
        failed = next(result for result in below_results if result.path == target)
        self.assertFalse(failed.passed)

        missing = payload()
        del missing["files"][target]
        missing_results = assess_critical_coverage(missing)
        absent = next(result for result in missing_results if result.path == target)
        self.assertIsNone(absent.actual)
        self.assertFalse(absent.passed)

    def test_duplicate_module_entries_are_rejected(self) -> None:
        data = payload()
        target = "src/mergetrain/git_runner.py"
        data["files"][f"/checkout/{target}"] = data["files"][target]
        with self.assertRaisesRegex(ValueError, "multiple entries"):
            assess_critical_coverage(data)

    def test_cli_returns_one_for_a_policy_failure(self) -> None:
        target = "src/mergetrain/persistence/jobs.py"
        data = payload(overrides={target: 50.0})
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "coverage.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = main([str(path)])
        self.assertEqual(result, 1)
        self.assertIn(f"FAIL {target}", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_cli_rejects_invalid_coverage_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "coverage.json"
            path.write_text("[]", encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = main([str(path)])
        self.assertEqual(result, 2)
        self.assertIn("root must be an object", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
