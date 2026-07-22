"""Git worktree runner for mergetrain."""

from __future__ import annotations

import io
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .config import GateConfig, MergetrainConfig
from .errors import (
    AmbiguousPush,
    CancellationRequested,
    CommandFailed,
    LostLease,
    MergeBlocked,
    MergetrainError,
    PushRejected,
    redact_secrets,
)
from .models import Job
from .reuse import (
    ReuseDecision,
    environment_sha,
    gate_policy_sha,
    train_identity_sha,
    validation_age_minutes,
)
from .store import (
    clear_rejected_push,
    get_job,
    mark_job,
    record_pending_push,
    record_run_event,
    refresh_runner_lock,
    utc_now,
)

Pulse = Callable[[], None]
GateProgress = Callable[[str, str, int, int, str], None]


@dataclass(slots=True)
class _PushVerifyState:
    push_status: str = "not_run"
    verify_status: str = "not_run"
    warning: str = ""


def _render_command(command: Sequence[str] | str) -> str:
    if isinstance(command, str):
        return command
    return " ".join(str(part) for part in command)


def _dashboard_command(command: Sequence[str] | str) -> str:
    """Render a bounded gate command while masking obvious inline secrets."""

    rendered = redact_secrets(_render_command(command))
    return rendered if len(rendered) <= 500 else f"{rendered[:497]}..."


def _stop_process(process: subprocess.Popen[str]) -> bool:
    if process.poll() is not None:
        return False
    stopped = False
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows compatibility
            process.terminate()
        stopped = True
        process.wait(timeout=5)
    except ProcessLookupError:
        process.wait()
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - Windows compatibility
                process.kill()
            stopped = True
            process.wait()
    return stopped


def _run_managed(
    command: Sequence[str] | str,
    *,
    cwd: str | Path,
    env: dict[str, str] | None,
    log: IO[str] | None,
    check: bool,
    shell: bool,
    pulse: Pulse | None,
    pulse_interval_seconds: float,
    timeout_seconds: float | None,
) -> subprocess.CompletedProcess[str]:
    if pulse is not None:
        pulse()
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        shell=shell,
        executable="/bin/sh" if shell and Path("/bin/sh").exists() else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        start_new_session=os.name == "posix",
    )
    stdout_tail: deque[str] = deque(maxlen=2000)
    stderr_tail: deque[str] = deque(maxlen=2000)
    log_lock = threading.Lock()

    def drain(stream: IO[str] | None, tail: deque[str]) -> None:
        if stream is None:
            return
        for line in iter(stream.readline, ""):
            tail.append(line)
            if log is not None:
                with log_lock:
                    log.write(line)
                    log.flush()
        stream.close()

    readers = [
        threading.Thread(target=drain, args=(process.stdout, stdout_tail), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr_tail), daemon=True),
    ]
    for reader in readers:
        reader.start()

    started = time.monotonic()
    next_pulse = started + max(0.1, pulse_interval_seconds)
    timed_out = False
    try:
        while process.poll() is None:
            now = time.monotonic()
            if timeout_seconds is not None and now - started >= timeout_seconds:
                if _stop_process(process):
                    timed_out = True
                    stderr_tail.append(
                        f"command timed out after {timeout_seconds:g} seconds\n"
                    )
                    break
                continue
            if pulse is not None and now >= next_pulse:
                pulse()
                next_pulse = now + max(0.1, pulse_interval_seconds)
            try:
                # Returns the instant the process exits; the timeout only
                # bounds how long we go between pulse/timeout checks.
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
    except BaseException:
        _stop_process(process)
        raise
    finally:
        # A normally-exited process closes its pipe write ends, so the drain
        # threads hit EOF and this join is instant. When we KILLED the process
        # (timeout/cancel), the read ends can stay blocked in a pending OS read
        # — on Windows TerminateProcess does not unblock the parent's ReadFile —
        # so bound the total wait instead of blocking up to 5s per reader (which
        # made a killed command take ~10s to return). The readers are daemon
        # threads, reaped when their pipe is finalized.
        _join_deadline = time.monotonic() + 2.0
        for reader in readers:
            reader.join(timeout=max(0.0, _join_deadline - time.monotonic()))

    stdout = "".join(stdout_tail)
    stderr = "".join(stderr_tail)
    returncode = process.returncode if process.returncode is not None else 124
    if timed_out:
        returncode = 124
    completed = subprocess.CompletedProcess(command, returncode, stdout, stderr)
    if check and completed.returncode != 0:
        raise CommandFailed(command, completed.returncode, stdout, stderr, str(cwd))
    return completed


# Ceiling for commands whose callers did not pass an explicit timeout. These
# are local git operations (reset, clean, worktree remove, merge --abort) that
# finish in seconds; the ceiling only exists so a pathological hang can never
# stall a runner — or a whole hub sweep — indefinitely.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600.0


def _git_safe_env(env: dict[str, str] | None) -> dict[str, str]:
    # Never let a git subprocess block on an interactive credential or
    # host-key prompt: a daemonized runner has no terminal to answer it.
    base = dict(os.environ) if env is None else dict(env)
    base.setdefault("GIT_TERMINAL_PROMPT", "0")
    return base


