"""Regressions for the safety boundaries a 2026-09-30 review found crossable.

Each case lets a commit nobody approved reach the integration branch, or lets
cleanup remove something mergetrain does not own.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_runner import git, make_demo_repo  # noqa: E402

from mergetrain.cli import main  # noqa: E402
from mergetrain.config import load_config  # noqa: E402
from mergetrain.persistence.connection import connect  # noqa: E402
from mergetrain.persistence.jobs import get_job  # noqa: E402


def _run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


def _ready_feature_branch(root: Path) -> Path:
    """A committed config on main and ``feature/a`` rebased onto it."""

    repo, _marker = make_demo_repo(root)
    git(repo, "add", ".mergetrain.yaml")
    git(repo, "commit", "-m", "configure mergetrain")
    git(repo, "push", "origin", "main")
    git(repo, "switch", "feature/a")
    git(repo, "rebase", "main")
    return repo


def _is_ancestor(repo: Path, commit: str, descendant: str) -> bool:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, descendant],
        cwd=repo,
        capture_output=True,
    )
    return completed.returncode == 0


class AmbiguousRefTests(unittest.TestCase):
    def test_a_tag_named_like_the_integration_ref_never_becomes_the_train_base(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _ready_feature_branch(root)
            remote = root / "remote.git"

            # Someone who may push branches and tags, but not main, publishes
            # a commit nobody enqueued and a tag literally named origin/main.
            other = root / "other"
            git(root, "clone", str(remote), str(other))
            git(other, "config", "user.email", "o@example.invalid")
            git(other, "config", "user.name", "Other")
            git(other, "switch", "-c", "side", "origin/main")
            (other / "unreviewed.txt").write_text("never enqueued\n", encoding="utf-8")
            git(other, "add", "unreviewed.txt")
            git(other, "commit", "-m", "unreviewed commit")
            unreviewed = git(other, "rev-parse", "HEAD")
            git(other, "push", "origin", "side")
            git(other, "tag", "origin/main", unreviewed)
            git(other, "push", "origin", "refs/tags/origin/main")

            code, out = _run(
                [
                    "--repo", str(repo), "enqueue", "--task", "task a",
                    "--branch", "feature/a", "--auto", "--json",
                ]
            )
            self.assertEqual(code, 0, out)
            job_id = int(json.loads(out)["job"]["id"])
            code, out = _run(["--repo", str(repo), "daemon", "--once"])
            self.assertEqual(code, 0, out)

            deployed = git(remote, "rev-parse", "main")
            self.assertFalse(
                _is_ancestor(remote, unreviewed, deployed),
                "a commit nobody enqueued reached main through the tag",
            )
            self.assertTrue(_is_ancestor(remote, git(repo, "rev-parse", "feature/a"), deployed))
            conn = connect(load_config(repo=repo).state.db)
            try:
                self.assertEqual(get_job(conn, job_id).status, "deployed")
            finally:
                conn.close()

    def test_enqueue_records_the_branch_even_when_a_tag_has_its_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _ready_feature_branch(root)
            branch_head = git(repo, "rev-parse", "refs/heads/feature/a")
            git(repo, "tag", "feature/a", "main")

            code, out = _run(
                [
                    "--repo", str(repo), "enqueue", "--task", "task a",
                    "--branch", "feature/a", "--json",
                ]
            )

            self.assertEqual(code, 0, out)
            self.assertEqual(json.loads(out)["job"]["head_sha"], branch_head)


if __name__ == "__main__":
    unittest.main()
