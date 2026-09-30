from __future__ import annotations

import unittest

from mergetrain.path_gates import (
    any_path_matches,
    parse_name_status_z,
    path_matches,
    validate_gate_path_pattern,
)


class PathPatternTests(unittest.TestCase):
    def test_segment_globs_and_recursive_segments_are_deterministic(self) -> None:
        cases = [
            ("src/*.py", "src/app.py", True),
            ("src/*.py", "src/pkg/app.py", False),
            ("src/**", "src/app.py", True),
            ("src/**", "src/pkg/app.py", True),
            ("src/**/*.py", "src/app.py", True),
            ("src/**/*.py", "src/pkg/app.py", True),
            ("**/*.md", "README.md", True),
            ("**/*.md", "docs/guide.md", True),
            ("docs/?.md", "docs/가.md", True),
            ("docs/[ab].md", "docs/a.md", True),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(path_matches(pattern, path), expected)

        self.assertTrue(
            any_path_matches(
                ("src/**", "pyproject.toml"),
                ("docs/read me.md", "src/패키지/app.py"),
            )
        )

    def test_a_leading_caret_negates_a_class_as_git_and_the_shells_read_it(self) -> None:
        # fnmatch negates a class only with '!' and took '^' for a member,
        # which inverted a pattern written for Git or a shell.
        cases = [
            ("src/[^_]*.py", "src/main.py", True),
            ("src/[^_]*.py", "src/_private.py", False),
            ("src/[!_]*.py", "src/main.py", True),
            ("src/[!_]*.py", "src/_private.py", False),
            ("docs/[^]].md", "docs/a.md", True),
            ("docs/[^]].md", "docs/].md", False),
            ("docs/[a^].md", "docs/^.md", True),
            ("docs/[a[^].md", "docs/^.md", True),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(path_matches(pattern, path), expected)

    def test_invalid_patterns_are_rejected(self) -> None:
        invalid = (
            "",
            "/src/**",
            "C:/src/**",
            "./src/**",
            "../src/**",
            "src//*.py",
            "src/",
            r"src\**",
            "src/foo**bar",
            "src/\0bad",
        )
        for pattern in invalid:
            with self.subTest(pattern=pattern):
                with self.assertRaises(ValueError):
                    validate_gate_path_pattern(pattern)


class NameStatusParserTests(unittest.TestCase):
    def test_parses_deletes_renames_spaces_and_non_ascii_paths(self) -> None:
        output = (
            "D\0deleted file.txt\0"
            "R100\0old/name.py\0new/이름.py\0"
            "M\0src/app.py\0"
        )
        self.assertEqual(
            parse_name_status_z(output),
            (
                "deleted file.txt",
                "old/name.py",
                "new/이름.py",
                "src/app.py",
            ),
        )

    def test_deduplicates_paths_and_rejects_malformed_output(self) -> None:
        self.assertEqual(
            parse_name_status_z("M\0same.txt\0M\0same.txt\0"),
            ("same.txt",),
        )
        for output in (
            "M\0missing-terminator",
            "R100\0only-old\0",
            "surprise\0file.txt\0",
            "M\0\0",
        ):
            with self.subTest(output=output):
                with self.assertRaises(ValueError):
                    parse_name_status_z(output)


if __name__ == "__main__":
    unittest.main()
