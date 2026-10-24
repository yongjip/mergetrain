"""Ctrl-C and MCP cancellation must stop configured gates promptly (#222).

Configured gates run in worker threads, and each gate leads its own process
group, so a SIGINT reaches only the runner's main thread. That thread has to
stop the workers' process groups before it waits for them; otherwise the gates
run on to completion in a worktree the runner is abandoning.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_runner import SHELL_PYTHON, make_demo_repo, py_path

from mergetrain.config import load_config
from mergetrain.persistence.connection import connect
from mergetrain.persistence.jobs import enqueue_job

SOURCE = Path(__file__).resolve().parents[1] / "src"
GATE_SECONDS = 60
EXIT_SECONDS = 15

_RUNNER = textwrap.dedent(
    """
    import sys
    from mergetrain.config import load_config
    from mergetrain.git_runner import GitRunner
    from mergetrain.persistence.claims import claim_all_queued
    from mergetrain.persistence.connection import connect
    from mergetrain.persistence.leases import default_owner

    config = load_config(repo=sys.argv[1])
    conn = connect(config.state.db)
    owner = default_owner()
    ttl = config.queue.lock_ttl_minutes
    claimed = claim_all_queued(conn, owner=owner, ttl_minutes=ttl)
    GitRunner(config).process_batch(conn, claimed, deploy=False, owner=owner, ttl_minutes=ttl)
    """
)


def _gate(pid_file: Path) -> str:
    return (
        f'{SHELL_PYTHON} -c "import os, pathlib, time; '
        f"pathlib.Path('{py_path(pid_file)}').write_text(str(os.getpid())); "
        f'time.sleep({GATE_SECONDS})"'
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until(condition, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def _wait_for_pids(pid_files: list[Path], seconds: float) -> list[int]:
    pids: list[int] = []

    def read_pids() -> bool:
        nonlocal pids
        try:
            values = [int(path.read_text(encoding="utf-8")) for path in pid_files]
        except (FileNotFoundError, ValueError):
            # write_text creates the file before its PID is available to read.
            return False
        if any(pid <= 0 for pid in values):
            return False
        pids = values
        return True

    _wait_until(read_pids, seconds)
    return pids


class GatePidReadinessTests(unittest.TestCase):
    def test_waits_for_both_pid_files_to_be_written(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pid_files = [Path(td) / "left.pid", Path(td) / "right.pid"]
            writes = iter([("", ""), ("123", ""), ("123", "456")])

            def publish_pids(_seconds: float) -> None:
                for path, value in zip(pid_files, next(writes), strict=True):
                    path.write_text(value, encoding="utf-8")

            with mock.patch.object(time, "sleep", side_effect=publish_pids) as sleep:
                self.assertEqual(_wait_for_pids(pid_files, 30), [123, 456])
            self.assertEqual(sleep.call_count, 3)


@unittest.skipUnless(os.name == "posix", "delivers SIGINT the way a terminal or MCP does")
class GateInterruptTests(unittest.TestCase):
    def test_sigint_stops_a_parallel_gate_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pid_files = [root / "left.pid", root / "right.pid"]
            repo, _ = make_demo_repo(root)
            config_path = repo / ".mergetrain.yaml"
            text = config_path.read_text(encoding="utf-8")
            head, rest = text.split("gates:\n", 1)
            _, tail = rest.split("deploy:\n", 1)
            gates = "".join(
                f"  - name: {name}\n    run: {_gate(pid_file)}\n    parallel_group: pair\n"
                for name, pid_file in zip(("left", "right"), pid_files, strict=True)
            )
            config_path.write_text(
                f"{head}gate_parallelism:\n  max_workers: 2\ngates:\n{gates}deploy:\n{tail}",
                encoding="utf-8",
            )
            conn = connect(load_config(repo=repo).state.db)
            try:
                enqueue_job(conn, task="a", branch="feature/a")
            finally:
                conn.close()

            env = {**os.environ, "PYTHONPATH": str(SOURCE)}
            runner = subprocess.Popen(
                [sys.executable, "-c", _RUNNER, str(repo)],
                env=env,
                stderr=subprocess.DEVNULL,
            )
            pids: list[int] = []
            try:
                pids = _wait_for_pids(pid_files, 30)
                self.assertEqual(len(pids), len(pid_files), "both gates never wrote their PIDs")
                self.assertTrue(all(_alive(pid) for pid in pids))

                runner.send_signal(signal.SIGINT)
                runner.wait(timeout=EXIT_SECONDS)
                self.assertTrue(
                    _wait_until(lambda: not any(_alive(pid) for pid in pids), 5),
                    "a gate outlived the interrupted runner",
                )
            finally:
                if runner.poll() is None:
                    runner.kill()
                    runner.wait()
                for pid in pids:
                    if _alive(pid):
                        os.killpg(os.getpgid(pid), signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