def run_command(
    command: Sequence[str],
    *,
    cwd: str | Path,
    env: dict[str, str] | None = None,
    log: IO[str] | None = None,
    check: bool = True,
    pulse: Pulse | None = None,
    pulse_interval_seconds: float = 10,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    if log:
        log.write(f"\n$ {_render_command(command)}\n")
        log.flush()
    env = _git_safe_env(env)
    if timeout_seconds is None:
        timeout_seconds = DEFAULT_COMMAND_TIMEOUT_SECONDS
    if pulse is not None or timeout_seconds is not None:
        return _run_managed(
            list(command),
            cwd=cwd,
            env=env,
            log=log,
            check=check,
            shell=False,
            pulse=pulse,
            pulse_interval_seconds=pulse_interval_seconds,
            timeout_seconds=timeout_seconds,
        )
    completed = subprocess.run(
        list(command), cwd=str(cwd), env=env, text=True,
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    if log:
        if completed.stdout:
            log.write(completed.stdout)
        if completed.stderr:
            log.write(completed.stderr)
        log.flush()
    if check and completed.returncode != 0:
        raise CommandFailed(command, completed.returncode, completed.stdout, completed.stderr, str(cwd))
    return completed


def run_shell(
    command: str,
    *,
    cwd: str | Path,
    env: dict[str, str],
    log: IO[str] | None = None,
    check: bool = True,
    pulse: Pulse | None = None,
    pulse_interval_seconds: float = 10,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    if log:
        log.write(f"\n$ /bin/sh -c {redact_secrets(command)!r}\n")
        log.flush()
    env = _git_safe_env(env)
    if timeout_seconds is None:
        timeout_seconds = DEFAULT_COMMAND_TIMEOUT_SECONDS
    if pulse is not None or timeout_seconds is not None:
        return _run_managed(
            command,
            cwd=cwd,
            env=env,
            log=log,
            check=check,
            shell=True,
            pulse=pulse,
            pulse_interval_seconds=pulse_interval_seconds,
            timeout_seconds=timeout_seconds,
        )
    completed = subprocess.run(
        command, cwd=str(cwd), env=env, shell=True,
        executable="/bin/sh" if Path("/bin/sh").exists() else None,
        text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, capture_output=True,
    )
    if log:
        if completed.stdout:
            log.write(completed.stdout)
        if completed.stderr:
            log.write(completed.stderr)
        log.flush()
    if check and completed.returncode != 0:
        raise CommandFailed(command, completed.returncode, completed.stdout, completed.stderr, str(cwd))
    return completed


def git_output(args: Sequence[str], *, cwd: str | Path) -> str:
    completed = run_command(["git", *args], cwd=cwd, check=True)
    return completed.stdout.strip()


def git_output_or_empty(args: Sequence[str], *, cwd: str | Path) -> str:
    completed = run_command(["git", *args], cwd=cwd, check=False)
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def git_repo_root(path: str | Path) -> str:
    return git_output_or_empty(["rev-parse", "--show-toplevel"], cwd=path)


def git_current_branch(path: str | Path) -> str:
    return git_output_or_empty(["branch", "--show-current"], cwd=path)


def git_worktree_clean(path: str | Path) -> bool:
    # Cleanliness is a deploy safety control, not a best-effort diagnostic.
    # A failed status command is unknown state and must propagate instead of
    # collapsing to the same empty string as a clean worktree.
    return git_output(["status", "--porcelain"], cwd=path) == ""


# Remote responses that mean "you are not allowed to update this ref" rather
# than "your code/merge is wrong" — protected branches, required PRs, denied
# non-fast-forward-by-policy, forge rulesets.
_PUSH_REJECTION_MARKERS = (
    "protected branch",
    "pull request",
    "GH006",
    "GH013",
    "denied to",
    "not permitted",
    "not allowed to",
    "refusing to allow",
    "pre-receive hook declined",
    "required status check",
    "ruleset",
    "permission to",
)


def is_push_rejection(stderr: str) -> bool:
    """True when a failed push was refused for a permission/policy reason."""
    low = (stderr or "").lower()
    return any(marker.lower() in low for marker in _PUSH_REJECTION_MARKERS)


def git_dirty_paths(path: str | Path, *, limit: int = 5) -> list[str]:
    """The paths making a worktree dirty (porcelain status), for error text."""
    lines = git_output_or_empty(["status", "--porcelain"], cwd=path).splitlines()
    paths = [line[3:].strip() for line in lines if len(line) > 3]
    return paths[:limit]


def git_remote_url(path: str | Path, remote: str) -> str:
    return git_output_or_empty(["remote", "get-url", remote], cwd=path)


def git_remote_exists(path: str | Path, remote: str) -> bool:
    return bool(git_remote_url(path, remote))


def git_ref_exists(path: str | Path, ref: str) -> bool:
    completed = run_command(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=path, check=False)
    return completed.returncode == 0


def git_rev_parse(path: str | Path, ref: str) -> str:
    return git_output(["rev-parse", f"{ref}^{{commit}}"], cwd=path)


def git_tree_sha(path: str | Path, ref: str) -> str:
    return git_output(["rev-parse", f"{ref}^{{tree}}"], cwd=path)


PENDING_REF_PREFIX = "refs/mergetrain/pending/"


def pending_ref_name(job_id: int) -> str:
    """The pin ref that keeps a job's pending deploy sha resolvable across a gc."""
    return f"{PENDING_REF_PREFIX}{job_id}"


def resolve_pending_ref(path: str | Path, job_id: int) -> str:
    """Return the commit the pin ref points at, or '' if it is gone/pruned."""
    return git_output_or_empty(
        ["rev-parse", f"{pending_ref_name(job_id)}^{{commit}}"], cwd=path
    )


def delete_pending_ref(
    path: str | Path, job_id: int, *, log: IO[str] | None = None
) -> None:
    run_command(
        ["git", "update-ref", "-d", pending_ref_name(job_id)],
        cwd=path,
        log=log,
        check=False,
    )


def expand_command(command: str, *, config: MergetrainConfig, worktree: Path) -> str:
    replacements = {
        "${integration_ref}": config.git.integration_ref,
        "${project}": config.project.name,
        "${repo}": str(config.repo),
        "${worktree}": str(worktree),
    }
    expanded = command
    for key, value in replacements.items():
        expanded = expanded.replace(key, value)
    return expanded


def command_env(*, config: MergetrainConfig, worktree: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "MERGETRAIN_PROJECT": config.project.name,
            "MERGETRAIN_INTEGRATION_REF": config.git.integration_ref,
            "MERGETRAIN_REPO": str(config.repo),
            "MERGETRAIN_WORKTREE": str(worktree),
        }
    )
    return env


class _BisectAbort(Exception):
    """Bisect isolation cannot classify the failure from gate evidence."""


class GitRunner:
    """Executes queued branches in temporary Git worktrees."""

    def __init__(self, config: MergetrainConfig):
        self.config = config
        self.repo = config.repo

    def _ensure_state_dirs(self) -> None:
        self.config.state.logs.mkdir(parents=True, exist_ok=True)
        self.config.state.worktree_root.mkdir(parents=True, exist_ok=True)

    def _refresh_lease(
        self,
        conn,
        *,
        owner: str | None,
        lease_token: str,
        ttl_minutes: int,
        worktree: Path,
        head_sha: str = "",
        check_cancel: bool = True,
    ) -> None:
        """Extend the runner lease so a long-running job is never seen as stale.

        A healthy runner keeps its lease valid for the whole job; only a dead,
        hung, or recycled-PID owner lets the lease expire and become reclaimable.
        No-op when ``owner`` is None (e.g. direct test calls without a lock).
        """
        if owner is None:
            return
        refresh_runner_lock(
            conn,
            owner=owner,
            token=lease_token,
            ttl_minutes=ttl_minutes,
            worktree_path=str(worktree),
            head_sha=head_sha,
            check_cancel=check_cancel,
        )

    def _mark_job(self, conn, job_id: int, *, lease_token: str, **values) -> Job:
        return mark_job(
            conn,
            job_id,
            expected_claim_token=lease_token or None,
            **values,
        )

    def _event(
        self,
        conn,
        *,
        lease_token: str,
        phase: str,
        state: str,
        message: str,
        job_id: int | None = None,
        detail: str = "",
    ) -> None:
        record_run_event(
            conn,
            claim_token=lease_token,
            job_id=job_id,
            phase=phase,
            state=state,
            message=message,
            detail=detail,
        )

    def _finish_job(self, conn, job_id: int, *, lease_token: str, **values) -> Job:
        try:
            result = self._mark_job(conn, job_id, lease_token=lease_token, **values)
        except CancellationRequested:
            result = self._mark_job(
                conn,
                job_id,
                lease_token=lease_token,
                status="canceled",
                log_path=str(values.get("log_path", "")),
                note="canceled by user while the train was running",
            )
        event_map = {
            "validated": ("ready", "success", f"Job #{job_id} validated"),
            "blocked": ("blocked", "error", f"Job #{job_id} blocked"),
            "failed": ("failed", "error", f"Job #{job_id} failed"),
            "canceled": ("canceled", "warning", f"Job #{job_id} canceled"),
        }
        if result.status == "deployed":
            completed = self.config.terminology.completed
            if result.verify_status == "failed":
                event_map["deployed"] = (
                    "complete",
                    "warning",
                    f"Job #{job_id} {completed}; verification needs attention",
                )
            else:
                event_map["deployed"] = (
                    "complete",
                    "success",
                    f"Job #{job_id} {completed}",
                )
        event = event_map.get(result.status)
        if event:
            phase, state, message = event
            self._event(
                conn,
                lease_token=lease_token,
                job_id=job_id,
                phase=phase,
                state=state,
                message=message,
                detail="",
            )
        return result

    def _log_path(self, prefix: str, first_job_id: int) -> Path:
        stamp = utc_now().replace(":", "").replace("-", "").replace("Z", "")
        suffix = uuid.uuid4().hex[:8]
        return self.config.state.logs / f"{prefix}-{first_job_id}-{stamp}-{suffix}.log"

    def _worktree_path(self, first_job_id: int) -> Path:
        suffix = uuid.uuid4().hex[:8]
        name = f"{self.config.project.name}-mergetrain-{first_job_id}-{suffix}"
        return self.config.state.worktree_root / name

    def _cleanup_worktree(self, worktree: Path, *, log: IO[str] | None, keep_worktree: bool) -> None:
        if keep_worktree:
            if log:
                log.write(f"\nkeeping integration worktree: {worktree}\n")
            return
        if not worktree.exists():
            return
        try:
            run_command(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, log=log, check=True)
        except Exception:
            shutil.rmtree(worktree, ignore_errors=True)

    def _run_gate(
        self,
        gate: GateConfig,
        *,
        worktree: Path,
        log: IO[str],
        pulse: Pulse | None,
    ) -> None:
        command = expand_command(gate.run, config=self.config, worktree=worktree)
        env = command_env(config=self.config, worktree=worktree)
        log.write(f"\n## gate: {gate.name}\n")
        run_shell(
            command,
            cwd=worktree,
            env=env,
            log=log,
            check=True,
            pulse=pulse,
            pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )

    def _run_gates(
        self,
        *,
        worktree: Path,
        log: IO[str],
        pulse: Pulse | None,
        on_gate: GateProgress | None = None,
    ) -> None:
        total = 1 + len(self.config.gates)
        diff_command = ["git", "diff", "--check", f"{self.config.git.integration_ref}..HEAD"]
        if on_gate:
            on_gate("diff-check", "active", 1, total, _dashboard_command(diff_command))
        run_command(
            diff_command,
            cwd=worktree,
            log=log,
            pulse=pulse,
            pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )
        if on_gate:
            on_gate("diff-check", "success", 1, total, _dashboard_command(diff_command))
        for index, gate in enumerate(self.config.gates, start=2):
            if on_gate:
                on_gate(gate.name, "active", index, total, _dashboard_command(gate.run))
            self._run_gate(gate, worktree=worktree, log=log, pulse=pulse)
            if on_gate:
                on_gate(gate.name, "success", index, total, _dashboard_command(gate.run))

    def _run_verify_hooks(
        self, *, worktree: Path, log: IO[str], pulse: Pulse | None
    ) -> None:
        for hook in self.config.deploy.verify:
            command = expand_command(hook.run, config=self.config, worktree=worktree)
            env = command_env(config=self.config, worktree=worktree)
            log.write(f"\n## verify: {hook.name}\n")
            run_shell(
                command,
                cwd=worktree,
                env=env,
                log=log,
                check=True,
                pulse=pulse,
                pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                timeout_seconds=self.config.queue.command_timeout_seconds,
            )

    def _environment_fingerprint(
        self,
        *,
        worktree: Path,
        log: IO[str],
        pulse: Pulse | None,
    ) -> str:
        values: list[tuple[str, str]] = []
        for fingerprint in self.config.deploy.reuse.fingerprints:
            command = expand_command(
                fingerprint.run, config=self.config, worktree=worktree
            )
            log.write(
                f"\n## reuse fingerprint: {fingerprint.name} (opaque output hashed)\n"
            )
            completed = run_shell(
                command,
                cwd=worktree,
                env=command_env(config=self.config, worktree=worktree),
                log=None,
                check=True,
                pulse=pulse,
                pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                timeout_seconds=self.config.queue.command_timeout_seconds,
            )
            value = completed.stdout.strip()
            if not value or "\n" in value or len(value) > 512:
                raise MergetrainError(
                    f"reuse fingerprint {fingerprint.name!r} must emit one non-empty line of at most 512 characters"
                )
            values.append((fingerprint.name, value))
        return environment_sha(values)

    def _validation_identity_fields(
        self,
        *,
        jobs: Sequence[Job],
        train_id: str,
        validated_heads: dict[int, str],
        validation_sha: str,
        worktree: Path,
        log: IO[str],
        pulse: Pulse | None,
    ) -> dict[str, str]:
        return {
            "validation_tree_sha": git_tree_sha(worktree, validation_sha),
            "validation_gate_policy_sha": gate_policy_sha(self.config),
            "validation_environment_sha": self._environment_fingerprint(
                worktree=worktree, log=log, pulse=pulse
            ),
            "validation_train_sha": train_identity_sha(
                jobs,
                train_id=train_id,
                train_size=len(jobs),
                validated_heads=validated_heads,
            ),
        }

    def _reuse_decision(
        self,
        jobs: Sequence[Job],
        *,
        worktree: Path,
        integration_base_sha: str,
        authorized: bool,
        log: IO[str],
        pulse: Pulse | None,
    ) -> ReuseDecision:
        validation_shas = {job.validation_sha for job in jobs if job.validation_sha}
        validation_sha = next(iter(validation_shas)) if len(validation_shas) == 1 else ""
        if not authorized:
            return ReuseDecision(
                authorized=False,
                eligible=False,
                action="rerun",
                validation_sha=validation_sha,
                reasons=("validated gate reuse is not authorized",),
            )

        reasons: list[str] = []
        if not jobs or len({job.train_id for job in jobs}) != 1:
            reasons.append("train membership is incomplete or mixed")
        if len({job.train_size for job in jobs}) != 1 or (
            jobs and jobs[0].train_size != len(jobs)
        ):
            reasons.append("train size does not match its validated membership")
        if len(validation_shas) != 1:
            reasons.append("validated jobs do not share one validation SHA")
        if len({job.validation_base_sha for job in jobs}) != 1 or (
            jobs and jobs[0].validation_base_sha != integration_base_sha
        ):
            reasons.append("integration ref moved since validation")
        if jobs and train_identity_sha(jobs) != jobs[0].validation_train_sha:
            reasons.append("train membership identity changed since validation")
        if jobs and gate_policy_sha(self.config) != jobs[0].validation_gate_policy_sha:
            reasons.append("gate or fingerprint policy changed since validation")
        if jobs and validation_age_minutes(jobs[0].validated_at) > (
            self.config.deploy.reuse.max_age_minutes
        ):
            reasons.append("validation is older than the configured reuse age")

        required_fields = (
            "validation_tree_sha",
            "validation_gate_policy_sha",
            "validation_environment_sha",
            "validation_train_sha",
        )
        for field in required_fields:
            values = {getattr(job, field) for job in jobs if getattr(job, field)}
            if len(values) != 1 or len(values) != len(
                {getattr(job, field) for job in jobs}
            ):
                reasons.append(f"validated jobs lack one shared {field}")

        if validation_sha and not git_ref_exists(worktree, validation_sha):
            reasons.append("validation commit is missing from the local repository")
        elif validation_sha and jobs:
            if git_tree_sha(worktree, validation_sha) != jobs[0].validation_tree_sha:
                reasons.append("validation commit tree does not match its recorded identity")

        if not reasons and jobs:
            reset = run_command(
                ["git", "reset", "--hard", validation_sha],
                cwd=worktree,
                log=log,
                check=False,
                pulse=pulse,
                pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                timeout_seconds=self.config.queue.command_timeout_seconds,
            )
            if reset.returncode != 0:
                reasons.append("validation commit could not be restored for fingerprinting")
            else:
                try:
                    current_environment_sha = self._environment_fingerprint(
                        worktree=worktree, log=log, pulse=pulse
                    )
                except (CommandFailed, MergetrainError):
                    reasons.append(
                        "required environment fingerprint could not be reproduced"
                    )
                else:
                    if current_environment_sha != jobs[0].validation_environment_sha:
                        reasons.append("environment or toolchain fingerprint changed")
                finally:
                    run_command(
                        ["git", "reset", "--hard", integration_base_sha],
                        cwd=worktree,
                        log=log,
                        pulse=pulse,
                        pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                        timeout_seconds=self.config.queue.command_timeout_seconds,
                    )

        eligible = not reasons
        action = "reuse" if eligible else self.config.deploy.reuse.on_mismatch
        return ReuseDecision(
            authorized=True,
            eligible=eligible,
            action=action,
            validation_sha=validation_sha,
            reused_validation_sha=validation_sha if eligible else "",
            reasons=tuple(reasons),
        )

    def preview_validated_reuse(
        self,
        jobs: Sequence[Job],
        *,
        authorized: bool = False,
    ) -> ReuseDecision:
        """Evaluate reuse without claiming jobs, running gates, or pushing refs."""

        reuse_authorized = authorized or self.config.deploy.reuse.enabled
        validation_shas = {job.validation_sha for job in jobs if job.validation_sha}
        validation_sha = next(iter(validation_shas)) if len(validation_shas) == 1 else ""
        if not reuse_authorized:
            return ReuseDecision(
                authorized=False,
                eligible=False,
                action="rerun",
                validation_sha=validation_sha,
                reasons=("validated gate reuse is not authorized",),
            )
        self._ensure_state_dirs()
        worktree = self._worktree_path(jobs[0].id if jobs else 0)
        log = io.StringIO()
        try:
            self._prepare_worktree(worktree=worktree, log=log, pulse=None)
            for job in jobs:
                self._merge_sha_for_job(job, deploying_validated=True)
            return self._reuse_decision(
                jobs,
                worktree=worktree,
                integration_base_sha=git_rev_parse(worktree, "HEAD"),
                authorized=True,
                log=log,
                pulse=None,
            )
        except MergeBlocked as exc:
            return ReuseDecision(
                authorized=True,
                eligible=False,
                action=self.config.deploy.reuse.on_mismatch,
                validation_sha=validation_sha,
                reasons=(str(exc),),
            )
        finally:
            self._cleanup_worktree(worktree, log=None, keep_worktree=False)

    def _run_reused_gates(
        self,
        *,
        worktree: Path,
        validation_sha: str,
        log: IO[str],
        pulse: Pulse | None,
        on_gate: GateProgress | None = None,
    ) -> None:
        total = 1 + len(self.config.gates)
        if on_gate:
            on_gate("diff-check", "reused", 1, total, validation_sha)
        for index, gate in enumerate(self.config.gates, start=2):
            if not gate.always_rerun_on_deploy:
                if on_gate:
                    on_gate(gate.name, "reused", index, total, validation_sha)
                continue
            if on_gate:
                on_gate(gate.name, "active", index, total, _dashboard_command(gate.run))
            self._run_gate(gate, worktree=worktree, log=log, pulse=pulse)
            if on_gate:
                on_gate(gate.name, "success", index, total, _dashboard_command(gate.run))

    def reverify_deploy(self, *, deploy_sha: str, log: IO[str]) -> bool:
        """Re-run the configured post-push verify hooks against a deploy_sha.

        Used by ``mergetrain verify`` to discharge a job left
        verify_status='unknown' by a crash in the post-push verify window.
        Assembles a throwaway detached worktree at the deployed commit and runs
        the hooks there; returns True iff every hook passed. Raises if the
        commit cannot be checked out (the caller reports it, does not guess).
        """

        if not self.config.deploy.verify:
            return True
        self._ensure_state_dirs()
        worktree = self._worktree_path(0)
        run_command(
            ["git", "fetch", self.config.git.remote], cwd=self.repo, log=log,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )
        run_command(
            ["git", "worktree", "add", "--detach", str(worktree), deploy_sha],
            cwd=self.repo, log=log,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )
        try:
            self._run_verify_hooks(worktree=worktree, log=log, pulse=None)
            return True
        except CommandFailed:
            return False
        finally:
            self._cleanup_worktree(worktree, log=log, keep_worktree=False)

    def _assert_tree_unchanged_by_gates(self, worktree: Path, deploy_sha: str) -> None:
        """Fail closed if a gate moved HEAD or dirtied the tree after the deploy
        sha was recorded. Gates are verification, not mutation — the exact sha
        that passed gates must be the sha that is pushed and recorded (#1)."""
        head = git_rev_parse(worktree, "HEAD")
        try:
            clean = git_worktree_clean(worktree)
        except CommandFailed as exc:
            raise MergeBlocked(
                "could not verify that the gated worktree is clean; blocking "
                "because unknown worktree state must never be pushed"
            ) from exc
        if head != deploy_sha or not clean:
            detail = "left the worktree dirty" if not clean else f"moved HEAD to {head[:12]}"
            raise MergeBlocked(
                f"a gate {detail} after gating began; gates must not change the "
                f"integration tree — blocking so a commit differing from the "
                f"tested {deploy_sha[:12]} tree is never shipped"
            )

    def push_verified_head(
        self,
        *,
        worktree: Path,
        deploy_sha: str = "",
        log: IO[str] | None = None,
        pulse: Pulse | None = None,
    ) -> None:
        if not self.config.git.push_refs:
            raise MergetrainError(
                "git.push_refs must not be empty for "
                f"{self.config.terminology.action} mode"
            )
        # Push the exact recorded sha, not HEAD: if anything moved HEAD after the
        # deploy sha was captured, the recorded and pinned sha is still what ships
        # (guarantee #1). Falls back to HEAD only when no sha is threaded.
        target = deploy_sha or "HEAD"
        push_args = ["git", "push", "--atomic", self.config.git.remote]
        push_args.extend(f"{target}:{ref}" for ref in self.config.git.push_refs)
        run_command(
            push_args,
            cwd=worktree,
            log=log,
            pulse=pulse,
            pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )

    def _pending_ref(self, job_id: int) -> str:
        return pending_ref_name(job_id)

    def _push_with_marker(
        self,
        conn,
        *,
        job_ids: list[int],
        deploy_sha: str,
        lease_token: str,
        worktree: Path,
        log: IO[str] | None,
        pulse: Pulse | None,
    ) -> None:
        """Write-ahead the pending-deploy marker + pin ref, then push.

        The marker commit is fsync-durable (synchronous=FULL) before the remote
        is mutated, and the pin ref keeps deploy_sha resolvable for a later
        reconcile even if git gc prunes a crashed worktree's objects. Both the
        batch and the one-by-one isolation push go through here, so neither can
        touch the remote without first recording intent (0.3.0 Phase 1).
        """
        record_pending_push(
            conn,
            job_ids=job_ids,
            deploy_sha=deploy_sha,
            claim_token=lease_token,
            remote=self.config.git.remote,
            push_refs=self.config.git.push_refs,
        )
        for job_id in job_ids:
            run_command(
                ["git", "update-ref", self._pending_ref(job_id), deploy_sha],
                cwd=self.repo,
                log=log,
                check=False,
            )
        self.push_verified_head(
            worktree=worktree, deploy_sha=deploy_sha, log=log, pulse=pulse
        )

    def _clear_pending_refs(
        self, job_ids: list[int], *, log: IO[str] | None = None
    ) -> None:
        for job_id in job_ids:
            delete_pending_ref(self.repo, job_id, log=log)

    def _clear_rejected_push(
        self,
        conn,
        *,
        job_ids: list[int],
        lease_token: str,
        log: IO[str] | None = None,
    ) -> None:
        """Drop DB and pin markers after a push rejection proves no ref landed."""

        clear_rejected_push(conn, job_ids=job_ids, claim_token=lease_token)
        self._clear_pending_refs(job_ids, log=log)

    def _gate_progress_callback(
        self,
        conn,
        *,
        lease_token: str,
        job_id: int | None = None,
    ) -> GateProgress:
        def report(
            name: str, state: str, index: int, total: int, command: str
        ) -> None:
            verb = {"active": "Running", "reused": "Reused"}.get(
                state, "Passed"
            )
            self._event(
                conn,
                lease_token=lease_token,
                job_id=job_id,
                phase="gating",
                state=state,
                message=f"{verb} gate {index}/{total}: {name}",
                detail=command,
            )

        return report

    def _push_and_verify(
        self,
        conn,
        *,
        job_ids: list[int],
        deploy_sha: str,
        lease_token: str,
        worktree: Path,
        log: IO[str],
        before_push: Pulse,
        ownership_pulse: Pulse,
        state: _PushVerifyState,
        event_job_id: int | None = None,
    ) -> None:
        """Run the shared marker, push classification, and verify sequence."""

        before_push()
        self._event(
            conn,
            lease_token=lease_token,
            job_id=event_job_id,
            phase="pushing",
            state="active",
            message="Pushing verified HEAD atomically",
        )
        try:
            self._push_with_marker(
                conn,
                job_ids=job_ids,
                deploy_sha=deploy_sha,
                lease_token=lease_token,
                worktree=worktree,
                log=log,
                pulse=ownership_pulse,
            )
        except CommandFailed as exc:
            state.push_status = "failed"
            self._event(
                conn,
                lease_token=lease_token,
                job_id=event_job_id,
                phase="pushing",
                state="error",
                message="Atomic push failed",
                detail=f"exit_code={exc.returncode}",
            )
            if is_push_rejection(exc.stderr):
                self._clear_rejected_push(
                    conn,
                    job_ids=job_ids,
                    lease_token=lease_token,
                    log=log,
                )
                raise PushRejected(
                    "remote rejected the push (protected branch, required "
                    "pull request, or ref permission) — a repo-config issue, "
                    f"not a code failure: {exc.stderr.strip() or exc}"
                ) from exc
            # The marker is durable but the remote outcome is unknown. Preserve
            # it and park the job(s) for remote-truth reconciliation.
            raise AmbiguousPush(
                "atomic push failed after the write-ahead marker was "
                f"recorded (exit {exc.returncode}); outcome ambiguous — "
                f"parked for reconcile: {exc.stderr.strip() or exc}"
            ) from exc

        state.push_status = "succeeded"
        self._event(
            conn,
            lease_token=lease_token,
            job_id=event_job_id,
            phase="pushing",
            state="success",
            message="Atomic push completed",
        )
        if not self.config.deploy.verify:
            state.verify_status = "not_configured"
            self._event(
                conn,
                lease_token=lease_token,
                job_id=event_job_id,
                phase="verifying",
                state="success",
                message="No post-push verification configured",
            )
            return

        try:
            self._event(
                conn,
                lease_token=lease_token,
                job_id=event_job_id,
                phase="verifying",
                state="active",
                message="Running post-push verification",
            )
            self._run_verify_hooks(
                worktree=worktree, log=log, pulse=ownership_pulse
            )
            state.verify_status = "succeeded"
            self._event(
                conn,
                lease_token=lease_token,
                job_id=event_job_id,
                phase="verifying",
                state="success",
                message="Post-push verification passed",
            )
        except CommandFailed as exc:
            state.verify_status = "failed"
            state.warning = f"post-push verify warning: {exc}"
            log.write(f"\nWARNING: {state.warning}\n")
            self._event(
                conn,
                lease_token=lease_token,
                job_id=event_job_id,
                phase="verifying",
                state="warning",
                message="Post-push verification needs attention",
                detail=f"exit_code={exc.returncode}",
            )

    def _prepare_worktree(
        self, *, worktree: Path, log: IO[str], pulse: Pulse | None
    ) -> None:
        run_command(
            ["git", "fetch", self.config.git.remote],
            cwd=self.repo,
            log=log,
            pulse=pulse,
            pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )
        run_command(
            ["git", "worktree", "add", "--detach", str(worktree), self.config.git.integration_ref],
            cwd=self.repo,
            log=log,
            pulse=pulse,
            pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )

    def _merge_sha_for_job(self, job: Job, *, deploying_validated: bool) -> str:
        """Resolve and verify the exact task commit that may be merged."""

        try:
            current_sha = git_rev_parse(self.repo, f"refs/heads/{job.branch}")
        except CommandFailed as exc:
            raise MergeBlocked(f"task branch cannot be resolved: {job.branch}") from exc
        expected_ref = job.validated_head_sha if deploying_validated else job.head_sha
        if not expected_ref:
            return current_sha
        try:
            expected_sha = git_rev_parse(self.repo, expected_ref)
        except CommandFailed as exc:
            checkpoint = "validation" if deploying_validated else "enqueue"
            raise MergeBlocked(f"recorded {checkpoint} HEAD cannot be resolved for {job.branch}") from exc
        if current_sha != expected_sha:
            checkpoint = "validation" if deploying_validated else "enqueue"
            raise MergeBlocked(
                f"branch HEAD changed since {checkpoint}: {job.branch} "
                f"(expected {expected_sha}, found {current_sha}); dismiss the job (mergetrain dismiss <id>) or use --allow-duplicate, then enqueue the fix"
            )
        return expected_sha

    def process_one(
        self,
        conn,
        job: Job,
        *,
        deploy: bool,
        keep_worktree: bool = False,
        owner: str | None = None,
        ttl_minutes: int = 30,
    ) -> Job:
        self._ensure_state_dirs()
        log_path = self._log_path("job", job.id)
        worktree = self._worktree_path(job.id)
        lease_token = job.claim_token
        deploy_sha = ""
        integration_base_sha = ""
        merge_sha = ""
        deploy_state = _PushVerifyState()
        deploying_validated = deploy and bool(job.train_id)

        def pulse(*, check_cancel: bool = True) -> None:
            self._refresh_lease(
                conn,
                owner=owner,
                lease_token=lease_token,
                ttl_minutes=ttl_minutes,
                worktree=worktree,
                head_sha=deploy_sha,
                check_cancel=check_cancel,
            )

        def normal_pulse() -> None:
            pulse(check_cancel=True)

        def ownership_pulse() -> None:
            pulse(check_cancel=False)

        gate_progress = self._gate_progress_callback(
            conn, lease_token=lease_token, job_id=job.id
        )

        def finish_after_error(*, status: str, note: str) -> Job:
            if deploy_state.push_status == "succeeded":
                status = "deployed"
                note = f"post-push completion warning: {note}"
                post_push_verify_status = "failed"
            else:
                post_push_verify_status = deploy_state.verify_status
            result = self._finish_job(
                conn,
                job.id,
                lease_token=lease_token,
                status=status,
                deploy_sha=deploy_sha,
                log_path=str(log_path),
                note=note,
                push_status=deploy_state.push_status,
                verify_status=post_push_verify_status,
            )
            if result.status == "deployed":
                self._clear_pending_refs([job.id], log=log)
            return result

        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"mergetrain job {job.id}: {job.task}\n")
            mode = self.config.terminology.action if deploy else "validate"
            log.write(f"branch: {job.branch}\nmode: {mode}\n")
            log.flush()
            try:
                self._mark_job(
                    conn,
                    job.id,
                    lease_token=lease_token,
                    status="in_progress",
                    log_path=str(log_path),
                    note=job.note,
                )
                self._event(
                    conn,
                    lease_token=lease_token,
                    job_id=job.id,
                    phase="fetching",
                    state="active",
                    message=f"Fetching {self.config.git.integration_ref}",
                )
                self._prepare_worktree(worktree=worktree, log=log, pulse=normal_pulse)
                self._event(
                    conn,
                    lease_token=lease_token,
                    job_id=job.id,
                    phase="fetching",
                    state="success",
                    message="Integration worktree prepared",
                )
                integration_base_sha = git_rev_parse(worktree, "HEAD")
                if deploying_validated and job.validation_base_sha != integration_base_sha:
                    log.write(
                        "\nintegration ref moved since validation; "
                        "reassembling the train and rerunning gates\n"
                    )
                self._event(
                    conn,
                    lease_token=lease_token,
                    job_id=job.id,
                    phase="assembling",
                    state="active",
                    message=f"Merging {job.branch}",
                )
                merge_sha = self._merge_sha_for_job(job, deploying_validated=deploying_validated)
                merge = run_command(
                    ["git", "merge", "--no-edit", merge_sha],
                    cwd=worktree,
                    log=log,
                    check=False,
                    pulse=normal_pulse,
                    pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                    timeout_seconds=self.config.queue.command_timeout_seconds,
                )
                if merge.returncode != 0:
                    raise MergeBlocked(merge.stderr.strip() or merge.stdout.strip() or f"merge failed for {job.branch}")
                if not git_worktree_clean(worktree):
                    raise MergeBlocked("integration worktree is dirty after merge")
                self._event(
                    conn,
                    lease_token=lease_token,
                    job_id=job.id,
                    phase="assembling",
                    state="success",
                    message=f"Merged {job.branch}",
                )
                deploy_sha = git_rev_parse(worktree, "HEAD")
                normal_pulse()
                self._event(
                    conn,
                    lease_token=lease_token,
                    job_id=job.id,
                    phase="gating",
                    state="active",
                    message="Running train gates",
                )
                self._run_gates(
                    worktree=worktree,
                    log=log,
                    pulse=normal_pulse,
                    on_gate=gate_progress,
                )
                self._assert_tree_unchanged_by_gates(worktree, deploy_sha)
                self._event(
                    conn,
                    lease_token=lease_token,
                    job_id=job.id,
                    phase="gating",
                    state="success",
                    message="All train gates passed",
                )
                if deploy:
                    self._push_and_verify(
                        conn,
                        job_ids=[job.id],
                        deploy_sha=deploy_sha,
                        lease_token=lease_token,
                        worktree=worktree,
                        log=log,
                        before_push=normal_pulse,
                        ownership_pulse=ownership_pulse,
                        state=deploy_state,
                        event_job_id=job.id,
                    )
                status = "deployed" if deploy else "validated"
                note = deploy_state.warning or "ok"
                validation_fields = {}
                if not deploy:
                    train_id = uuid.uuid4().hex
                    validation_fields = {
                        "train_id": train_id,
                        "train_size": 1,
                        "validated_at": utc_now(),
                        "validation_base_sha": integration_base_sha,
                        "validation_sha": deploy_sha,
                        "validated_head_sha": merge_sha,
                        **self._validation_identity_fields(
                            jobs=[job],
                            train_id=train_id,
                            validated_heads={job.id: merge_sha},
                            validation_sha=deploy_sha,
                            worktree=worktree,
                            log=log,
                            pulse=normal_pulse,
                        ),
                    }
                result = self._finish_job(
                    conn,
                    job.id,
                    lease_token=lease_token,
                    status=status,
                    deploy_sha=deploy_sha,
                    log_path=str(log_path),
                    note=note,
                    push_status=deploy_state.push_status,
                    verify_status=deploy_state.verify_status,
                    **validation_fields,
                )
                if deploy and result.status == "deployed":
                    self._clear_pending_refs([job.id], log=log)
                return result
            except LostLease:
                raise
            except CancellationRequested:
                return finish_after_error(
                    status="canceled",
                    note="canceled by user while the train was running",
                )
            except MergeBlocked as exc:
                return finish_after_error(status="blocked", note=str(exc))
            except AmbiguousPush as exc:
                return finish_after_error(status="needs_reconcile", note=str(exc))
            except CommandFailed as exc:
                return finish_after_error(status="failed", note=str(exc))
            except MergetrainError as exc:
                return finish_after_error(status="blocked", note=str(exc))
            except Exception as exc:  # pragma: no cover - defensive boundary
                return finish_after_error(status="failed", note=f"unexpected error: {exc}")
            finally:
                self._cleanup_worktree(worktree, log=log, keep_worktree=keep_worktree)

    def _process_isolated_jobs(
        self,
        conn,
        jobs: Sequence[Job],
        *,
        deploy: bool,
        keep_worktree: bool,
        owner: str | None,
        ttl_minutes: int,
        lease_token: str,
    ) -> list[Job]:
        """Process isolated jobs in order, stopping at an ambiguous deploy.

        Isolation happens after the whole batch has already been claimed. If an
        isolated push becomes ambiguous, no later job may target the same refs
        until reconcile resolves that outcome. Return the untouched suffix to
        ``queued`` so it is neither stranded in-progress nor pushed out of FIFO
        order.
        """

        results: list[Job] = []
        for index, job in enumerate(jobs):
            result = self.process_one(
                conn,
                job,
                deploy=deploy,
                keep_worktree=keep_worktree,
                owner=owner,
                ttl_minutes=ttl_minutes,
            )
            results.append(result)
            if not deploy or result.status != "needs_reconcile":
                continue

            note = (
                f"deferred because isolated job {job.id} has an unresolved "
                "push; reconcile before deploying this job"
            )
            self._event(
                conn,
                lease_token=lease_token,
                phase="pushing",
                state="warning",
                message="Isolation stopped for pending reconcile",
                detail=f"job_id={job.id}",
            )
            for pending in jobs[index + 1 :]:
                current = get_job(conn, pending.id)
                if current.status == "in_progress" and (
                    not lease_token or current.claim_token == lease_token
                ):
                    current = self._finish_job(
                        conn,
                        pending.id,
                        lease_token=lease_token,
                        status="queued",
                        note=note,
                    )
                results.append(current)
            break
        return results

    def _bisect_failed_train(
        self,
        conn,
        merged_jobs: list[Job],
        *,
        merge_shas: dict[int, str],
        integration_base_sha: str,
        worktree: Path,
        log: IO[str],
        log_path: Path,
        lease_token: str,
        deploy: bool,
        keep_worktree: bool,
        owner: str | None,
        ttl_minutes: int,
        pulse: Pulse,
    ) -> list[Job]:
        """Isolate a failed train in O(log n) gate runs instead of O(n).

        Bisection only ever *removes* jobs from the train: individually
        failing jobs finish as ``failed``, and combinations whose members
        pass alone but fail together finish as ``blocked`` semantic
        conflicts with ``conflict_with`` naming the partners. Surviving
        jobs are re-run through ``process_batch``, so nothing ships without
        a full gate pass over the exact final combination.
        """
        order = {job.id: index for index, job in enumerate(merged_jobs)}
        probe_cache: dict[frozenset[int], bool] = {}
        probe_count = 0
        probe_worktree = self._worktree_path(merged_jobs[0].id)

        def probe(subset: Sequence[Job]) -> bool:
            """Assemble ``subset`` on the recorded base and run the gates.

            Returns True iff the merges are clean and every gate passes.
            Raises ``_BisectAbort`` on a merge conflict: a subset whose merge
            does not reproduce the train's context cannot be classified by
            gate evidence, so the caller falls back to linear isolation.
            """
            nonlocal probe_count
            members = sorted(subset, key=lambda job: order[job.id])
            key = frozenset(job.id for job in members)
            if key in probe_cache:
                return probe_cache[key]
            probe_count += 1
            ids = [job.id for job in members]
            log.write(f"\n## bisect probe {probe_count}: jobs {ids}\n")
            self._event(
                conn,
                lease_token=lease_token,
                phase="gating",
                state="active",
                message=f"Bisect probe {probe_count}: jobs {ids}",
            )
            pulse()
            run_command(
                ["git", "reset", "--hard", integration_base_sha],
                cwd=probe_worktree,
                log=log,
            )
            run_command(
                ["git", "clean", "-fdx"], cwd=probe_worktree, log=log, check=False
            )
            for job in members:
                merge = run_command(
                    ["git", "merge", "--no-edit", merge_shas[job.id]],
                    cwd=probe_worktree,
                    log=log,
                    check=False,
                    pulse=pulse,
                    pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                    timeout_seconds=self.config.queue.command_timeout_seconds,
                )
                if merge.returncode != 0:
                    run_command(
                        ["git", "merge", "--abort"],
                        cwd=probe_worktree,
                        log=log,
                        check=False,
                    )
                    raise _BisectAbort(
                        f"probe merge of job {job.id} ({job.branch}) conflicted "
                        f"without its train predecessors"
                    )
            try:
                self._run_gates(worktree=probe_worktree, log=log, pulse=pulse)
                passed = True
            except CommandFailed:
                passed = False
            probe_cache[key] = passed
            return passed

        singles: list[Job] = []
        conflict_sets: list[list[Job]] = []

        def minimize_joint_failure(subset: list[Job]) -> None:
            """Both halves of ``subset`` pass alone, so the failure is joint.

            Greedily shrink to a minimal failing set, then verify each
            remaining member really passes alone before calling the set a
            semantic conflict. Members proven unnecessary rejoin the
            survivors; a failure that does not reproduce aborts to linear
            isolation instead of blaming anyone.
            """
            if probe(subset):
                raise _BisectAbort(
                    "train gate failure did not reproduce when the full "
                    "subset was re-assembled (flaky gate?)"
                )
            minimal = list(subset)
            for job in list(minimal):
                if len(minimal) == 1:
                    break
                reduced = [item for item in minimal if item.id != job.id]
                if not probe(reduced):
                    minimal = reduced
            if len(minimal) == 1:
                singles.append(minimal[0])
                return
            solo_failures = [job for job in minimal if not probe([job])]
            if solo_failures:
                # A member fails alone: the joint attribution is unsound, so
                # only the proven-solo failures are removed; the rest rejoin
                # the survivors (a remaining real conflict re-surfaces there).
                singles.extend(solo_failures)
                return
            conflict_sets.append(minimal)

        def descend(subset: list[Job]) -> None:
            # Invariant: subset is known to fail as a combination — proven by
            # the original train gate run (top level) or by a probe.
            if len(subset) == 1:
                singles.append(subset[0])
                return
            mid = len(subset) // 2
            left, right = subset[:mid], subset[mid:]
            left_fails = not probe(left)
            right_fails = not probe(right)
            if left_fails:
                descend(left)
            if right_fails:
                descend(right)
            if left_fails or right_fails:
                return
            minimize_joint_failure(subset)

        try:
            run_command(
                [
                    "git",
                    "worktree",
                    "add",
                    "--detach",
                    str(probe_worktree),
                    integration_base_sha,
                ],
                cwd=self.repo,
                log=log,
            )
            try:
                descend(list(merged_jobs))
            finally:
                self._cleanup_worktree(probe_worktree, log=log, keep_worktree=False)
        except _BisectAbort as abort:
            log.write(f"\nbisect aborted: {abort}; falling back to linear isolation\n")
            self._event(
                conn,
                lease_token=lease_token,
                phase="gating",
                state="warning",
                message="Bisect inconclusive; isolating jobs one-by-one",
                detail=str(abort),
            )
            return self._process_isolated_jobs(
                conn,
                merged_jobs,
                deploy=deploy,
                keep_worktree=keep_worktree,
                owner=owner,
                ttl_minutes=ttl_minutes,
                lease_token=lease_token,
            )

        culprit_ids = {job.id for job in singles}
        for group in conflict_sets:
            culprit_ids.update(job.id for job in group)
        goods = [job for job in merged_jobs if job.id not in culprit_ids]

        results = []
        for job in singles:
            results.append(
                self._finish_job(
                    conn,
                    job.id,
                    lease_token=lease_token,
                    status="failed",
                    log_path=str(log_path),
                    note=(
                        "failed train gates individually during bisect isolation; "
                        "fix the branch and dismiss the job (mergetrain dismiss <id>) or use --allow-duplicate, then enqueue the fix"
                    ),
                )
            )
        for group in conflict_sets:
            for job in group:
                others = [item for item in group if item.id != job.id]
                partners = ", ".join(
                    f"job {other.id} ({other.branch} @ {merge_shas[other.id][:12]})"
                    for other in others
                )
                note = (
                    "semantic conflict: passes gates alone but fails combined "
                    f"with {partners}; rebase onto the integration branch with "
                    "the other side merged, fix the joint breakage, and enqueue "
                    "a fresh job"
                )
                results.append(
                    self._finish_job(
                        conn,
                        job.id,
                        lease_token=lease_token,
                        status="blocked",
                        log_path=str(log_path),
                        note=note,
                        conflict_with=",".join(str(other.id) for other in others),
                    )
                )
        summary = (
            f"bisect isolation: {probe_count} probe(s), {len(singles)} failing alone, "
            f"{sum(len(group) for group in conflict_sets)} in conflict, "
            f"{len(goods)} rejoining"
        )
        log.write(f"\n{summary}\n")
        self._event(
            conn,
            lease_token=lease_token,
            phase="gating",
            state="warning" if conflict_sets else "success",
            message=f"Bisect isolation complete: {len(goods)} job(s) rejoin the train",
            detail=summary,
        )
        self._cleanup_worktree(worktree, log=log, keep_worktree=keep_worktree)
        if goods:
            results.extend(
                self.process_batch(
                    conn,
                    goods,
                    deploy=deploy,
                    keep_worktree=keep_worktree,
                    owner=owner,
                    ttl_minutes=ttl_minutes,
                )
            )
        return results

    def process_batch(
        self,
        conn,
        jobs: Iterable[Job],
        *,
        deploy: bool,
        keep_worktree: bool = False,
        owner: str | None = None,
        ttl_minutes: int = 30,
        reuse_validated: bool = False,
    ) -> list[Job]:
        jobs = list(jobs)
        if not jobs:
            return []
        claim_tokens = {job.claim_token for job in jobs}
        if owner is not None and (len(claim_tokens) != 1 or not next(iter(claim_tokens))):
            raise LostLease("batch jobs do not share one valid claim token")
        lease_token = next(iter(claim_tokens)) if owner is not None else ""
        validated_train_ids = {job.train_id for job in jobs if job.train_id}
        deploying_validated = deploy and bool(validated_train_ids)
        self._ensure_state_dirs()
        log_path = self._log_path("batch", jobs[0].id)
        worktree = self._worktree_path(jobs[0].id)
        merged_jobs: list[Job] = []
        results: list[Job] = []
        merge_shas: dict[int, str] = {}
        deploy_sha = ""
        integration_base_sha = ""
        deploy_state = _PushVerifyState()
        reused_validation_sha = ""
        reuse_fallback_reason = ""
        reuse_authorized = reuse_validated or self.config.deploy.reuse.enabled

        def pulse(*, check_cancel: bool = True) -> None:
            self._refresh_lease(
                conn,
                owner=owner,
                lease_token=lease_token,
                ttl_minutes=ttl_minutes,
                worktree=worktree,
                head_sha=deploy_sha,
                check_cancel=check_cancel,
            )

        def normal_pulse() -> None:
            pulse(check_cancel=True)

        def ownership_pulse() -> None:
            pulse(check_cancel=False)

        gate_progress = self._gate_progress_callback(
            conn, lease_token=lease_token
        )

        def finish(item: Job, **values) -> Job:
            return self._finish_job(
                conn, item.id, lease_token=lease_token, **values
            )

        def cancel_active_jobs() -> list[Job]:
            canceled: list[Job] = []
            for item in jobs:
                current = get_job(conn, item.id)
                if current.status == "in_progress" and current.claim_token == lease_token:
                    canceled.append(
                        finish(
                            item,
                            status="canceled",
                            log_path=str(log_path),
                            note="canceled by user while the train was running",
                        )
                    )
                else:
                    canceled.append(current)
            return canceled

        def finish_active_after_error(*, status: str, note: str) -> list[Job]:
            affected_jobs = jobs if deploying_validated else merged_jobs or jobs
            if deploy_state.push_status == "succeeded":
                status = "deployed"
                note = f"post-push completion warning: {note}"
                post_push_verify_status = "failed"
            else:
                post_push_verify_status = deploy_state.verify_status
            deployed_ids: list[int] = []
            for item in affected_jobs:
                current = get_job(conn, item.id)
                if current.status == "in_progress" and current.claim_token == lease_token:
                    result = finish(
                        item,
                        status=status,
                        deploy_sha=deploy_sha,
                        log_path=str(log_path),
                        note=note,
                        push_status=deploy_state.push_status,
                        verify_status=post_push_verify_status,
                        reused_validation_sha=reused_validation_sha,
                    )
                    results.append(result)
                    if result.status == "deployed":
                        deployed_ids.append(item.id)
            if deployed_ids:
                self._clear_pending_refs(deployed_ids, log=log)
            return results

        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"mergetrain batch starting at job {jobs[0].id}\n")
            mode = self.config.terminology.action if deploy else "validate"
            log.write(f"jobs: {[job.id for job in jobs]}\nmode: {mode}\n")
            log.flush()
            try:
                for job in jobs:
                    self._mark_job(
                        conn,
                        job.id,
                        lease_token=lease_token,
                        status="in_progress",
                        log_path=str(log_path),
                        note=job.note,
                    )
                if deploying_validated and (
                    len(validated_train_ids) != 1
                    or any(not job.train_id for job in jobs)
                    or {job.train_size for job in jobs} != {len(jobs)}
                ):
                    note = "validated train identity is incomplete or mixes multiple trains; enqueue a fresh train"
                    return [
                        finish(job, status="blocked", log_path=str(log_path), note=note)
                        for job in jobs
                    ]
                self._event(
                    conn,
                    lease_token=lease_token,
                    phase="fetching",
                    state="active",
                    message=f"Fetching {self.config.git.integration_ref}",
                )
                self._prepare_worktree(worktree=worktree, log=log, pulse=normal_pulse)
                self._event(
                    conn,
                    lease_token=lease_token,
                    phase="fetching",
                    state="success",
                    message="Integration worktree prepared",
                )
                integration_base_sha = git_rev_parse(worktree, "HEAD")
                if deploying_validated:
                    validation_bases = {job.validation_base_sha for job in jobs}
                    try:
                        merge_shas = {
                            job.id: self._merge_sha_for_job(job, deploying_validated=True)
                            for job in jobs
                        }
                    except MergeBlocked as exc:
                        note = f"validated train identity check failed: {exc}"
                        return [
                            finish(job, status="blocked", log_path=str(log_path), note=note)
                            for job in jobs
                        ]
                    if reuse_authorized:
                        reuse_decision = self._reuse_decision(
                            jobs,
                            worktree=worktree,
                            integration_base_sha=integration_base_sha,
                            authorized=True,
                            log=log,
                            pulse=normal_pulse,
                        )
                        if reuse_decision.eligible:
                            reused_validation_sha = reuse_decision.reused_validation_sha
                            self._event(
                                conn,
                                lease_token=lease_token,
                                phase="assembling",
                                state="active",
                                message="Restoring exact validated train commit",
                                detail=reused_validation_sha,
                            )
                            run_command(
                                ["git", "reset", "--hard", reused_validation_sha],
                                cwd=worktree,
                                log=log,
                                pulse=normal_pulse,
                                pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                                timeout_seconds=self.config.queue.command_timeout_seconds,
                            )
                            deploy_sha = git_rev_parse(worktree, "HEAD")
                            if deploy_sha != reused_validation_sha or not git_worktree_clean(worktree):
                                raise MergeBlocked(
                                    "exact validation commit could not be restored cleanly"
                                )
                            merged_jobs.extend(jobs)
                            self._event(
                                conn,
                                lease_token=lease_token,
                                phase="assembling",
                                state="success",
                                message="Exact validated train commit restored",
                                detail=reused_validation_sha,
                            )
                        else:
                            reuse_fallback_reason = "; ".join(reuse_decision.reasons)
                            log.write(
                                "\nvalidated gate reuse declined: "
                                f"{reuse_fallback_reason}\n"
                            )
                            if reuse_decision.action == "fail":
                                raise MergeBlocked(
                                    "validated gate reuse policy failed closed: "
                                    f"{reuse_fallback_reason}"
                                )
                    if not reused_validation_sha and validation_bases != {integration_base_sha}:
                        log.write(
                            "\nintegration ref moved since validation; "
                            "reassembling the exact train and rerunning gates\n"
                        )

                if not reused_validation_sha:
                    self._event(
                        conn,
                        lease_token=lease_token,
                        phase="assembling",
                        state="active",
                        message=f"Assembling train with {len(jobs)} job(s)",
                    )
                    for job in jobs:
                        log.write(f"\n## merge job {job.id}: {job.branch}\n")
                        normal_pulse()
                        self._event(
                            conn,
                            lease_token=lease_token,
                            job_id=job.id,
                            phase="assembling",
                            state="active",
                            message=f"Merging {job.branch}",
                        )
                        if not deploying_validated:
                            try:
                                merge_shas[job.id] = self._merge_sha_for_job(job, deploying_validated=False)
                            except MergeBlocked as exc:
                                results.append(
                                    finish(job, status="blocked", log_path=str(log_path), note=str(exc))
                                )
                                continue
                        pre_merge_head = git_output(["rev-parse", "HEAD"], cwd=worktree)
                        merge = run_command(
                            ["git", "merge", "--no-edit", merge_shas[job.id]],
                            cwd=worktree,
                            log=log,
                            check=False,
                            pulse=normal_pulse,
                            pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                            timeout_seconds=self.config.queue.command_timeout_seconds,
                        )
                        if merge.returncode != 0:
                            note = merge.stderr.strip() or merge.stdout.strip() or f"merge failed for {job.branch}"
                            if deploying_validated:
                                run_command(["git", "merge", "--abort"], cwd=worktree, log=log, check=False)
                                note = f"validated train could not be reassembled: {note}"
                                return [
                                    finish(item, status="blocked", log_path=str(log_path), note=note)
                                    for item in jobs
                                ]
                            results.append(finish(job, status="blocked", log_path=str(log_path), note=note))
                            run_command(["git", "merge", "--abort"], cwd=worktree, log=log, check=False)
                            continue
                        if not git_worktree_clean(worktree):
                            if deploying_validated:
                                note = "validated train produced a dirty integration worktree after reassembly"
                                return [
                                    finish(item, status="blocked", log_path=str(log_path), note=note)
                                    for item in jobs
                                ]
                            results.append(
                                finish(
                                    job,
                                    status="blocked",
                                    log_path=str(log_path),
                                    note="integration worktree is dirty after merge",
                                )
                            )
                            # the merge already committed (HEAD advanced), so
                            # `reset --hard HEAD` would only drop the stray dirt
                            # and keep this blocked job's merge commit in the
                            # assembled tree. Reset to the pre-merge tip instead
                            # so a blocked job can never ride the train.
                            run_command(
                                ["git", "reset", "--hard", pre_merge_head],
                                cwd=worktree, log=log, check=True,
                            )
                            continue
                        merged_jobs.append(job)
                        self._event(
                            conn,
                            lease_token=lease_token,
                            job_id=job.id,
                            phase="assembling",
                            state="success",
                            message=f"Merged {job.branch}",
                        )
                    if not merged_jobs:
                        log.write("\nno jobs were merged\n")
                        return results
                    self._event(
                        conn,
                        lease_token=lease_token,
                        phase="assembling",
                        state="success",
                        message=f"Assembled {len(merged_jobs)} job(s)",
                    )
                    deploy_sha = git_rev_parse(worktree, "HEAD")
                normal_pulse()
                try:
                    if reuse_fallback_reason:
                        self._event(
                            conn,
                            lease_token=lease_token,
                            phase="gating",
                            state="warning",
                            message="Validated gates were not reused; rerunning all gates",
                            detail=reuse_fallback_reason,
                        )
                    self._event(
                        conn,
                        lease_token=lease_token,
                        phase="gating",
                        state="active",
                        message=(
                            "Reusing validated gates"
                            if reused_validation_sha
                            else "Running train gates"
                        ),
                        detail=reused_validation_sha,
                    )
                    if reused_validation_sha:
                        self._run_reused_gates(
                            worktree=worktree,
                            validation_sha=reused_validation_sha,
                            log=log,
                            pulse=normal_pulse,
                            on_gate=gate_progress,
                        )
                    else:
                        self._run_gates(
                            worktree=worktree,
                            log=log,
                            pulse=normal_pulse,
                            on_gate=gate_progress,
                        )
                    self._assert_tree_unchanged_by_gates(worktree, deploy_sha)
                    self._event(
                        conn,
                        lease_token=lease_token,
                        phase="gating",
                        state="success",
                        message="All train gates passed",
                        detail=reused_validation_sha,
                    )
                except CommandFailed as exc:
                    if deploying_validated:
                        gate_mode = "validated reuse" if reused_validation_sha else "reassembly"
                        note = f"validated train gate failed after {gate_mode}: {exc}"
                        return [
                            finish(
                                job,
                                status="failed",
                                deploy_sha=deploy_sha,
                                log_path=str(log_path),
                                note=note,
                            )
                            for job in jobs
                        ]
                    if len(merged_jobs) <= 3:
                        log.write("\ntrain gate failed; isolating merged jobs one-by-one\n")
                        self._event(
                            conn,
                            lease_token=lease_token,
                            phase="gating",
                            state="warning",
                            message="Train gate failed; isolating jobs",
                            detail=f"exit_code={exc.returncode}",
                        )
                        self._cleanup_worktree(worktree, log=log, keep_worktree=False)
                        results.extend(
                            self._process_isolated_jobs(
                                conn,
                                merged_jobs,
                                deploy=deploy,
                                keep_worktree=keep_worktree,
                                owner=owner,
                                ttl_minutes=ttl_minutes,
                                lease_token=lease_token,
                            )
                        )
                        return results
                    log.write(
                        f"\ntrain gate failed; bisecting {len(merged_jobs)} merged jobs\n"
                    )
                    self._event(
                        conn,
                        lease_token=lease_token,
                        phase="gating",
                        state="warning",
                        message=f"Train gate failed; bisecting {len(merged_jobs)} jobs",
                        detail=f"exit_code={exc.returncode}",
                    )
                    results.extend(
                        self._bisect_failed_train(
                            conn,
                            merged_jobs,
                            merge_shas=merge_shas,
                            integration_base_sha=integration_base_sha,
                            worktree=worktree,
                            log=log,
                            log_path=log_path,
                            lease_token=lease_token,
                            deploy=deploy,
                            keep_worktree=keep_worktree,
                            owner=owner,
                            ttl_minutes=ttl_minutes,
                            pulse=normal_pulse,
                        )
                    )
                    return results
                if deploy:
                    self._push_and_verify(
                        conn,
                        job_ids=[job.id for job in merged_jobs],
                        deploy_sha=deploy_sha,
                        lease_token=lease_token,
                        worktree=worktree,
                        log=log,
                        before_push=normal_pulse,
                        ownership_pulse=ownership_pulse,
                        state=deploy_state,
                    )
                status = "deployed" if deploy else "validated"
                note = deploy_state.warning or (
                    f"batch ok; reused validation {reused_validation_sha}"
                    if reused_validation_sha
                    else f"batch ok; merged {len(merged_jobs)} job(s)"
                )
                train_id = uuid.uuid4().hex if not deploy else ""
                validated_at = utc_now() if not deploy else ""
                validation_identity_fields: dict[str, str] = {}
                if not deploy:
                    validation_identity_fields = self._validation_identity_fields(
                        jobs=merged_jobs,
                        train_id=train_id,
                        validated_heads=merge_shas,
                        validation_sha=deploy_sha,
                        worktree=worktree,
                        log=log,
                        pulse=normal_pulse,
                    )
                for job in merged_jobs:
                    validation_fields = {}
                    if not deploy:
                        validation_fields = {
                            "train_id": train_id,
                            "train_size": len(merged_jobs),
                            "validated_at": validated_at,
                            "validation_base_sha": integration_base_sha,
                            "validation_sha": deploy_sha,
                            "validated_head_sha": merge_shas[job.id],
                            **validation_identity_fields,
                        }
                    results.append(
                        finish(
                            job,
                            status=status,
                            deploy_sha=deploy_sha,
                            log_path=str(log_path),
                            note=note,
                            push_status=deploy_state.push_status,
                            verify_status=deploy_state.verify_status,
                            reused_validation_sha=reused_validation_sha,
                            **validation_fields,
                        )
                    )
                if deploy:
                    self._clear_pending_refs(
                        [job.id for job in merged_jobs], log=log
                    )
                return results
            except LostLease:
                raise
            except CancellationRequested:
                if deploy_state.push_status == "succeeded":
                    return finish_active_after_error(
                        status="canceled",
                        note="canceled by user while the train was running",
                    )
                return cancel_active_jobs()
            except AmbiguousPush as exc:
                return finish_active_after_error(status="needs_reconcile", note=str(exc))
            except CommandFailed as exc:
                return finish_active_after_error(status="failed", note=str(exc))
            except MergetrainError as exc:
                return finish_active_after_error(status="blocked", note=str(exc))
            except Exception as exc:  # pragma: no cover - defensive boundary
                return finish_active_after_error(
                    status="failed", note=f"unexpected error: {exc}"
                )
            finally:
                self._cleanup_worktree(worktree, log=log, keep_worktree=keep_worktree)


