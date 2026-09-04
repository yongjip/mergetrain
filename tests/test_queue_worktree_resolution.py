from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mergetrain.commands.queue import (
    _resolve_enqueue_worktree,
    _validate_enqueue_worktree,
)
from mergetrain.errors import QueueError


class EnqueueWorktreeResolutionTests(unittest.TestCase):
    def test_explicit_worktree_wins_without_registry_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            explicit = repo / "explicit"
            with patch(
                "mergetrain.commands.queue.git_worktrees_for_branch"
            ) as worktrees:
                resolved = _resolve_enqueue_worktree(
                    repo=repo,
                    branch="agent/target",
                    explicit_worktree=str(explicit),
                )

        self.assertEqual(resolved, explicit.resolve())
        worktrees.assert_not_called()

    def test_missing_repository_fails_with_queue_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing"

            with self.assertRaisesRegex(QueueError, "repository does not exist"):
                _resolve_enqueue_worktree(
                    repo=missing,
                    branch="agent/target",
                    explicit_worktree=None,
                )

    def test_multiple_registered_worktrees_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            with (
                patch("mergetrain.commands.queue.git_current_branch", return_value="main"),
                patch(
                    "mergetrain.commands.queue.git_worktrees_for_branch",
                    return_value=(Path("/tmp/one"), Path("/tmp/two")),
                ),
            ):
                with self.assertRaisesRegex(QueueError, "multiple live worktrees"):
                    _resolve_enqueue_worktree(
                        repo=repo,
                        branch="agent/target",
                        explicit_worktree=None,
                    )

    def test_foreign_repository_worktree_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            owner = root / "owner"
            foreign = root / "foreign"
            for repo in (owner, foreign):
                subprocess.run(
                    ["git", "init", "-q", "--initial-branch=agent/target", str(repo)],
                    check=True,
                )
                subprocess.run(
                    ["git", "config", "user.email", "test@example.invalid"],
                    cwd=repo,
                    check=True,
                )
                subprocess.run(
                    ["git", "config", "user.name", "Test User"],
                    cwd=repo,
                    check=True,
                )
                (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=repo, check=True)
                subprocess.run(["git", "commit", "-qm", "clean"], cwd=repo, check=True)

            with self.assertRaisesRegex(QueueError, "does not belong"):
                _validate_enqueue_worktree(
                    foreign,
                    "agent/target",
                    repo=owner,
                )


if __name__ == "__main__":
    unittest.main()
