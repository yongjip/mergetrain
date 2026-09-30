"""A runner killed mid-push must not let reconcile outrun its orphaned push (#220).

Managed commands start in their own session (their own Job Object on Windows),
so killing the runner leaves the ``git push`` it started running. Here the push
waits in a slow ``pre-receive`` hook, the runner process alone is killed, and
reconcile runs while the orphan is still going. It must refuse, rather than
record "did not land" for a push that then lands; once the push has exited, it
must record ``deployed``. POSIX finds the orphan by its push lock and Windows by
its named Job Object, so the killed-runner scenarios run on both.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_runner import git, make_demo_repo

from mergetrain.config import load_config
from mergetrain.errors import CommandFailed, LockHeld
from mergetrain.persistence.connection import connect
from mergetrain.persistence.jobs import cancel_job, enqueue_job, get_job
from mergetrain.push_liveness import push_in_flight
from mergetrain.recovery import reconcile

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


class OrphanedPushTests(unittest.TestCase):
    def _run(self, *, kill_signal: int | None, cancel: bool) -> None:
        """Stop the runner mid-push with ``kill_signal``, or ``Popen.kill`` when None."""

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            remote = root / "remote.git"
            sentinel = root / "hook-started"
            hook = remote / "hooks" / "pre-receive"
            # LF endings: Git for Windows runs hooks with its own sh.
            hook.write_text(
                f"#!/bin/sh\ntouch '{sentinel.as_posix()}'\nsleep {HOOK_SECONDS}\n",
                encoding="utf-8",
                newline="\n",
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
                if kill_signal is None:
                    runner.kill()  # SIGKILL on POSIX, TerminateProcess on Windows
                    runner.wait(timeout=WAIT_SECONDS)
                else:
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
                    reconcile(config, conn, apply=True)
                parked = get_job(conn, job_id)
                self.assertEqual(parked.status, "needs_reconcile")
                self.assertEqual(parked.pending_deploy_sha, pending_sha)

                _wait_for(
                    lambda: not push_in_flight(config, pending_sha),
                    what="the orphaned push to exit",
                )
                self.assertEqual(git(remote, "rev-parse", "main"), pending_sha)

                outcome = reconcile(config, conn, apply=True)
                healed = get_job(conn, job_id)
            finally:
                conn.close()
            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(healed.status, "deployed")
            self.assertEqual(healed.deploy_sha, pending_sha)
            reason = outcome.jobs[0]["reason"]
            self.assertIn("push landed", reason)
            if cancel:
                self.assertIn("late cancel ignored", reason)

    def test_a_killed_runner_waits_for_the_orphan_then_records_deployed(self) -> None:
        self._run(kill_signal=None, cancel=False)

    @unittest.skipUnless(os.name == "posix", "delivers a POSIX SIGTERM")
    def test_sigterm_mid_push_waits_for_the_orphan_then_records_deployed(self) -> None:
        self._run(kill_signal=signal.SIGTERM, cancel=False)

    def test_a_late_cancel_cannot_turn_a_landing_push_into_canceled(self) -> None:
        self._run(kill_signal=None, cancel=True)


def _process_command(pid: int) -> str:
    completed = subprocess.run(
        ["ps", "-ww", "-o", "command=", "-p", str(pid)],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip()


@unittest.skipUnless(os.name == "posix", "the POSIX push lock is an inherited flock")
class PushLockLifetimeTests(unittest.TestCase):
    def test_a_receive_pack_that_outlives_a_killed_git_push_keeps_the_push_in_flight(
        self,
    ) -> None:
        # A local receive-pack that already has the whole push still applies
        # it after git push dies, so a push killed on its own must stay "in
        # flight" until every process that inherited the lock has exited.
        from mergetrain.atomic_push import AtomicPush
        from mergetrain.git_destination import resolve_git_destination

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            remote = root / "remote.git"
            # Keepalives would make receive-pack write to the dead client and
            # die, so without them it deterministically outlives git push.
            git(remote, "config", "receive.keepAlive", "0")
            sentinel = root / "hook.pid"
            hook = remote / "hooks" / "pre-receive"
            hook.write_text(
                f"#!/bin/sh\necho $$ > '{sentinel}.tmp'\n"
                f"mv '{sentinel}.tmp' '{sentinel}'\nsleep {HOOK_SECONDS}\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            config = load_config(repo=repo)
            base = git(remote, "rev-parse", "main")
            target = git(repo, "rev-parse", "feature/a")
            push = AtomicPush(config)
            destination = resolve_git_destination(config)
            audit_ref, expected = push.audit_ref_expectation(
                deploy_sha=target, log=None, destination=destination
            )
            failures: list[BaseException] = []

            def run_push() -> None:
                try:
                    push.push_verified_head(
                        worktree=repo,
                        deploy_sha=target,
                        audit_ref=audit_ref,
                        audit_expected_sha=expected,
                        destination=destination,
                    )
                except BaseException as exc:  # noqa: BLE001 - asserted below
                    failures.append(exc)

            pusher = threading.Thread(target=run_push, daemon=True)
            pusher.start()
            _wait_for(sentinel.exists, what="the pre-receive hook to start")
            # The push runs in its own session, led by git push itself.
            client = os.getpgid(int(sentinel.read_text(encoding="utf-8")))
            self.assertNotEqual(client, os.getpgid(0))
            self.assertIn("push --atomic", _process_command(client))
            os.kill(client, signal.SIGKILL)
            pusher.join(timeout=WAIT_SECONDS)
            self.assertFalse(pusher.is_alive(), "the push never returned")
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], CommandFailed)

            # receive-pack is still in its hook: nothing has landed yet, and
            # nothing may decide that nothing will.
            self.assertEqual(git(remote, "rev-parse", "main"), base)
            self.assertTrue(push_in_flight(config, target))

            _wait_for(
                lambda: not push_in_flight(config, target),
                what="receive-pack to exit",
            )
            self.assertEqual(git(remote, "rev-parse", "main"), target)

    def test_a_helper_the_push_leaves_running_does_not_hold_the_push_lock(self) -> None:
        # git starts helpers that outlive the push, such as a credential cache
        # daemon or anything a hook backgrounds. Once git push has exited,
        # they must not keep the push "in flight" and block reconcile.
        from mergetrain.atomic_push import AtomicPush
        from mergetrain.git_destination import resolve_git_destination

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            pid_file = root / "helper.pid"
            hook = repo / ".git" / "hooks" / "pre-push"
            hook.write_text(
                "#!/bin/sh\n"
                "sleep 60 >/dev/null 2>&1 </dev/null &\n"
                f"echo $! > '{pid_file}'\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            config = load_config(repo=repo)
            target = git(repo, "rev-parse", "feature/a")
            push = AtomicPush(config)
            destination = resolve_git_destination(config)
            audit_ref, expected = push.audit_ref_expectation(
                deploy_sha=target, log=None, destination=destination
            )
            push.push_verified_head(
                worktree=repo,
                deploy_sha=target,
                audit_ref=audit_ref,
                audit_expected_sha=expected,
                destination=destination,
            )
            helper = int(pid_file.read_text(encoding="utf-8"))
            try:
                os.kill(helper, 0)  # the helper outlived the push
                self.assertFalse(push_in_flight(config, target))
            finally:
                os.kill(helper, signal.SIGKILL)
            self.assertEqual(git(root / "remote.git", "rev-parse", "main"), target)


if __name__ == "__main__":
    unittest.main()
