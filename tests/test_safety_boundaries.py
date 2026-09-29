"""Regressions for the safety boundaries a 2026-09-30 review found crossable.

Each case lets a commit nobody approved reach the integration branch, or lets
cleanup remove something mergetrain does not own.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_runner import add_branch, git, make_demo_repo  # noqa: E402
from test_mcp_server import FakeContext, completed  # noqa: E402

from mergetrain import runtime  # noqa: E402
from mergetrain.cli import main  # noqa: E402
from mergetrain.command_runner import without_repository_env  # noqa: E402
from mergetrain.commands.deploy import _render_v3_preview  # noqa: E402
from mergetrain.config import load_config  # noqa: E402
from mergetrain.errors import escape_controls  # noqa: E402
from mergetrain.git_ops import apply_gc, branch_deletion_blocker  # noqa: E402
from mergetrain.mcp_server import MergetrainTools  # noqa: E402
from mergetrain.persistence.connection import connect  # noqa: E402
from mergetrain.persistence.jobs import enqueue_job, get_job  # noqa: E402


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


def _init_repo(root: Path, name: str) -> Path:
    repo = root / name
    git(root, "init", "-q", str(repo))
    git(repo, "config", "user.email", f"{name}@example.invalid")
    git(repo, "config", "user.name", name)
    (repo / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    git(repo, "add", f"{name}.txt")
    git(repo, "commit", "-m", name)
    return repo


def _repository_state(repo: Path, origin: Path) -> tuple[str, ...]:
    return (
        git(repo, "symbolic-ref", "HEAD"),
        git(repo, "for-each-ref"),
        git(repo, "worktree", "list", "--porcelain"),
        git(repo, "status", "--porcelain"),
        git(origin, "for-each-ref"),
    )


class InheritedGitEnvironmentTests(unittest.TestCase):
    """Git exports GIT_DIR and GIT_INDEX_FILE to hooks, and to aliases run from
    a linked worktree. mergetrain started there must still use its own repo."""

    def test_repository_variables_are_dropped_and_config_is_kept(self) -> None:
        env = without_repository_env(
            {
                "GIT_DIR": "/elsewhere/.git",
                "GIT_INDEX_FILE": "/elsewhere/.git/index",
                "GIT_WORK_TREE": "/elsewhere",
                "GIT_OBJECT_DIRECTORY": "/elsewhere/.git/objects",
                "GIT_COMMON_DIR": "/elsewhere/.git",
                "GIT_CONFIG_PARAMETERS": "'core.quotepath'='false'",
                "GIT_TERMINAL_PROMPT": "0",
                "PATH": "/usr/bin",
            }
        )
        self.assertEqual(
            env,
            {
                "GIT_CONFIG_PARAMETERS": "'core.quotepath'='false'",
                "GIT_TERMINAL_PROMPT": "0",
                "PATH": "/usr/bin",
            },
        )

    def test_validate_from_a_task_worktree_hook_leaves_that_worktree_alone(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _ready_feature_branch(root)
            git(repo, "switch", "main")
            add_branch(repo, "feature/b", "b.txt")
            task_a, task_b = root / "task-a", root / "task-b"
            git(repo, "worktree", "add", str(task_a), "feature/a")
            git(repo, "worktree", "add", str(task_b), "feature/b")
            for task, branch in (("a", "feature/a"), ("b", "feature/b")):
                code, out = _run(
                    [
                        "--repo", str(repo), "enqueue", "--task", task,
                        "--branch", branch, "--json",
                    ]
                )
                self.assertEqual(code, 0, out)
            feature_a = git(repo, "rev-parse", "feature/a")
            gitdir = git(task_a, "rev-parse", "--absolute-git-dir")

            hook_env = {"GIT_DIR": gitdir, "GIT_INDEX_FILE": str(Path(gitdir) / "index")}
            with patch.dict(os.environ, hook_env):
                code, out = _run(["--repo", str(repo), "validate", "--json"])

            self.assertEqual(code, 0, out)
            self.assertEqual(json.loads(out)["counts"], {"validated": 2})
            self.assertEqual(git(repo, "rev-parse", "feature/a"), feature_a)
            self.assertEqual(git(task_a, "status", "--porcelain"), "")

    def test_demo_with_git_dir_set_never_touches_that_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            origin = root / "origin.git"
            git(root, "init", "-q", "--bare", str(origin))
            user = _init_repo(root, "user")
            git(user, "branch", "-M", "feature")
            git(user, "remote", "add", "origin", str(origin))
            git(user, "push", "-q", "origin", "feature")
            before = _repository_state(user, origin)

            hook_env = {
                "GIT_DIR": str(user / ".git"),
                "GIT_INDEX_FILE": str(user / ".git" / "index"),
                "GIT_WORK_TREE": str(user),
                "MERGETRAIN_DEMO_STEP_DELAY": "0",
            }
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, hook_env), redirect_stdout(out), redirect_stderr(err):
                code = main(["demo", "--brief", "--dir", str(root / "demo")])

            self.assertEqual(code, 0, err.getvalue()[-2000:])
            self.assertEqual(_repository_state(user, origin), before)

    def test_runtime_provenance_reads_the_package_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ours, theirs = _init_repo(root, "ours"), _init_repo(root, "theirs")

            with patch.dict(os.environ, {"GIT_DIR": str(theirs / ".git")}):
                head = runtime._git_output(ours, "rev-parse", "HEAD")

            self.assertEqual(head, git(ours, "rev-parse", "HEAD"))


# Task text an enqueuing agent controls, crafted to rewrite what a human
# approves: a carriage return that overwrites the count line, ANSI and C1
# control sequences, a backspace, a bidirectional override, and a Unicode line
# separator.
FORGED_TASKS = (
    "x\rReady to deploy 1 job(s): Legit fix (#12)",
    "x\x1b[1A\x1b[2KReady to deploy 1 job(s): Legit fix",
    "evil\x9b2K\x08\x08",
    "‮gnp.txe",
    "a Ready to deploy 1 job(s): Legit fix",
)


def _two_job_plan(forged: str) -> dict[str, Any]:
    return {
        "ok": True,
        "result": "confirmation_required",
        "deploy_plan_sha": "f" * 64,
        "push_plan": {
            "remote": "origin",
            "url": "git@github.com:example/checkout.git",
            "refs": [{"source": "HEAD", "target": "main", "spec": "HEAD:main"}],
        },
        "reuse": {"decision": {"action": "rerun"}},
        "jobs": [
            {"id": 11, "task": "Legit fix", "branch": "agent/legit"},
            {"id": 12, "task": forged, "branch": "agent/evil"},
        ],
    }


def _hidden_characters(text: str) -> list[str]:
    """Characters that move the cursor, reorder, or hide text, bar line ends."""

    return [
        char
        for char in text
        if char != "\n" and unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}
    ]


class ConfirmationTextTests(unittest.TestCase):
    """The plan a human approves must show every job, whatever a task says."""

    def test_the_terminal_confirmation_lists_each_job_with_controls_escaped(self) -> None:
        for forged in FORGED_TASKS:
            with self.subTest(forged=forged):
                out = io.StringIO()
                with redirect_stdout(out):
                    _render_v3_preview(_two_job_plan(forged))
                shown = out.getvalue()
                lines = shown.splitlines()
                self.assertEqual(_hidden_characters(shown), [])
                self.assertEqual(lines[0], "Ready to deploy 2 job(s):")
                self.assertEqual(lines[1], "  #11 Legit fix (agent/legit)")
                self.assertTrue(lines[2].startswith("  #12 "), lines[2])
                self.assertTrue(lines[2].endswith(" (agent/evil)"), lines[2])
                self.assertTrue(lines[3].startswith("Destination: "), lines[3])

    def test_the_mcp_confirmation_counts_the_jobs_and_escapes_controls(self) -> None:
        tools = MergetrainTools(repo=Path("/repo"))
        for forged in FORGED_TASKS:
            with self.subTest(forged=forged):
                with patch.object(
                    MergetrainTools,
                    "_run",
                    return_value=completed(json.dumps(_two_job_plan(forged))),
                ):
                    plan = asyncio.run(tools.prepare_deploy(FakeContext()))
                lines = plan.summary.splitlines()
                self.assertEqual(_hidden_characters(plan.summary), [])
                self.assertEqual(lines[0], "Changes (2 jobs):")
                self.assertEqual(lines[1], "  #11 Legit fix (agent/legit)")
                self.assertTrue(lines[2].startswith("  #12 "), lines[2])
                self.assertTrue(lines[3].startswith("Destination: "), lines[3])

    def test_escapes_read_as_python_escapes_and_keep_ordinary_text(self) -> None:
        self.assertEqual(escape_controls("x\ry\x1b\x9b"), "x\\x0dy\\x1b\\x9b")
        self.assertEqual(escape_controls("‮ab "), "\\u202eab\\u2028")
        # A tag character beyond the BMP, and a lone surrogate that could not
        # be encoded for output at all.
        self.assertEqual(escape_controls("t\U000e0041\ud800"), "t\\U000e0041\\ud800")
        self.assertEqual(escape_controls("Fix café, #12 (a/b)"), "Fix café, #12 (a/b)")

    def test_enqueue_stores_the_task_label_on_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "queue.sqlite")
            try:
                job = enqueue_job(conn, task="  fix\r\nthe\tbug now  ", branch="agent/x")
            finally:
                conn.close()
            self.assertEqual(job.task, "fix the bug now")


class GcOwnershipTests(unittest.TestCase):
    """gc may remove only what this repository's queue made and no one uses."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo, _marker = make_demo_repo(self.root)
        self.config = load_config(repo=self.repo)
        # Named the way gc recognizes a temporary mergetrain worktree.
        self.worktrees = self.config.state.worktree_root

    def test_gc_keeps_a_worktree_locked_with_git(self) -> None:
        locked = self.worktrees / "demo-mergetrain-7-deadbeef"
        git(self.repo, "worktree", "add", "--detach", str(locked), "main")
        git(self.repo, "worktree", "lock", "--reason", "investigating", str(locked))
        (locked / "notes.txt").write_text("kept for investigation\n", encoding="utf-8")
        leftover = self.worktrees / "demo-mergetrain-8-cafebabe"
        leftover.mkdir()
        (leftover / "junk.txt").write_text("x\n", encoding="utf-8")

        result = apply_gc(self.config)

        self.assertTrue((locked / "notes.txt").is_file())
        self.assertFalse(leftover.exists())
        self.assertEqual(
            [item["path"] for item in result["removed_worktrees"]], [str(leftover)]
        )

    def test_gc_keeps_another_repositorys_worktree_in_a_shared_root(self) -> None:
        other = _init_repo(self.root, "other")
        foreign = self.worktrees / "demo-mergetrain-3-0badc0de"
        git(other, "worktree", "add", "--detach", str(foreign), "HEAD")

        result = apply_gc(self.config)

        self.assertTrue((foreign / "other.txt").is_file())
        self.assertEqual(result["removed_worktrees"], [])
        self.assertNotIn("prunable", git(other, "worktree", "list", "--porcelain"))

    def test_gc_keeps_a_branch_that_a_worktree_is_rebasing(self) -> None:
        git(self.repo, "switch", "-c", "clash", "main")
        (self.repo / "app.txt").write_text("clash\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "clash")
        git(self.repo, "switch", "feature/a")
        (self.repo / "app.txt").write_text("feature\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "feature")
        git(self.repo, "switch", "main")
        head = git(self.repo, "rev-parse", "feature/a")
        task = self.root / "task-a"
        git(self.repo, "worktree", "add", str(task), "feature/a")
        rebase = subprocess.run(["git", "rebase", "clash"], cwd=task, capture_output=True)
        self.assertNotEqual(rebase.returncode, 0, "the rebase should stop on its conflict")

        self.assertIn("rebased", branch_deletion_blocker(self.config, "feature/a", head))
        result = apply_gc(self.config, delete_branches={"feature/a": head})

        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/feature/a"), head)
        self.assertEqual(result["deleted_branches"], [])
        self.assertIn("rebased", result["failed"][0]["reason"])

    def test_gc_keeps_a_branch_whose_worktree_is_temporarily_missing(self) -> None:
        head = git(self.repo, "rev-parse", "feature/a")
        task = self.root / "task-a"
        git(self.repo, "worktree", "add", str(task), "feature/a")
        task.rename(self.root / "task-a-unplugged")

        self.assertIn("checked out", branch_deletion_blocker(self.config, "feature/a", head))
        result = apply_gc(self.config, delete_branches={"feature/a": head})

        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/feature/a"), head)
        self.assertEqual(result["deleted_branches"], [])
        self.assertIn("checked out", result["failed"][0]["reason"])


class PushScopeTests(unittest.TestCase):
    """The deploy pushes the approved refs and its audit ref, and nothing else."""

    def test_operator_push_config_adds_no_ref_to_the_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = _ready_feature_branch(root)
            git(repo, "config", "push.followTags", "true")
            git(repo, "config", "push.recurseSubmodules", "on-demand")
            git(repo, "tag", "-a", "wip-local-experiment", "-m", "local only", "feature/a")

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

            conn = connect(load_config(repo=repo).state.db)
            try:
                self.assertEqual(get_job(conn, job_id).status, "deployed")
            finally:
                conn.close()
            remote_refs = git(root / "remote.git", "for-each-ref", "--format=%(refname)")
            self.assertEqual(
                sorted(ref for ref in remote_refs.splitlines() if not ref.startswith("refs/heads/")),
                [line for line in remote_refs.splitlines() if line.startswith("refs/mergetrain/")],
            )
            pushes = [
                line
                for log in sorted((root / "logs").rglob("*.log"))
                for line in log.read_text(encoding="utf-8").splitlines()
                if " push --atomic" in line
            ]
            self.assertTrue(pushes)
            for line in pushes:
                self.assertIn("--no-follow-tags", line)
                self.assertIn("--recurse-submodules=no", line)


if __name__ == "__main__":
    unittest.main()
