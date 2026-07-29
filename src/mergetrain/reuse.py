"""Safety identity primitives for opt-in validated-gate reuse."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from statistics import median
from typing import Any

from .config import MergetrainConfig
from .models import Job
from .path_gates import any_path_matches

REUSE_ESTIMATE_SAMPLE_LIMIT = 20


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def gate_policy_sha(config: MergetrainConfig) -> str:
    """Hash the semantic inputs that define the pre-push gate policy."""

    return _sha256_json(
        {
            "version": 2,
            "project": config.project.name,
            "integration_ref": config.git.integration_ref,
            "gate_parallelism": {
                "max_workers": config.gate_parallelism.max_workers,
                "timeout_seconds": config.gate_parallelism.timeout_seconds,
            },
            "built_in_gates": [
                {
                    "name": "diff-check",
                    "run": "git diff --check ${integration_ref}..HEAD",
                    "always_rerun_on_deploy": False,
                }
            ],
            "gates": [
                {
                    "name": gate.name,
                    "run": gate.run,
                    "always_rerun_on_deploy": gate.always_rerun_on_deploy,
                    "paths": list(gate.paths),
                    "parallel_group": gate.parallel_group,
                    "needs": list(gate.needs),
                    "workers": gate.workers,
                    "timeout_seconds": gate.timeout_seconds,
                }
                for gate in config.gates
            ],
            "environment_fingerprints": [
                {"name": item.name, "run": item.run}
                for item in config.deploy.reuse.fingerprints
            ],
        }
    )


def environment_sha(values: Iterable[tuple[str, str]]) -> str:
    """Hash opaque adapter-provided fingerprint values without persisting them."""

    return _sha256_json(
        [{"name": name, "value": value} for name, value in values]
    )


def train_identity_sha(
    jobs: Iterable[Job],
    *,
    train_id: str | None = None,
    train_size: int | None = None,
    validated_heads: dict[int, str] | None = None,
) -> str:
    ordered = list(jobs)
    resolved_train_id = train_id if train_id is not None else (
        ordered[0].train_id if ordered else ""
    )
    resolved_train_size = train_size if train_size is not None else (
        ordered[0].train_size if ordered else 0
    )
    return _sha256_json(
        {
            "version": 1,
            "train_id": resolved_train_id,
            "train_size": resolved_train_size,
            "members": [
                {
                    "job_id": job.id,
                    "task": job.task,
                    "branch": job.branch,
                    "validated_head_sha": (
                        validated_heads[job.id]
                        if validated_heads is not None
                        else job.validated_head_sha
                    ),
                }
                for job in ordered
            ],
        }
    )


def validation_age_minutes(validated_at: str, *, now: datetime | None = None) -> float:
    if not validated_at:
        return float("inf")
    try:
        parsed = datetime.fromisoformat(validated_at.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age = (current - parsed.astimezone(timezone.utc)).total_seconds() / 60
    return age if age >= 0 else float("inf")


@dataclass(frozen=True, slots=True)
class ReuseCheck:
    code: str
    status: str
    expected: Any
    actual: Any
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReuseDecision:
    authorized: bool
    eligible: bool
    action: str
    validation_sha: str
    reused_validation_sha: str = ""
    reasons: tuple[str, ...] = ()
    checks: tuple[ReuseCheck, ...] = ()
    changed_paths: tuple[str, ...] | None = None
    evaluation: str = "exact"

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation": self.evaluation,
            "authorized": self.authorized,
            "eligible": self.eligible,
            "action": self.action,
            "validation_sha": self.validation_sha,
            "reused_validation_sha": self.reused_validation_sha,
            "reasons": list(self.reasons),
            "identity_checks": [check.to_dict() for check in self.checks],
        }


def _reuse_gate_plan(
    config: MergetrainConfig,
    decision: ReuseDecision | None,
) -> list[dict[str, Any]]:
    exact = decision is not None
    authorized = decision.authorized if decision is not None else config.deploy.reuse.enabled
    eligible = decision.eligible if decision is not None else None
    changed_paths = decision.changed_paths if decision is not None else None
    plan: list[dict[str, Any]] = []

    gates: list[dict[str, Any]] = [
        {
            "name": "diff-check",
            "paths": (),
            "always_rerun": False,
            "parallel_group": "",
            "workers": 1,
        },
        *[
            {
                "name": gate.name,
                "paths": gate.paths,
                "always_rerun": gate.always_rerun_on_deploy,
                "parallel_group": gate.parallel_group,
                "workers": gate.workers,
            }
            for gate in config.gates
        ],
    ]
    authorization_only = bool(
        exact
        and decision is not None
        and not decision.authorized
        and decision.reasons == ("validated gate reuse is not authorized",)
    )
    for gate in gates:
        paths = tuple(gate["paths"])
        always_rerun = bool(gate["always_rerun"])
        if eligible:
            if paths and changed_paths is not None and not any_path_matches(
                paths, changed_paths
            ):
                action = "skip"
                reason = "no_matching_paths"
            elif always_rerun:
                action = "rerun"
                reason = "always_rerun_on_deploy"
            elif paths and changed_paths is None:
                action = "rerun"
                reason = "path_discovery_unavailable"
            else:
                action = "reuse"
                reason = "exact_identity_match"
        elif not exact or authorization_only:
            if always_rerun:
                action = "rerun"
                reason = "always_rerun_on_deploy"
            elif paths:
                action = "conditional_reuse"
                reason = (
                    "preview_required"
                    if authorized
                    else "authorization_and_preview_required"
                )
            else:
                action = "potential_reuse"
                reason = (
                    "preview_required"
                    if authorized
                    else "authorization_required"
                )
        else:
            action = "conditional_run" if paths else "rerun"
            reason = "identity_mismatch"
        plan.append(
            {
                "name": gate["name"],
                "scope": {
                    "kind": "paths" if paths else "all_changes",
                    "paths": list(paths),
                },
                "always_rerun_on_deploy": always_rerun,
                "parallel_group": gate["parallel_group"],
                "workers": gate["workers"],
                "action": action,
                "reason_code": reason,
            }
        )
    return plan


def reuse_explanation(
    config: MergetrainConfig,
    jobs: Iterable[Job],
    *,
    decision: ReuseDecision | None,
    gate_runs: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build the shared CLI/dashboard reuse explanation.

    Timing is deliberately advisory and can never change ``authorized`` or
    ``eligible``. It sums per-gate medians rather than claiming wall-clock
    precision, and names incomplete historical coverage explicitly.
    """

    members = list(jobs)
    plan = _reuse_gate_plan(config, decision)
    samples: dict[str, list[float]] = {}
    for run in gate_runs:
        duration = run.get("duration_seconds")
        if duration is None or str(run.get("state")) not in {"success", "reused"}:
            continue
        name = str(run.get("name", ""))
        if not name:
            continue
        values = samples.setdefault(name, [])
        values.append(float(duration))
        if len(values) > REUSE_ESTIMATE_SAMPLE_LIMIT:
            del values[0]

    potential_actions = {"reuse", "potential_reuse", "conditional_reuse"}
    candidate_gates = [
        gate for gate in plan if gate["action"] in potential_actions
    ]
    timed_candidates = 0
    estimated_seconds = 0.0
    per_gate_samples: list[int] = []
    for gate in plan:
        values = samples.get(str(gate["name"]), [])
        median_seconds = round(median(values), 3) if values else None
        timing = {
            "sample_count": len(values),
            "median_seconds": median_seconds,
        }
        gate["timing"] = timing
        if gate in candidate_gates and values:
            timed_candidates += 1
            per_gate_samples.append(len(values))
            assert median_seconds is not None
            estimated_seconds += float(median_seconds)

    candidate_count = len(candidate_gates)
    coverage = (
        round(timed_candidates / candidate_count, 4)
        if candidate_count
        else 1.0
    )
    minimum_samples = min(per_gate_samples) if per_gate_samples else 0
    if not candidate_count:
        confidence = "none"
        estimate: float | None = 0.0
    elif not timed_candidates:
        confidence = "none"
        estimate = None
    elif coverage < 1.0 or minimum_samples < 3:
        confidence = "low"
        estimate = round(estimated_seconds, 3)
    elif minimum_samples < 10:
        confidence = "medium"
        estimate = round(estimated_seconds, 3)
    else:
        confidence = "high"
        estimate = round(estimated_seconds, 3)

    validation_shas = {
        job.validation_sha for job in members if job.validation_sha
    }
    validation_sha = (
        next(iter(validation_shas)) if len(validation_shas) == 1 else ""
    )
    if decision is None:
        authorized = config.deploy.reuse.enabled
        eligible: bool | None = None
        action = "preview_required" if validation_sha else "not_available"
        reused_validation_sha = ""
        reasons = (
            ["run an exact deploy preview to evaluate reuse identity"]
            if validation_sha
            else ["no validated train is selected"]
        )
        checks = [
            ReuseCheck(
                code="authorization",
                status="match" if authorized else "mismatch",
                expected=True,
                actual=authorized,
                detail=(
                    "reuse is enabled by project config"
                    if authorized
                    else "reuse requires explicit config or CLI authorization"
                ),
            ).to_dict()
        ]
        evaluation = "not_evaluated"
    else:
        authorized = decision.authorized
        eligible = decision.eligible
        action = decision.action
        validation_sha = decision.validation_sha
        reused_validation_sha = decision.reused_validation_sha
        reasons = list(decision.reasons)
        checks = [check.to_dict() for check in decision.checks]
        evaluation = decision.evaluation

    estimate_mode = (
        "exact"
        if decision is not None and decision.eligible
        else (
            "potential"
            if decision is None
            or (
                decision is not None
                and not decision.authorized
                and decision.reasons
                == ("validated gate reuse is not authorized",)
            )
            else "unavailable"
        )
    )
    if estimate_mode == "unavailable":
        estimate = 0.0
        confidence = "none"

    return {
        "evaluation": evaluation,
        "authorized": authorized,
        "eligible": eligible,
        "action": action,
        "validation_sha": validation_sha,
        "reused_validation_sha": reused_validation_sha,
        "reasons": reasons,
        "identity_checks": checks,
        "gates": plan,
        "estimated_savings": {
            "seconds": estimate,
            "mode": estimate_mode,
            "basis": "sum_of_per_gate_medians",
            "sample_count": minimum_samples,
            "candidate_gate_count": candidate_count,
            "timed_gate_count": timed_candidates,
            "coverage": coverage,
            "confidence": confidence,
            "sample_limit_per_gate": REUSE_ESTIMATE_SAMPLE_LIMIT,
            "authorizes_reuse": False,
        },
    }
