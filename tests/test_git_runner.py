from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mergetrain.config import load_config
from mergetrain.errors import CommandFailed
from mergetrain.git_runner import GitRunner, run_shell
from mergetrain.store import (
    cancel_job,
    claim_all_queued,
    connect,
    enqueue_job,
    get_job,
    get_lock,
    list_run_events,
    release_runner_lock,
)


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed\n{completed.stdout}\n{completed.stderr}")
    return completed.stdout.strip()


def make_demo_repo(root: Path, *, gate_command: str = "") -> tuple[Path, Path]:
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
    gate_command = gate_command or (
        f"{sys.executable} -c \"from pathlib import Path; p=Path('{marker}'); "
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\""
    )
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
queue:
  lock_ttl_minutes: 1
  daemon_interval_seconds: 1
  heartbeat_interval_seconds: 1
  command_timeout_seconds: 30
gates:
  - name: marker
    run: {gate_command}
deploy:
  verify: []
"""
    (repo / ".mergetrain.yaml").write_text(config_text, encoding="utf-8")
    return repo, marker


class GitRunnerTests(unittest.TestCase):
    def test_managed_command_timeout_terminates_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            started = time.monotonic()
            with self.assertRaises(CommandFailed) as raised:
                run_shell(
                    f'{sys.executable} -c "import time; time.sleep(10)"',
                    cwd=td,
                    env=os.environ.copy(),
                    log=io.StringIO(),
                    timeout_seconds=0.2,
                    pulse_interval_seconds=0.1,
                )
            self.assertEqual(raised.exception.returncode, 124)
            self.assertLess(time.monotonic() - started, 3)

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
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual([result.status for result in results], ["validated"])
            self.assertEqual(stored.status, "validated")
            self.assertTrue(stored.train_id)
            self.assertEqual(stored.train_size, 1)
            self.assertTrue(stored.validated_at)
            self.assertTrue(stored.validation_base_sha)
            self.assertEqual(stored.validation_sha, stored.deploy_sha)
            self.assertEqual(stored.validated_head_sha, git(repo, "rev-parse", "feature/a"))
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            self.assertIn("Merged feature/a", [event.message for event in events])
            self.assertIn("Running gate 2/2: marker", [event.message for event in events])

    def test_validated_batch_deploys_after_integration_ref_moves(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(
                    conn,
                    task="a",
                    branch="feature/a",
                    base_sha=git(repo, "rev-parse", "origin/main"),
                    head_sha=git(repo, "rev-parse", "feature/a"),
                )
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                (repo / "base-moved.txt").write_text("moved\n", encoding="utf-8")
                git(repo, "add", "base-moved.txt")
                git(repo, "commit", "-m", "move integration")
                git(repo, "push", "origin", "main")
                deployed = GitRunner(config).process_batch(conn, [validated], deploy=True)[0]
            finally:
                conn.close()
            self.assertEqual(deployed.status, "deployed")
            self.assertNotEqual(deployed.validation_base_sha, deployed.deploy_sha)
            self.assertEqual(git(root / "remote.git", "show", "main:a.txt"), "a")
            self.assertEqual(git(root / "remote.git", "show", "main:base-moved.txt"), "moved")
            self.assertEqual(marker.read_text(encoding="utf-8"), "xx")

    def test_changed_branch_head_blocks_validated_train(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                git(repo, "switch", "feature/a")
                (repo / "changed.txt").write_text("changed\n", encoding="utf-8")
                git(repo, "add", "changed.txt")
                git(repo, "commit", "-m", "change after validation")
                git(repo, "switch", "main")
                result = GitRunner(config).process_batch(conn, [validated], deploy=True)[0]
            finally:
                conn.close()
            self.assertEqual(result.status, "blocked")
            self.assertIn("HEAD changed since validation", result.note)
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:a.txt")

    def test_batch_refreshes_lease_while_holding_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            try:
                enqueue_job(conn, task="a", branch="feature/a")
                claimed = claim_all_queued(conn, owner=owner, ttl_minutes=-1)
                before = get_lock(conn)
                results = GitRunner(config).process_batch(
                    conn, claimed, deploy=False, owner=owner, ttl_minutes=30
                )
                after = get_lock(conn)
            finally:
                conn.close()
            self.assertEqual([result.status for result in results], ["validated"])
            self.assertIsNotNone(before)
            self.assertIsNotNone(after)
            # Lease advanced from expired (past) to valid (~30 min ahead).
            self.assertGreater(after.expires_at, before.expires_at)

    def test_long_gate_heartbeats_and_cooperatively_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate = f'{sys.executable} -c "import time; time.sleep(10)"'
            repo, _marker = make_demo_repo(root, gate_command=gate)
            config = load_config(repo=repo)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            job = enqueue_job(conn, task="a", branch="feature/a")
            claimed = claim_all_queued(conn, owner=owner, ttl_minutes=1)
            token = claimed[0].claim_token
            conn.close()

            results: list = []
            errors: list[Exception] = []

            def run() -> None:
                worker_conn = connect(config.state.db)
                try:
                    results.extend(
                        GitRunner(config).process_batch(
                            worker_conn,
                            claimed,
                            deploy=False,
                            owner=owner,
                            ttl_minutes=1,
                        )
                    )
                except Exception as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
                finally:
                    worker_conn.close()

            worker = threading.Thread(target=run)
            worker.start()
            time.sleep(0.5)
            control = connect(config.state.db)
            control.execute(
                "UPDATE locks SET expires_at = '2000-01-01T00:00:00Z' WHERE token = ?",
                (token,),
            )
            control.commit()
            time.sleep(1.5)
            self.assertGreater(get_lock(control).expires_at, "2000-01-01T00:00:00Z")
            cancel_job(control, job.id)
            control.close()
            worker.join(timeout=6)

            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual([result.status for result in results], ["canceled"])
            cleanup = connect(config.state.db)
            try:
                release_runner_lock(cleanup, owner=owner, token=token)
            finally:
                cleanup.close()


if __name__ == "__main__":
    unittest.main()
