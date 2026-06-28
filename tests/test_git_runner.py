from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from trainyard.config import load_config
from trainyard.git_runner import GitRunner
from trainyard.store import connect, enqueue_job, get_job


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed\n{completed.stdout}\n{completed.stderr}")
    return completed.stdout.strip()


class GitRunnerTests(unittest.TestCase):
    def test_batch_merges_jobs_and_runs_gate_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
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
    run: python -c \"from pathlib import Path; p=Path('{marker}'); p.write_text(p.read_text() + 'x' if p.exists() else 'x')\"
deploy:
  verify: []
"""
            config_path = repo / ".trainyard.yaml"
            config_path.write_text(config_text, encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
