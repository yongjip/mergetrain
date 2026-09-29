"""A runner killed mid-push must not let reconcile outrun its orphaned push (#220).

Managed commands start in their own session, so killing the runner leaves the
``git push`` it started running. Here the push waits in a slow ``pre-receive``
hook, the runner process alone is killed, and reconcile runs while the orphan
is still going. It must refuse, rather than record "did not land" for a push
that then lands; once the push has exited, it must record ``deployed``.
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

from test_git_runner import git, make_demo_repo

from mergetrain.config import load_config
from mergetrain.errors import LockHeld
from mergetrain.persistence.connection import connect
from mergetrain.persistence.jobs import cancel_job, enqueue_job, get_job
from mergetrain.push_liveness import push_in_flight
from mergetrain.recovery import recover

SOURCE = Path(__file__).resolve().parents[1] / "src"
HOOK_SECONDS = 8
WAIT_SECONDS = 30

# The runner claims under its own pid, so the lock it leaves reads as dead.
_RUNNER = textwrap.dedent(
    """
    import sys
    from mergetrain.config import load_config
    from mergetrain.git_runner import GitRunner
    from mergetrain.persistence.claims import claim_deploy_batch
    from mergetrain.persistence.connection import connect
    from mergetrain.persistence.leases import default_owner

    config = load_config(repo=sys.argv[1])
    conn = connect(config.state.db)
    owner = default_owner()
    ttl = config.queue.lock_ttl_minutes
    claimed = claim_deploy_batch(conn, owner=owner, ttl_minutes=ttl)
    GitRunner(config).process_batch(conn, claimed, deploy=True, owner=owner, ttl_minutes=ttl)
    """
)


def _wait_for(condition, *, what: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@unittest.skipUnless(os.name == "posix", "kills a runner process with POSIX signals")
class OrphanedPushTests(unittest.TestCase):
    def _run(self, *, kill_signal: int, cancel: bool) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            remote = root / "remote.git"
            sentinel = root / "hook-started"
            hook = remote / "hooks" / "pre-receive"
            hook.write_text(
                f"#!/bin/sh\ntouch '{sentinel}'\nsleep {HOOK_SECONDS}\n", encoding="utf-8"
            )
            hook.chmod(0o755)
            config = load_config(repo=repo)
            base = git(remote, "rev-parse", "main")
            conn = connect(config.state.db)
            try:
                job_id = enqueue_job(conn, task="a", branch="feature/a").id
            finally:
                conn.close()

            env = {**os.environ, "PYTHONPATH": str(SOURCE)}
            runner = subprocess.Popen([sys.executable, "-c", _RUNNER, str(repo)], env=env)
            try:
                _wait_for(
                    lambda: sentinel.exists() or runner.poll() is not None,
                    what="the push to reach the pre-receive hook",
                )
                self.assertIsNone(runner.poll(), "the runner exited before it pushed")
                runner.send_signal(kill_signal)
                self.assertEqual(runner.wait(timeout=WAIT_SECONDS), -kill_signal)
            finally:
                if runner.poll() is None:
                    runner.kill()
                    runner.wait()

            conn = connect(config.state.db)
            try:
                if cancel:
                    cancel_job(conn, job_id)
                pending_sha = get_job(conn, job_id).pending_deploy_sha
                self.assertTrue(pending_sha)
                # The orphaned push is still in the hook: nothing has landed yet,
                # and reconcile must not decide that nothing will.
                self.assertEqual(git(remote, "rev-parse", "main"), base)
                self.assertTrue(push_in_flight(config, pending_sha))
                with self.assertRaisesRegex(LockHeld, "still running and may yet land"):
                    recover(config, conn, gc=False)
                parked = get_job(conn, job_id)
                self.assertEqual(parked.status, "needs_reconcile")
                self.assertEqual(parked.pending_deploy_sha, pending_sha)

                _wait_for(
                    lambda: not push_in_flight(config, pending_sha),
                    what="the orphaned push to exit",
                )
                self.assertEqual(git(remote, "rev-parse", "main"), pending_sha)

                outcome = recover(config, conn, gc=False)
                healed = get_job(conn, job_id)
            finally:
                conn.close()
            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(healed.status, "deployed")
            self.assertEqual(healed.deploy_sha, pending_sha)
            reason = outcome.reconcile.jobs[0]["reason"]
            self.assertIn("push landed", reason)
            if cancel:
                self.assertIn("late cancel ignored", reason)

    def test_sigkill_mid_push_waits_for_the_orphan_then_records_deployed(self) -> None:
        self._run(kill_signal=signal.SIGKILL, cancel=False)

    def test_sigterm_mid_push_waits_for_the_orphan_then_records_deployed(self) -> None:
        self._run(kill_signal=signal.SIGTERM, cancel=False)

    def test_a_late_cancel_cannot_turn_a_landing_push_into_canceled(self) -> None:
        self._run(kill_signal=signal.SIGKILL, cancel=True)


if __name__ == "__main__":
    unittest.main()
