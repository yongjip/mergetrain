"""Validation identity construction and exact validated-gate reuse decisions."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import IO

from .command_runner import Pulse, run_command
from .config import MergetrainConfig
from .errors import CommandFailed, MergetrainError, QueueBusy
from .gate_runner import GateRunner
from .git_ops import git_ref_exists, git_tree_sha
from .models import Job
from .reuse import (
    ReuseCheck,
    ReuseDecision,
    gate_policy_sha,
    train_identity_sha,
    validation_age_minutes,
)


def unauthorized_reuse_decision(jobs: Sequence[Job]) -> ReuseDecision:
    validation_shas = {job.validation_sha for job in jobs if job.validation_sha}
    validation_sha = next(iter(validation_shas)) if len(validation_shas) == 1 else ""
    return ReuseDecision(
        authorized=False,
        eligible=False,
        action="rerun",
        validation_sha=validation_sha,
        reasons=("validated gate reuse is not authorized",),
        checks=(
            ReuseCheck(
                code="authorization",
                status="mismatch",
                expected=True,
                actual=False,
                detail="validated gate reuse is not authorized",
            ),
        ),
    )


class ValidationReuse:
    """Own validation identity and the fail-closed reuse decision table."""

    def __init__(self, config: MergetrainConfig, gates: GateRunner):
        self.config = config
        self.gates = gates

    def identity_fields(
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
            "validation_environment_sha": self.gates.environment_fingerprint(
                worktree=worktree, log=log, pulse=pulse
            ),
            "validation_train_sha": train_identity_sha(
                jobs,
                train_id=train_id,
                train_size=len(jobs),
                validated_heads=validated_heads,
            ),
        }

    def decide(
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
            return unauthorized_reuse_decision(jobs)

        reasons: list[str] = []
        checks: list[ReuseCheck] = [
            ReuseCheck(
                code="authorization",
                status="match",
                expected=True,
                actual=True,
                detail="reuse was explicitly authorized",
            )
        ]

        train_ids = sorted({job.train_id for job in jobs if job.train_id})
        membership_matches = bool(jobs) and len(train_ids) == 1
        checks.append(
            ReuseCheck(
                code="train_membership",
                status="match" if membership_matches else "mismatch",
                expected="one non-empty train id",
                actual=train_ids,
                detail=(
                    "train membership is complete"
                    if membership_matches
                    else "train membership is incomplete or mixed"
                ),
            )
        )
        if not membership_matches:
            reasons.append("train membership is incomplete or mixed")

        train_sizes = sorted({job.train_size for job in jobs})
        size_matches = bool(jobs and len(train_sizes) == 1 and jobs[0].train_size == len(jobs))
        checks.append(
            ReuseCheck(
                code="train_size",
                status="match" if size_matches else "mismatch",
                expected=len(jobs),
                actual=train_sizes,
                detail=(
                    "validated train size matches membership"
                    if size_matches
                    else "train size does not match its validated membership"
                ),
            )
        )
        if not size_matches:
            reasons.append("train size does not match its validated membership")

        validation_sha_matches = len(validation_shas) == 1
        checks.append(
            ReuseCheck(
                code="validation_commit",
                status="match" if validation_sha_matches else "mismatch",
                expected="one shared validation SHA",
                actual=sorted(validation_shas),
                detail=(
                    "validated jobs share one validation SHA"
                    if validation_sha_matches
                    else "validated jobs do not share one validation SHA"
                ),
            )
        )
        if not validation_sha_matches:
            reasons.append("validated jobs do not share one validation SHA")

        validation_bases = sorted(
            {job.validation_base_sha for job in jobs if job.validation_base_sha}
        )
        base_matches = bool(
            jobs
            and len(validation_bases) == 1
            and jobs[0].validation_base_sha == integration_base_sha
        )
        checks.append(
            ReuseCheck(
                code="integration_base",
                status="match" if base_matches else "mismatch",
                expected=integration_base_sha,
                actual=validation_bases,
                detail=(
                    "integration ref still matches validation"
                    if base_matches
                    else "integration ref moved since validation"
                ),
            )
        )
        if not base_matches:
            reasons.append("integration ref moved since validation")

        current_train_identity = train_identity_sha(jobs) if jobs else ""
        recorded_train_identity = jobs[0].validation_train_sha if jobs else ""
        train_identity_matches = bool(
            jobs and recorded_train_identity and current_train_identity == recorded_train_identity
        )
        checks.append(
            ReuseCheck(
                code="train_identity",
                status="match" if train_identity_matches else "mismatch",
                expected=recorded_train_identity,
                actual=current_train_identity,
                detail=(
                    "train membership identity matches validation"
                    if train_identity_matches
                    else "train membership identity changed since validation"
                ),
            )
        )
        if jobs and current_train_identity != recorded_train_identity:
            reasons.append("train membership identity changed since validation")

        current_policy_sha = gate_policy_sha(self.config)
        recorded_policy_sha = jobs[0].validation_gate_policy_sha if jobs else ""
        policy_matches = bool(
            jobs and recorded_policy_sha and current_policy_sha == recorded_policy_sha
        )
        checks.append(
            ReuseCheck(
                code="gate_policy",
                status="match" if policy_matches else "mismatch",
                expected=recorded_policy_sha,
                actual=current_policy_sha,
                detail=(
                    "gate and fingerprint policy matches validation"
                    if policy_matches
                    else "gate or fingerprint policy changed since validation"
                ),
            )
        )
        if jobs and current_policy_sha != recorded_policy_sha:
            reasons.append("gate or fingerprint policy changed since validation")

        age_minutes = validation_age_minutes(jobs[0].validated_at) if jobs else float("inf")
        age_matches = age_minutes <= self.config.deploy.reuse.max_age_minutes
        checks.append(
            ReuseCheck(
                code="validation_age",
                status="match" if age_matches else "mismatch",
                expected={"maximum_minutes": self.config.deploy.reuse.max_age_minutes},
                actual={
                    "age_minutes": (round(age_minutes, 3) if age_minutes != float("inf") else None)
                },
                detail=(
                    "validation is within the configured reuse age"
                    if age_matches
                    else "validation is older than the configured reuse age"
                ),
            )
        )
        if jobs and not age_matches:
            reasons.append("validation is older than the configured reuse age")

        required_fields = (
            "validation_tree_sha",
            "validation_gate_policy_sha",
            "validation_environment_sha",
            "validation_train_sha",
        )
        for field in required_fields:
            all_values = {getattr(job, field) for job in jobs}
            values = {value for value in all_values if value}
            shared = len(values) == 1 and len(values) == len(all_values)
            detail = (
                f"validated jobs share {field}"
                if shared
                else f"validated jobs lack one shared {field}"
            )
            checks.append(
                ReuseCheck(
                    code=f"shared_{field}",
                    status="match" if shared else "mismatch",
                    expected="one shared non-empty SHA",
                    actual=sorted(values),
                    detail=detail,
                )
            )
            if not shared:
                reasons.append(detail)

        commit_exists = bool(validation_sha and git_ref_exists(worktree, validation_sha))
        checks.append(
            ReuseCheck(
                code="validation_commit_available",
                status="match" if commit_exists else "mismatch",
                expected=True,
                actual=commit_exists,
                detail=(
                    "validation commit exists in the local repository"
                    if commit_exists
                    else "validation commit is missing from the local repository"
                ),
            )
        )
        if validation_sha and not commit_exists:
            reasons.append("validation commit is missing from the local repository")
        elif validation_sha and jobs:
            current_tree_sha = git_tree_sha(worktree, validation_sha)
            recorded_tree_sha = jobs[0].validation_tree_sha
            tree_matches = current_tree_sha == recorded_tree_sha
            checks.append(
                ReuseCheck(
                    code="validation_tree",
                    status="match" if tree_matches else "mismatch",
                    expected=recorded_tree_sha,
                    actual=current_tree_sha,
                    detail=(
                        "validation commit tree matches recorded identity"
                        if tree_matches
                        else "validation commit tree does not match its recorded identity"
                    ),
                )
            )
            if not tree_matches:
                reasons.append("validation commit tree does not match its recorded identity")

        environment_check_recorded = False
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
                    current_environment_sha = self.gates.environment_fingerprint(
                        worktree=worktree, log=log, pulse=pulse
                    )
                except QueueBusy:
                    raise
                except (CommandFailed, MergetrainError):
                    environment_check_recorded = True
                    checks.append(
                        ReuseCheck(
                            code="environment",
                            status="mismatch",
                            expected=jobs[0].validation_environment_sha,
                            actual="unavailable",
                            detail=("required environment fingerprint could not be reproduced"),
                        )
                    )
                    reasons.append("required environment fingerprint could not be reproduced")
                else:
                    environment_check_recorded = True
                    environment_matches = (
                        current_environment_sha == jobs[0].validation_environment_sha
                    )
                    checks.append(
                        ReuseCheck(
                            code="environment",
                            status="match" if environment_matches else "mismatch",
                            expected=jobs[0].validation_environment_sha,
                            actual=current_environment_sha,
                            detail=(
                                "environment fingerprint matches validation"
                                if environment_matches
                                else "environment or toolchain fingerprint changed"
                            ),
                        )
                    )
                    if not environment_matches:
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
                    run_command(
                        ["git", "clean", "-fdx"],
                        cwd=worktree,
                        log=log,
                        pulse=pulse,
                        pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                        timeout_seconds=self.config.queue.command_timeout_seconds,
                    )

        if not environment_check_recorded:
            checks.append(
                ReuseCheck(
                    code="environment",
                    status="not_evaluated",
                    expected=(jobs[0].validation_environment_sha if jobs else ""),
                    actual=None,
                    detail=(
                        "environment check was skipped because an earlier "
                        "identity check did not match"
                    ),
                )
            )

        eligible = not reasons
        action = "reuse" if eligible else self.config.deploy.reuse.on_mismatch
        changed_paths: tuple[str, ...] | None = ()
        if eligible and any(gate.paths for gate in self.config.gates):
            changed_paths = self.gates.changed_paths(
                worktree=worktree,
                base_ref=integration_base_sha,
                head_ref=validation_sha,
                log=log,
                pulse=pulse,
            )
        return ReuseDecision(
            authorized=True,
            eligible=eligible,
            action=action,
            validation_sha=validation_sha,
            reused_validation_sha=validation_sha if eligible else "",
            reasons=tuple(reasons),
            checks=tuple(checks),
            changed_paths=changed_paths,
        )
