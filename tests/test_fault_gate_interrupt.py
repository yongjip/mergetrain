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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_runner import SHELL_PYTHON, make_demo_repo, py_path

from mergetrain.config import load_config
from mergetrain.store import connect, enqueue_job

SOURCE = Path(__file__).resolve().parents[1] / "src"
GATE_SECONDS = 60
EXIT_SECONDS = 15

_RUNNER = textwrap.dedent(
    """
    import sys
    from mergetrain.config import load_config
    from mergetrain.git_runner import GitRunner
    from mergetrain.store import claim_all_queued, connect, default_owner

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
                self.assertTrue(
                    _wait_until(lambda: all(path.exists() for path in pid_files), 30),
                    "both gates never started",
                )
                pids = [int(path.read_text(encoding="utf-8")) for path in pid_files]
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
