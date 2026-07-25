"""Fault matrix cases 1-2: a REAL ``git push --atomic`` killed mid-flight.

Every other fault test in this repo reaches the ambiguous-push state by patching
``GitRunner.push_verified_head`` and raising a hand-written ``CommandFailed``.
That never exercises the three things the 1.0 gate actually depends on:

* git's real exit status when the push client dies (a signal, not exit 1),
* git's real stderr at the moment of death, and
* ``is_push_rejection`` running on that real stderr.

If ``run_command`` ever mapped a signal death to a different return code, or the
rejection classifier grew a pattern that matched ordinary push chatter, a push
that HAD landed on the remote would be recorded terminal ``failed`` — the exact
lie ("never lie about deployed/failed") the gate forbids — and the whole existing
suite would still be green, because no other test lets git speak for itself.

The two cases are one decision table over which receive hook hangs:

    hook          refs applied when it runs?   recover() must decide
    post-receive  yes  (remote advanced)       deployed, push_status succeeded
    pre-receive   no   (remote untouched)      queued, marker + pin cleared

The hook announces its pid and then sleeps, which pins the push at a known point
in the protocol; the test resolves that pid's process group (``git_runner`` starts
every command with ``start_new_session=True`` on POSIX, so the push client leads
its own group) and SIGKILLs the group. A second, non-blocking ``post-receive``
line counts every push the remote applies refs for, which is how "landed exactly
once" is measured from the remote's side rather than inferred from shas. POSIX
only: the whole point is a real signal death, and the group isolation is what
makes that safe.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

# `unittest discover -s tests` puts this dir on sys.path; add it explicitly so a
# single-module run (python -m unittest tests.test_fault_push_kill) also resolves
# the shared bare-remote fixture in the sibling module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_runner import git, make_demo_repo

from mergetrain.config import load_config
from mergetrain.git_runner import GitRunner, is_push_rejection, pending_ref_name
from mergetrain.recovery import recover
from mergetrain.store import claim_deploy_batch, connect, enqueue_job, get_job

# A pid that is never live, so the lock left by the killed "runner" reads as DEAD
# and a later recover()/claim can reap it (the test process is alive, so it
# cannot itself be the owner).
DEAD_OWNER = "ghost:999999"

# The hook must still be hanging when the SIGKILL lands, so it sleeps well past
# the push timeout; the killpg takes the hook down with the client. The lowered
# command timeout only bounds the damage if the kill never lands at all — a run
# that falls back to the timeout is reported as a failure, not silently passed.
HOOK_SLEEP_SECONDS = 15
COMMAND_TIMEOUT_SECONDS = 10

# The killer must give up BEFORE the push's own timeout can fire, so that a run
# where the hook never announced has its reason recorded by the time the test
# reads it. If this outlived the push timeout, the deploy would return first and
# the assertions would read a still-empty result dict ("None is not true")
# instead of the real cause, while the thread wrote to it concurrently.
KILL_DEADLINE_SECONDS = COMMAND_TIMEOUT_SECONDS - 2

_SHEBANG = "#!/bin/sh\n"

# Appended to the remote's post-receive hook: one line per push whose ref updates
# the remote actually applied. This is the independent "exactly once" measure —
# a bare repo keeps no reflog, so without it the test could only compare shas,
# which cannot tell one applied push from two.
_COUNT_APPLIED = "echo applied >> '{counter}'\n"

# Publish the hook's pid, then hang. The pid belongs to the process group led by
# the `git push` client, which is what the test kills. Written via a rename so the
# test can never read a half-written pid.
_ANNOUNCE_AND_HANG = """echo $$ > '{sentinel}.tmp'
mv '{sentinel}.tmp' '{sentinel}'
sleep {sleep}
"""


@dataclass(frozen=True)
class _Case:
    hook: str  # which receive hook hangs -> whether the refs are applied first
    remote_applied: bool  # did the remote apply the refs before the client died?
    verdict: str  # the status recover() must reach from remote truth alone


_DECISION_TABLE = (
    # post-receive runs AFTER the ref update is committed: the deploy landed but
    # was never acknowledged, so the only truthful verdict is `deployed`.
    _Case(hook="post-receive", remote_applied=True, verdict="deployed"),
    # pre-receive runs BEFORE any ref moves: nothing landed, so the work must go
    # back to the queue and ship on a later run.
    _Case(hook="pre-receive", remote_applied=False, verdict="queued"),
)


def _pending_refs(repo: Path) -> str:
    return git(repo, "for-each-ref", "--format=%(refname)", "refs/mergetrain/pending/")


def _applied_pushes(counter: Path) -> int:
    """How many pushes the remote has applied refs for, from its own hook."""
    if not counter.exists():
        return 0
    return len([line for line in counter.read_text(encoding="utf-8").splitlines() if line.strip()])


def _process_command(pid: int) -> str:
    completed = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip()


@unittest.skipUnless(
    os.name == "posix", "needs POSIX process groups to kill a push client safely"
)
class KilledAtomicPushTests(unittest.TestCase):
    """The real push, really killed, then reconciled against the real remote."""

    def _prepare(self, root: Path, case: _Case) -> tuple[Path, Path, Path, Path, Path]:
        """A repo + bare remote whose receive hooks count applied pushes and hang.

        ``post-receive`` always counts (it only runs once the refs are committed);
        the case's hook is the one that also announces its pid and then blocks, so
        the kill lands either after or before the ref update.
        """
        repo, _ = make_demo_repo(root)
        remote = root / "remote.git"
        config_path = repo / ".mergetrain.yaml"
        text = config_path.read_text(encoding="utf-8")
        lowered = text.replace(
            "command_timeout_seconds: 30",
            f"command_timeout_seconds: {COMMAND_TIMEOUT_SECONDS}",
        )
        # Fail loudly if the shared fixture's timeout line changes shape: silently
        # keeping a 30s push timeout would blow this file's runtime budget.
        self.assertNotEqual(lowered, text, "fixture no longer sets command_timeout_seconds: 30")
        config_path.write_text(lowered, encoding="utf-8")
        sentinel = root / "hook-pid"
        counter = root / "applied-pushes"
        hang = _ANNOUNCE_AND_HANG.format(sentinel=sentinel, sleep=HOOK_SLEEP_SECONDS)
        scripts = {"post-receive": _SHEBANG + _COUNT_APPLIED.format(counter=counter)}
        if case.hook == "post-receive":
            scripts["post-receive"] += hang
        else:
            scripts[case.hook] = _SHEBANG + hang
        for name, body in scripts.items():
            path = remote / "hooks" / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        return repo, remote, sentinel, counter, remote / "hooks" / case.hook

    def _kill_push_group(self, sentinel: Path, result: dict) -> None:
        """Wait for the hanging receive hook, then SIGKILL the push's group.

        Runs on a side thread while the deploy runs on the test's own thread
        (sqlite connections are ``check_same_thread``, so the deploy has to stay
        where its connection was opened). Every refusal path records a reason
        instead of signalling anything: the test asserts on ``result``, so a kill
        that did not happen fails loudly rather than passing via the timeout.
        """
        deadline = time.monotonic() + KILL_DEADLINE_SECONDS
        pid = 0
        while time.monotonic() < deadline:
            try:
                pid = int(sentinel.read_text(encoding="utf-8").strip())
                break
            except (OSError, ValueError):
                time.sleep(0.05)
        if pid <= 0:
            result["error"] = "the receive hook never announced its pid"
            return
        try:
            pgid = os.getpgid(pid)
        except OSError as exc:  # the hook exited before we could read its group
            result["error"] = f"hook pid {pid} vanished: {exc}"
            return
        result["pgid"] = pgid
        result["command"] = _process_command(pgid)
        # Safety belt: git_runner launches with start_new_session=True, so the
        # push leads a group that is never ours. If that ever stops being true,
        # refuse — a SIGKILL to our own group would take the test runner out.
        if pgid <= 0 or pgid == os.getpgid(0):
            result["error"] = f"refusing to signal the test runner's own group ({pgid})"
            return
        if "push" not in result["command"]:
            result["error"] = f"group leader {pgid} is not a git push: {result['command']!r}"
            return
        os.killpg(pgid, signal.SIGKILL)
        result["killed"] = True

    def _deploy(self, config, conn, job_id: int):
        ttl = config.queue.lock_ttl_minutes
        claimed = claim_deploy_batch(conn, owner=DEAD_OWNER, ttl_minutes=ttl)
        self.assertEqual([job.id for job in claimed], [job_id])
        GitRunner(config).process_batch(
            conn, claimed, deploy=True, owner=DEAD_OWNER, ttl_minutes=ttl
        )
        return get_job(conn, job_id)

    def _deploy_and_kill(self, config, conn, job_id: int, sentinel: Path) -> tuple:
        result: dict = {}
        killer = threading.Thread(
            target=self._kill_push_group, args=(sentinel, result), daemon=True
        )
        killer.start()
        try:
            job = self._deploy(config, conn, job_id)
        finally:
            # Wait out the killer's own deadline so its recorded reason is always
            # in `result` by the time the assertions read it. Instant on the happy
            # path (the killer returns the moment it signals); only a run where
            # the deploy failed before the hook ever ran pays the wait, and that
            # run needs the reason more than it needs the second.
            killer.join(timeout=KILL_DEADLINE_SECONDS + 1)
            # Never leave a pid behind for a later poll to resolve: pids are
            # recycled, and a stale sentinel could aim a SIGKILL at anything.
            sentinel.unlink(missing_ok=True)
        return job, result

    def _run_case(self, case: _Case) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, remote, sentinel, counter, hook_path = self._prepare(root, case)
            config = load_config(repo=repo)
            base_remote_sha = git(remote, "rev-parse", "main")
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                parked, kill = self._deploy_and_kill(config, conn, job.id, sentinel)
            finally:
                conn.close()

            # The kill itself has to be real, or every assertion below is vacuous.
            # The whole result dict goes in the message: every refusal path in the
            # killer records its reason there, so a run that killed nothing says
            # why instead of just "None is not true".
            self.assertIsNone(kill.get("error"), kill)
            self.assertTrue(kill.get("killed"), f"the push was never killed: {kill}")
            self.assertIn("push", kill["command"])
            self.assertIn("--atomic", kill["command"])

            # The remote's answer, decided purely by which hook hung. The counter is
            # the remote's own record of how many pushes it applied refs for.
            after_push_sha = git(remote, "rev-parse", "main")
            if case.remote_applied:
                self.assertEqual(_applied_pushes(counter), 1)
                self.assertNotEqual(after_push_sha, base_remote_sha)
                self.assertEqual(after_push_sha, parked.pending_deploy_sha)
            else:
                self.assertEqual(_applied_pushes(counter), 0)
                self.assertEqual(after_push_sha, base_remote_sha)

            # Both halves of the table park identically: the client died by signal,
            # so the outcome is unknown and the durable marker must survive intact.
            # `failed` here would be the forbidden lie in the post-receive case.
            self.assertEqual(parked.status, "needs_reconcile")
            self.assertNotEqual(parked.pending_deploy_sha, "")
            self.assertEqual(parked.push_status, "pending")
            self.assertEqual(parked.pending_deploy_remote, config.git.remote)
            self.assertEqual(parked.pending_deploy_refs, "main")
            # The pin must be THIS job's, not merely some ref under the namespace:
            # reconcile resolves the pending sha through refs/mergetrain/pending/<id>,
            # so a pin written under the wrong id would leave the sha unresolvable
            # after a gc and route the job to `blocked` instead of reconciling.
            self.assertEqual(_pending_refs(repo), pending_ref_name(job.id))
            # -9 is git's real SIGKILL status surfacing through run_command; a
            # regression that normalized it (to 1, or to the 124 timeout code)
            # would still be ambiguous, but this pins that the classification came
            # from the signal death and not from a rewritten return code.
            self.assertIn("exit -9", parked.note)

            # The classifier, run on git's REAL dying output. Nothing git prints
            # while being killed proves a refusal, so this must stay False; if it
            # ever turns True the same run parks `blocked`/`failed` instead.
            log_text = Path(parked.log_path).read_text(encoding="utf-8", errors="replace")
            self.assertIn("git push --atomic", log_text)
            self.assertFalse(is_push_rejection(log_text))

            conn = connect(config.state.db)
            try:
                outcome = recover(config, conn, gc=False)
                healed = get_job(conn, job.id)
            finally:
                conn.close()
            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(healed.status, case.verdict)
            # Recovery reads the remote and never writes to it, in either half.
            self.assertEqual(git(remote, "rev-parse", "main"), after_push_sha)

            if case.verdict == "deployed":
                # Still one applied push: reconcile finalized from what the remote
                # already had and never re-pushed the landed deploy.
                self.assertEqual(_applied_pushes(counter), 1)
                self.assertEqual(healed.push_status, "succeeded")
                # The verify hooks never ran (the client died first), so the
                # deploy is honestly 'unknown' rather than claimed as verified.
                self.assertEqual(healed.verify_status, "unknown")
                self.assertEqual(healed.deploy_sha, after_push_sha)
                self.assertEqual(outcome.reconcile.summary["reconciled_deployed"], 1)
                # Finalized, so the write-ahead marker is retired: a row that
                # stayed 'pending' would be re-decided by the next reconcile and
                # would keep showing a phantom in-flight push in status/doctor.
                self.assertEqual(healed.pending_deploy_sha, "")
                self.assertEqual(_pending_refs(repo), "")
                return

            # Requeued: the marker and pin are gone, so nothing claims a landed
            # push, and reconcile still applied nothing to the remote itself.
            self.assertEqual(healed.pending_deploy_sha, "")
            self.assertEqual(healed.pending_deploy_remote, "")
            self.assertEqual(healed.pending_deploy_refs, "")
            # The whole marker retires together, push_status included. Leaving it
            # at 'pending' on a queued row would keep claiming a push that
            # reconcile just proved never landed — and the next attempt's outcome
            # would inherit the dead attempt's field.
            self.assertEqual(healed.push_status, "not_run")
            self.assertEqual(_pending_refs(repo), "")
            self.assertEqual(outcome.reconcile.summary["requeued"], 1)
            self.assertEqual(_applied_pushes(counter), 0)

            hook_path.unlink()  # let the retry through
            conn = connect(config.state.db)
            try:
                shipped = self._deploy(config, conn, job.id)
            finally:
                conn.close()
            self.assertEqual(shipped.status, "deployed")
            self.assertEqual(shipped.push_status, "succeeded")
            self.assertEqual(git(remote, "rev-parse", "main"), shipped.deploy_sha)
            self.assertEqual(git(remote, "show", "main:a.txt"), "a")
            # Exactly once, counted by the remote across the whole scenario: the
            # killed attempt applied nothing and the retry applied one ref update.
            # A requeue of work that HAD landed would show two here.
            self.assertEqual(_applied_pushes(counter), 1)
            self.assertEqual(_pending_refs(repo), "")

    def test_killed_atomic_push_reconciles_to_the_remotes_truth(self) -> None:
        for case in _DECISION_TABLE:
            with self.subTest(hook=case.hook, verdict=case.verdict):
                self._run_case(case)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