def find_worktree_gc_candidates(
    config: MergetrainConfig, *, protect: Iterable[str] = ()
) -> list[dict[str, str]]:
    root = config.state.worktree_root
    prefix = f"{config.project.name}-mergetrain-"
    if not root.exists():
        return []
    # A live runner's integration worktree must never be a gc candidate —
    # removing it mid-run kills the deploy. Callers pass the live lock's
    # worktree_path; it is reported (skipped), not silently dropped.
    protected = {str(Path(p)) for p in protect if p}
    candidates = []
    for path in sorted(root.iterdir()):
        if not (path.is_dir() and path.name.startswith(prefix)):
            continue
        if str(path) in protected:
            candidates.append(
                {"path": str(path), "reason": "active runner worktree, skipped", "protected": "true"}
            )
            continue
        candidates.append({"path": str(path), "reason": "temporary mergetrain worktree"})
    return candidates


def branch_exists(repo: Path, branch: str) -> bool:
    return run_command(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo, check=False).returncode == 0


def current_branch(repo: Path) -> str:
    return git_current_branch(repo)


def apply_gc(
    config: MergetrainConfig,
    *,
    delete_branches: Iterable[str] = (),
    protect: Iterable[str] = (),
    live_worktree_now: Callable[[], str | None] | None = None,
) -> dict[str, list[dict[str, str]]]:
    removed_worktrees: list[dict[str, str]] = []
    deleted_branches: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for candidate in find_worktree_gc_candidates(config, protect=protect):
        if candidate.get("protected"):
            continue  # a live runner is merging/gating here — never remove it
        path = Path(candidate["path"])
        # The protect list is a snapshot taken before this loop. A runner that
        # acquired the lock since then holds a worktree absent from it. Re-read
        # the live lock immediately before each removal and never delete a tree a
        # running deploy is now inside (#84, defect 5).
        if live_worktree_now is not None:
            active = live_worktree_now()
            if active and Path(active) == path:
                continue
        try:
            run_command(["git", "worktree", "remove", "--force", str(path)], cwd=config.repo, check=True)
        except Exception:
            shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed_worktrees.append(candidate)
        else:
            failed.append({"path": str(path), "reason": "could not remove worktree"})
    active_branch = current_branch(config.repo)
    for branch in delete_branches:
        if branch == active_branch:
            failed.append({"branch": branch, "reason": "currently checked out"})
            continue
        if not branch_exists(config.repo, branch):
            continue
        completed = run_command(["git", "branch", "-D", branch], cwd=config.repo, check=False)
        if completed.returncode == 0:
            deleted_branches.append({"branch": branch, "reason": "terminal queue branch"})
        else:
            failed.append({"branch": branch, "reason": completed.stderr.strip() or "delete failed"})
    return {"removed_worktrees": removed_worktrees, "deleted_branches": deleted_branches, "failed": failed}
