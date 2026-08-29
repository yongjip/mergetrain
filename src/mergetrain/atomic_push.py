"""Atomic Git push primitives with durable local recovery evidence."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .command_runner import Pulse, run_command
from .config import MergetrainConfig
from .errors import (
    AmbiguousPush,
    CancellationRequested,
    CommandFailed,
    MergeBlocked,
    MergetrainError,
    PushRejected,
)
from .git_ops import (
    delete_pending_ref,
    deploy_audit_ref_name,
    git_remote_ref_sha,
    git_rev_parse,
    git_worktree_clean,
    is_push_rejection,
    pending_ref_name,
    resolve_pending_ref,
)
from .store import clear_rejected_push, record_pending_push

AuditExpectation = Callable[..., tuple[str, str]]
PushVerified = Callable[..., None]
EventWriter = Callable[..., None]
VerifyHooks = Callable[..., None]


@dataclass(slots=True)
class PushVerifyState:
    push_status: str = "not_run"
    verify_status: str = "not_run"
    warning: str = ""


def post_push_verify_status(state: PushVerifyState) -> str:
    """Return the truthful verification state after a landed push and error."""

    if state.verify_status in {"not_configured", "succeeded", "failed"}:
        return state.verify_status
    return "unknown"


class AtomicPush:
    """Prepare immutable evidence and push one exact verified commit."""

    def __init__(self, config: MergetrainConfig):
        self.config = config
        self.repo = config.repo

    def assert_tree_unchanged(self, worktree: Path, deploy_sha: str) -> None:
        """Fail closed unless the exact gated commit and clean tree remain."""

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
        audit_ref: str = "",
        audit_expected_sha: str | None = None,
        audit_expectation: AuditExpectation | None = None,
    ) -> None:
        if not self.config.git.push_refs:
            raise MergetrainError(
                "git.push_refs must not be empty for deploy mode"
            )
        target = deploy_sha or git_rev_parse(worktree, "HEAD")
        audit_ref = audit_ref or deploy_audit_ref_name(target)
        if audit_expected_sha is None:
            expectation = audit_expectation or self.audit_ref_expectation
            audit_ref, audit_expected_sha = expectation(
                worktree=worktree,
                deploy_sha=target,
                log=log,
                pulse=pulse,
            )
        push_args = [
            "git",
            "push",
            "--atomic",
            f"--force-with-lease={audit_ref}:{audit_expected_sha}",
            self.config.git.remote,
        ]
        push_args.extend(f"{target}:{ref}" for ref in self.config.git.push_refs)
        if audit_ref not in self.config.git.push_refs:
            push_args.append(f"{target}:{audit_ref}")
        run_command(
            push_args,
            cwd=worktree,
            log=log,
            pulse=pulse,
            pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )

    def audit_ref_expectation(
        self,
        *,
        worktree: Path,
        deploy_sha: str,
        log: IO[str] | None,
        pulse: Pulse | None = None,
    ) -> tuple[str, str]:
        """Read the immutable audit ref and return its push lease expectation."""

        audit_ref = deploy_audit_ref_name(deploy_sha)
        reachable, current = git_remote_ref_sha(
            worktree,
            self.config.git.remote,
            audit_ref,
            log=log,
            pulse=pulse,
            pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )
        if not reachable:
            raise MergetrainError(
                f"could not inspect deploy audit ref {audit_ref}; push was not attempted"
            )
        if current and current != deploy_sha:
            raise PushRejected(
                f"deploy audit ref {audit_ref} points to {current}, expected "
                f"{deploy_sha}; refusing to rewrite immutable audit evidence"
            )
        return audit_ref, current

    def push_with_marker(
        self,
        conn: sqlite3.Connection,
        *,
        job_ids: list[int],
        deploy_sha: str,
        lease_token: str,
        worktree: Path,
        log: IO[str] | None,
        pulse: Pulse | None,
        audit_ref: str,
        audit_expected_sha: str,
        push_verified: PushVerified | None = None,
    ) -> None:
        """Persist pending intent and local pins before touching the remote."""

        record_pending_push(
            conn,
            job_ids=job_ids,
            deploy_sha=deploy_sha,
            claim_token=lease_token,
            remote=self.config.git.remote,
            push_refs=self.config.git.push_refs,
        )
        for job_id in job_ids:
            pending_ref = pending_ref_name(job_id)
            try:
                run_command(
                    ["git", "update-ref", pending_ref, deploy_sha],
                    cwd=self.repo,
                    log=log,
                    check=True,
                )
            except CommandFailed as exc:
                raise MergetrainError(
                    f"could not create recovery pin {pending_ref}; push was not attempted"
                ) from exc
            pinned_sha = resolve_pending_ref(self.repo, job_id)
            if pinned_sha != deploy_sha:
                raise MergetrainError(
                    f"recovery pin {pending_ref} resolved to "
                    f"{pinned_sha or 'nothing'}, expected {deploy_sha}; "
                    "push was not attempted"
                )
        push = push_verified or self.push_verified_head
        push(
            worktree=worktree,
            deploy_sha=deploy_sha,
            log=log,
            pulse=pulse,
            audit_ref=audit_ref,
            audit_expected_sha=audit_expected_sha,
        )

    def clear_pending_refs(self, job_ids: list[int], *, log: IO[str] | None = None) -> None:
        for job_id in job_ids:
            delete_pending_ref(self.repo, job_id, log=log)

    def clear_rejected_push(
        self,
        conn: sqlite3.Connection,
        *,
        job_ids: list[int],
        lease_token: str,
        log: IO[str] | None = None,
    ) -> None:
        """Drop DB and local pins only after a rejection proves nothing landed."""

        clear_rejected_push(conn, job_ids=job_ids, claim_token=lease_token)
        self.clear_pending_refs(job_ids, log=log)

    def deploy_and_verify(
        self,
        conn: sqlite3.Connection,
        *,
        job_ids: list[int],
        deploy_sha: str,
        lease_token: str,
        worktree: Path,
        log: IO[str],
        before_push: Pulse,
        ownership_pulse: Pulse,
        state: PushVerifyState,
        event: EventWriter,
        event_job_id: int | None = None,
        audit_expectation: AuditExpectation | None = None,
        push_with_marker: Callable[..., None] | None = None,
        clear_rejected: Callable[..., None] | None = None,
        run_verify_hooks: VerifyHooks | None = None,
    ) -> None:
        """Run audit preflight, durable marker, atomic push, and verification."""

        before_push()
        event(
            conn,
            lease_token=lease_token,
            job_id=event_job_id,
            phase="pushing",
            state="active",
            message="Pushing verified HEAD atomically",
        )
        expectation = audit_expectation or self.audit_ref_expectation
        try:
            audit_ref, audit_expected_sha = expectation(
                worktree=worktree,
                deploy_sha=deploy_sha,
                log=log,
                pulse=ownership_pulse,
            )
        except CancellationRequested:
            raise
        except MergetrainError as exc:
            event(
                conn,
                lease_token=lease_token,
                job_id=event_job_id,
                phase="pushing",
                state="error",
                message="Deploy audit preflight failed",
                detail=type(exc).__name__,
            )
            raise

        state.push_status = "pending"
        push = push_with_marker or self.push_with_marker
        try:
            push(
                conn,
                job_ids=job_ids,
                deploy_sha=deploy_sha,
                lease_token=lease_token,
                worktree=worktree,
                log=log,
                pulse=ownership_pulse,
                audit_ref=audit_ref,
                audit_expected_sha=audit_expected_sha,
            )
        except CommandFailed as exc:
            event(
                conn,
                lease_token=lease_token,
                job_id=event_job_id,
                phase="pushing",
                state="error",
                message="Atomic push failed",
                detail=f"exit_code={exc.returncode}",
            )
            if is_push_rejection(exc.stderr):
                state.push_status = "failed"
                clear = clear_rejected or self.clear_rejected_push
                clear(
                    conn,
                    job_ids=job_ids,
                    lease_token=lease_token,
                    log=log,
                )
                raise PushRejected(
                    "remote definitively rejected the push (policy, permission, "
                    "or non-fast-forward); no ref update landed: "
                    f"{exc.stderr.strip() or exc}"
                ) from exc
            raise AmbiguousPush(
                "atomic push failed after the write-ahead marker was "
                f"recorded (exit {exc.returncode}); outcome ambiguous — "
                f"parked for reconcile: {exc.stderr.strip() or exc}"
            ) from exc

        state.push_status = "succeeded"
        event(
            conn,
            lease_token=lease_token,
            job_id=event_job_id,
            phase="pushing",
            state="success",
            message="Atomic push completed",
        )
        if not self.config.deploy.verify:
            state.verify_status = "not_configured"
            event(
                conn,
                lease_token=lease_token,
                job_id=event_job_id,
                phase="verifying",
                state="success",
                message="No post-push verification configured",
            )
            return

        verify = run_verify_hooks
        if verify is None:
            raise MergetrainError("post-push verify runner is not configured")
        try:
            event(
                conn,
                lease_token=lease_token,
                job_id=event_job_id,
                phase="verifying",
                state="active",
                message="Running post-push verification",
            )
            verify(worktree=worktree, log=log, pulse=ownership_pulse)
            state.verify_status = "succeeded"
            event(
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
            event(
                conn,
                lease_token=lease_token,
                job_id=event_job_id,
                phase="verifying",
                state="warning",
                message="Post-push verification needs attention",
                detail=f"exit_code={exc.returncode}",
            )
