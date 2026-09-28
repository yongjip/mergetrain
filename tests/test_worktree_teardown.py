"""Worktree teardown removes links as links, never through them (#214).

Git for Windows deletes a worktree by recursing into NTFS junctions, so a
directory a gate linked into the worktree used to be emptied on cleanup. POSIX
Git does not follow symlinks, so the unit tests below stand in a deleter that
does, and run on every OS; Windows CI additionally exercises real junctions.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_runner import SHELL_PYTHON, git, make_demo_repo, py_path

import mergetrain.git_ops as git_ops
from mergetrain.config import load_config
from mergetrain.git_ops import remove_worktree
from mergetrain.git_runner import GitRunner
from mergetrain.store import connect, enqueue_job

# The gate a Windows user would write: an NTFS junction needs no privilege,
# while a Windows symlink does.
LINK_GATE = """\
import os
import sys
from pathlib import Path

link = Path.cwd() / "linked"
target = os.path.abspath(sys.argv[1])
if os.name == "nt":
    import _winapi

    _winapi.CreateJunction(target, str(link))
else:
    link.symlink_to(target, target_is_directory=True)
"""


def link_directory(link: Path, target: Path) -> None:
    """Link a directory the way a gate would: a junction on Windows, else a symlink."""

    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def delete_through_links(path: Path) -> None:
    """Delete a tree the way Git for Windows does: a directory link is a directory."""

    for child in list(path.iterdir()):
        if child.is_dir():  # follows the link, like Git's lstat() of a junction
            delete_through_links(child)
        else:
            child.unlink()
    if path.is_symlink():
        path.unlink()
    else:
        path.rmdir()


class RemoveWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.repo, _ = make_demo_repo(root)
        self.worktree = root / "worktrees" / "demo-mergetrain-1-abc"
        git(self.repo, "worktree", "add", "--detach", str(self.worktree), "main")
        self.external = root / "external"
        self.external.mkdir()
        self.sentinel = self.external / "sentinel.txt"
        self.sentinel.write_text("keep me\n", encoding="utf-8")

    def assert_external_intact(self) -> None:
        self.assertEqual(self.sentinel.read_text(encoding="utf-8"), "keep me\n")

    def test_links_are_gone_before_a_delete_that_follows_them(self) -> None:
        build = self.worktree / "build"
        build.mkdir()
        link_directory(self.worktree / "linked", self.external)
        link_directory(build / "cache", self.external)
        run_command = git_ops.run_command

        def git_for_windows(args, **kwargs):
            if list(args[:3]) == ["git", "worktree", "remove"]:
                delete_through_links(Path(args[-1]))
                args = ["git", "worktree", "prune"]
            return run_command(args, **kwargs)

        with patch.object(git_ops, "run_command", side_effect=git_for_windows):
            remove_worktree(self.repo, self.worktree)

        self.assert_external_intact()
        self.assertFalse(self.worktree.exists())
        self.assertNotIn(
            self.worktree.name,
            git(self.repo, "worktree", "list", "--porcelain"),
        )

    def test_unregistered_directory_falls_back_without_following_links(self) -> None:
        stray = self.worktree.parent / "demo-mergetrain-2-def"
        stray.mkdir()
        link_directory(stray / "linked", self.external)

        remove_worktree(self.repo, stray)

        self.assert_external_intact()
        self.assertFalse(stray.exists())

    def test_worktree_is_kept_when_a_link_cannot_be_removed(self) -> None:
        link = self.worktree / "linked"
        link_directory(link, self.external)
        log = io.StringIO()
        denied = PermissionError(13, "Access is denied", str(link))

        with (
            patch("os.rmdir", side_effect=denied),
            patch("os.unlink", side_effect=denied),
            patch.object(git_ops, "run_command") as run_command,
        ):
            remove_worktree(self.repo, self.worktree, log=log)

        run_command.assert_not_called()
        self.assertTrue(os.path.lexists(link))
        self.assert_external_intact()
        self.assertIn(f"keeping integration worktree: {self.worktree}", log.getvalue())
        self.assertIn("could not remove a link inside it", log.getvalue())


class ValidationTeardownTests(unittest.TestCase):
    def test_validation_keeps_the_directory_a_gate_linked_in(self) -> None:
        # The #214 reproduction: a prepare gate links an ignored path in the
        # worktree to external state, and validation cleanup must not empty it.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            external = root / "external"
            external.mkdir()
            sentinel = external / "sentinel.txt"
            sentinel.write_text("keep me\n", encoding="utf-8")
            gate = root / "link_gate.py"
            gate.write_text(LINK_GATE, encoding="utf-8")
            repo, _ = make_demo_repo(
                root,
                gate_command=f"{SHELL_PYTHON} {py_path(gate)} {py_path(external)}",
            )
            (repo / ".gitignore").write_text("linked\n", encoding="utf-8")
            git(repo, "add", ".gitignore")
            git(repo, "commit", "-m", "ignore gate links")
            git(repo, "push", "origin", "main")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="link gate", branch="feature/a")
                result = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
            finally:
                conn.close()

            self.assertEqual(result.status, "validated", result.note)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me\n")
            self.assertEqual(list(config.state.worktree_root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
