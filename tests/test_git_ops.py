from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from mergetrain.git_ops import _parse_worktree_porcelain, git_dirty_paths


class DirtyPathTests(unittest.TestCase):
    def test_the_first_dirty_path_keeps_its_first_character(self) -> None:
        # Stripping the whole porcelain output took the first line's leading
        # status space, so enqueue reported "pp.py" for a modified app.py.
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            names = ("a.txt", "app.py", "lib.py")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
            )
            for name in names:
                (repo / name).write_text("one\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
            for name in names:
                (repo / name).write_text("two\n", encoding="utf-8")

            self.assertEqual(git_dirty_paths(repo), list(names))


class WorktreePorcelainTests(unittest.TestCase):
    def test_path_cannot_forge_a_branch_attribute(self) -> None:
        output = (
            "worktree /tmp/decoy\nbranch refs/heads/agent/target\0"
            "HEAD abc\0branch refs/heads/agent/decoy\0\0"
        )

        self.assertEqual(_parse_worktree_porcelain(output, "agent/target"), ())

    def test_path_whitespace_is_preserved(self) -> None:
        output = (
            "worktree /tmp/path with a trailing space \0"
            "HEAD abc\0branch refs/heads/agent/target\0\0"
        )

        self.assertEqual(
            _parse_worktree_porcelain(output, "agent/target"),
            (Path("/tmp/path with a trailing space "),),
        )

    def test_prunable_registration_is_not_live(self) -> None:
        output = (
            "worktree /tmp/gone\0HEAD abc\0branch refs/heads/agent/target\0"
            "prunable gitdir file points to non-existent location\0\0"
        )

        self.assertEqual(_parse_worktree_porcelain(output, "agent/target"), ())

    def test_multiple_live_matches_are_preserved_for_fail_closed_resolution(self) -> None:
        output = (
            "worktree /tmp/one\0HEAD abc\0branch refs/heads/agent/target\0\0"
            "worktree /tmp/two\0HEAD def\0branch refs/heads/agent/target\0\0"
        )

        self.assertEqual(
            _parse_worktree_porcelain(output, "agent/target"),
            (Path("/tmp/one"), Path("/tmp/two")),
        )


if __name__ == "__main__":
    unittest.main()
