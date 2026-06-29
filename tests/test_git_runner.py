from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mergetrain.config import load_config
from mergetrain.git_runner import GitRunner
from mergetrain.store import acquire_runner_lock, connect, enqueue_job, get_job, get_lock


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed\n{completed.stdout}\n{completed.stderr}")
    return completed.stdout.strip()


def make_demo_repo(root: Path) -> tuple[Path, Path]:
    """Create a remote+clone with a ``feature/a`` branch and return (repo, marker).

    The gate appends to ``marker`` once per gate run so tests can assert the train
    gate executed exactly once over the assembled batch.
    """
    repo = root / "repo"
    remote = root / "remote.git"
    git(root, "init", "--bare", str(remote))
    git(root, "clone", str(remote), str(repo))
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test User")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "app.txt")
    git(repo, "commit", "-m", "base")
    git(repo, "branch", "-M", "main")
    git(repo, "push", "-u", "origin", "main")
    git(repo, "switch", "-c", "feature/a")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-m", "a")
    git(repo, "switch", "main")
    marker = root / "gate-count.txt"
    config_text = f"""project:
  name: demo
state:
  db: {root / 'queue.sqlite'}
  logs: {root / 'logs'}
  worktree_root: {root / 'worktrees'}
git:
  remote: origin
  integration_branch: main
  push_refs:
    - main
gates:
  - name: marker
    run: {sys.executable} -c \"from pathlib import Path; p=Path('{marker}'); p.write_text(p.read_text() + 'x' if p.exists() else 'x')\"
deploy:
  verify: []
"""
    (repo / ".mergetrain.yaml").write_text(config_text, encoding="utf-8")
    return repo, marker


class GitRunnerTests(unittest.TestCase):
    def test_batch_merges_jobs_and_runs_gate_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                results = GitRunner(config).process_batch(conn, [job], deploy=False)
                stored = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual([result.status for result in results], ["validated"])
            self.assertEqual(stored.status, "validated")
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")

    def test_batch_refreshes_lease_while_holding_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                # Hold the lock with an already-expired lease (as a long job's lease
                # would lapse). process_batch must refresh it so a concurrent runner
                # cannot reclaim the lock out from under an active deploy.
                acquire_runner_lock(conn, owner=owner, ttl_minutes=-1)
                before = get_lock(conn)
                results = GitRunner(config).process_batch(
                    conn, [job], deploy=False, owner=owner, ttl_minutes=30
                )
                after = get_lock(conn)
            finally:
                conn.close()
            self.assertEqual([result.status for result in results], ["validated"])
            self.assertIsNotNone(before)
            self.assertIsNotNone(after)
            # Lease advanced from expired (past) to valid (~30 min ahead).
            self.assertGreater(after.expires_at, before.expires_at)


if __name__ == "__main__":
    unittest.main()
