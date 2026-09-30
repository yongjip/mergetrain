"""Validation identity construction and exact validated-gate reuse decisions."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from .command_runner import Pulse, command_limits, run_command
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
        log: IO[str],
        pulse: Pulse | None,
    ) -> ReuseDecision:
        """Decide reuse for a train whose reuse the config already authorizes."""

        validation_shas = {job.validation_sha for job in jobs if job.validation_sha}
        validation_sha = next(iter(validation_shas)) if len(validation_shas) == 1 else ""
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

        def record(
            code: str,
            matches: bool,
            *,
            expected: Any,
            actual: Any,
            match: str = "",
            mismatch: str,
            blocks: bool | None = None,
        ) -> None:
            """Record one check; its mismatch text is a reason when ``blocks``.

            ``blocks`` defaults to whether the check mismatched.
            """

            checks.append(
                ReuseCheck(
                    code=code,
                    status="match" if matches else "mismatch",
                    expected=expected,
                    actual=actual,
                    detail=match if matches else mismatch,
                )
            )
            if blocks is None:
                blocks = not matches
            if blocks:
                reasons.append(mismatch)

        train_ids = sorted({job.train_id for job in jobs if job.train_id})
        record(
            "train_membership",
            bool(jobs) and len(train_ids) == 1,
            expected="one non-empty train id",
            actual=train_ids,
            match="train membership is complete",
            mismatch="train membership is incomplete or mixed",
        )

        train_sizes = sorted({job.train_size for job in jobs})
        record(
            "train_size",
            bool(jobs and len(train_sizes) == 1 and jobs[0].train_size == len(jobs)),
            expected=len(jobs),
            actual=train_sizes,
            match="validated train size matches membership",
            mismatch="train size does not match its validated membership",
        )

        record(
            "validation_commit",
            len(validation_shas) == 1,
            expected="one shared validation SHA",
            actual=sorted(validation_shas),
            match="validated jobs share one validation SHA",
            mismatch="validated jobs do not share one validation SHA",
        )

        validation_bases = sorted(
            {job.validation_base_sha for job in jobs if job.validation_base_sha}
        )
        record(
            "integration_base",
            bool(
                jobs
                and len(validation_bases) == 1
                and jobs[0].validation_base_sha == integration_base_sha
            ),
            expected=integration_base_sha,
            actual=validation_bases,
            match="integration ref still matches validation",
            mismatch="integration ref moved since validation",
        )

        # A train with no jobs mismatches the recorded identity, policy, and
        # age, but only a train with jobs gives those mismatches as reasons.
        current_train_identity = train_identity_sha(jobs) if jobs else ""
        recorded_train_identity = jobs[0].validation_train_sha if jobs else ""
        record(
            "train_identity",
            bool(
                jobs
                and recorded_train_identity
                and current_train_identity == recorded_train_identity
            ),
            expected=recorded_train_identity,
            actual=current_train_identity,
            match="train membership identity matches validation",
            mismatch="train membership identity changed since validation",
            blocks=bool(jobs) and current_train_identity != recorded_train_identity,
        )

        current_policy_sha = gate_policy_sha(self.config)
        recorded_policy_sha = jobs[0].validation_gate_policy_sha if jobs else ""
        record(
            "gate_policy",
            bool(jobs and recorded_policy_sha and current_policy_sha == recorded_policy_sha),
            expected=recorded_policy_sha,
            actual=current_policy_sha,
            match="gate and fingerprint policy matches validation",
            mismatch="gate or fingerprint policy changed since validation",
            blocks=bool(jobs) and current_policy_sha != recorded_policy_sha,
        )

        age_minutes = validation_age_minutes(jobs[0].validated_at) if jobs else float("inf")
        age_matches = age_minutes <= self.config.deploy.reuse.max_age_minutes
        record(
            "validation_age",
            age_matches,
            expected={"maximum_minutes": self.config.deploy.reuse.max_age_minutes},
            actual={
                "age_minutes": (round(age_minutes, 3) if age_minutes != float("inf") else None)
            },
            match="validation is within the configured reuse age",
            mismatch="validation is older than the configured reuse age",
            blocks=bool(jobs) and not age_matches,
        )

        required_fields = (
            "validation_tree_sha",
            "validation_gate_policy_sha",
            "validation_environment_sha",
            "validation_train_sha",
        )
        for field in required_fields:
            all_values = {getattr(job, field) for job in jobs}
            values = {value for value in all_values if value}
            record(
                f"shared_{field}",
                len(values) == 1 and len(values) == len(all_values),
                expected="one shared non-empty SHA",
                actual=sorted(values),
                match=f"validated jobs share {field}",
                mismatch=f"validated jobs lack one shared {field}",
            )

        # Without one validation SHA, validation_commit already gave the reason.
        commit_exists = bool(validation_sha and git_ref_exists(worktree, validation_sha))
        record(
            "validation_commit_available",
            commit_exists,
            expected=True,
            actual=commit_exists,
            match="validation commit exists in the local repository",
            mismatch="validation commit is missing from the local repository",
            blocks=bool(validation_sha) and not commit_exists,
        )
        if commit_exists and jobs:
            current_tree_sha = git_tree_sha(worktree, validation_sha)
            recorded_tree_sha = jobs[0].validation_tree_sha
            record(
                "validation_tree",
                current_tree_sha == recorded_tree_sha,
                expected=recorded_tree_sha,
                actual=current_tree_sha,
                match="validation commit tree matches recorded identity",
                mismatch="validation commit tree does not match its recorded identity",
            )

        environment_check_recorded = False
        if not reasons and jobs:
            reset = run_command(
                ["git", "reset", "--hard", validation_sha],
                cwd=worktree,
                log=log,
                check=False,
                pulse=pulse,
                **command_limits(self.config),
            )
            if reset.returncode != 0:
                reasons.append("validation commit could not be restored for fingerprinting")
            else:
                recorded_environment_sha = jobs[0].validation_environment_sha
                try:
                    current_environment_sha = self.gates.environment_fingerprint(
                        worktree=worktree, log=log, pulse=pulse
                    )
                except QueueBusy:
                    raise
                except (CommandFailed, MergetrainError):
                    environment_check_recorded = True
                    record(
                        "environment",
                        False,
                        expected=recorded_environment_sha,
                        actual="unavailable",
                        mismatch="required environment fingerprint could not be reproduced",
                    )
                else:
                    environment_check_recorded = True
                    record(
                        "environment",
                        current_environment_sha == recorded_environment_sha,
                        expected=recorded_environment_sha,
                        actual=current_environment_sha,
                        match="environment fingerprint matches validation",
                        mismatch="environment or toolchain fingerprint changed",
                    )
                finally:
                    run_command(
                        ["git", "reset", "--hard", integration_base_sha],
                        cwd=worktree,
                        log=log,
                        pulse=pulse,
                        **command_limits(self.config),
                    )
                    run_command(
                        ["git", "clean", "-fdx"],
                        cwd=worktree,
                        log=log,
                        pulse=pulse,
                        **command_limits(self.config),
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
