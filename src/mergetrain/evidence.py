"""Evidence-backed product metrics derived from local queue history.

This module deliberately keeps counterfactual estimates and incomplete event
coverage visible.  It never turns missing runner history into a successful run
or claims that a recovery operation happened when the queue did not record it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil
from statistics import mean, median
from typing import Any

from .models import ALL_STATUSES, Job, RecoveryOperationEvent, RunEvent

_CLAIMED_JOBS = re.compile(r"\bclaimed\s+(\d+)\s+job(?:\(s\)|s)?\b", re.IGNORECASE)
_RUN_MODES = ("validate", "deploy")
_RUN_OUTCOMES = ("succeeded", "partial", "failed", "canceled", "incomplete")
_HISTORY_STATUSES = (
    "queued",
    "in_progress",
    "blocked",
    "failed",
    "validated",
    "needs_reconcile",
    "deployed",
    "canceled",
    "unknown",
)
_TERMINAL_HISTORY_STATUSES = {"deployed", "blocked", "failed", "canceled"}
_NOT_LANDED_REASONS = (
    "queued",
    "running",
    "awaiting_deploy",
    "pending_reconcile",
    "semantic_conflict",
    "merge_conflict",
    "push_rejected",
    "deploy_authorization_changed",
    "source_identity_mismatch",
    "validated_reuse_mismatch",
    "merge_blocked",
    "gate_failed",
    "command_timeout",
    "push_failed",
    "runner_failed",
    "superseded",
    "canceled",
    "unknown",
)
_RECOVERY_OPERATIONS = ("reconcile", "recover")
_RECOVERY_OUTCOMES = (
    "success",
    "conflict",
    "lock_held",
    "remote_unreachable",
    "error",
    "incomplete",
)


@dataclass(frozen=True, slots=True)
class RunAttempt:
    """One runner claim reconstructed from retained local events."""

    claim_token: str
    mode: str
    outcome: str
    job_count: int | None


@dataclass(frozen=True, slots=True)
class RecoveryAttempt:
    """One operator recovery invocation reconstructed from its append-only ledger."""

    invocation_id: str
    operation: str
    applied: bool
    outcome: str


def history_groups(jobs: Sequence[Job]) -> dict[str, list[Job]]:
    """Group durable rows with the same compatibility semantics as history."""

    grouped: dict[str, list[Job]] = {}
    for job in jobs:
        key = f"train:{job.train_id}" if job.train_id else f"job:{job.id}"
        grouped.setdefault(key, []).append(job)
    return grouped


def history_status(jobs: Sequence[Job]) -> str:
    statuses = {job.status for job in jobs}
    for status in (
        "needs_reconcile",
        "in_progress",
        "failed",
        "blocked",
        "queued",
        "validated",
        "canceled",
        "deployed",
    ):
        if status in statuses:
            return status
    return "unknown"


def run_mode(events: Sequence[RunEvent]) -> str:
    """Classify a retained run, preferring its durable claiming detail."""

    phases = {event.phase for event in events}
    messages = [event.message.lower() for event in events if event.phase == "claiming"]
    details = {event.detail for event in events if event.phase == "claiming"}
    return (
        "deploy"
        if "mode=deploy" in details
        or {"pushing", "verifying"} & phases
        or any("deploy runner" in message for message in messages)
        else "validate"
    )


def _claimed_job_count(events: Sequence[RunEvent]) -> int | None:
    for event in events:
        if event.phase != "claiming" or event.state != "active":
            continue
        matched = _CLAIMED_JOBS.search(event.message)
        if matched is not None:
            return int(matched.group(1))
    return None


def run_attempts(events: Sequence[RunEvent]) -> list[RunAttempt]:
    """Reconstruct run outcomes without exposing lease/claim tokens publicly."""

    grouped: dict[str, list[RunEvent]] = {}
    for event in events:
        if event.claim_token:
            grouped.setdefault(event.claim_token, []).append(event)

    attempts: list[RunAttempt] = []
    for token, token_events in grouped.items():
        ordered = sorted(token_events, key=lambda event: event.id)
        mode = run_mode(ordered)
        success_phase = "complete" if mode == "deploy" else "ready"
        succeeded = any(
            event.phase == success_phase and event.state in {"success", "warning"}
            for event in ordered
        )
        failed = any(event.phase in {"blocked", "failed"} for event in ordered)
        canceled = any(event.phase == "canceled" for event in ordered)
        if succeeded and (failed or canceled):
            outcome = "partial"
        elif succeeded:
            outcome = "succeeded"
        elif failed:
            outcome = "failed"
        elif canceled:
            outcome = "canceled"
        else:
            outcome = "incomplete"
        attempts.append(
            RunAttempt(
                claim_token=token,
                mode=mode,
                outcome=outcome,
                job_count=_claimed_job_count(ordered),
            )
        )
    return attempts


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _nearest_rank(values: Sequence[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, ceil(percentile * len(ordered)) - 1)]


def _count_summary(values: Sequence[int]) -> dict[str, Any]:
    return {
        "samples": len(values),
        "median": round(float(median(values)), 3) if values else None,
        "p95": _nearest_rank(values, 0.95),
        "average": round(float(mean(values)), 3) if values else None,
        "max": max(values) if values else None,
    }


def _run_outcome_summary(attempts: Sequence[RunAttempt], *, mode: str) -> dict[str, Any]:
    selected = [attempt for attempt in attempts if attempt.mode == mode]
    counts = Counter(attempt.outcome for attempt in selected)
    conclusive = counts["succeeded"] + counts["partial"] + counts["failed"]
    terminal = conclusive + counts["canceled"]
    runs_with_failure = counts["partial"] + counts["failed"]
    return {
        "source": "retained_run_events",
        "attempted": len(selected),
        "terminal": terminal,
        "failure_rate_denominator": conclusive,
        **{outcome: counts[outcome] for outcome in _RUN_OUTCOMES},
        "runs_with_failure": runs_with_failure,
        "failure_rate": _rate(runs_with_failure, conclusive),
    }


def _not_landed_reason(jobs: Sequence[Job]) -> str | None:
    status = history_status(jobs)
    if status == "deployed":
        return None
    if status == "queued":
        return "queued"
    if status == "in_progress":
        return "running"
    if status == "validated":
        return "awaiting_deploy"
    if status == "needs_reconcile":
        return "pending_reconcile"

    relevant = [job for job in jobs if job.status == status]
    lowered = [job.note.lower() for job in relevant]
    if status == "canceled":
        if any("superseded by" in note for note in lowered):
            return "superseded"
        return "canceled"
    if status == "blocked":
        if any(job.push_status == "failed" for job in relevant):
            return "push_rejected"
        if any(
            "approval_destination_changed" in note
            or "approval_execution_policy_changed" in note
            or "deploy_plan_changed" in note
            for note in lowered
        ):
            return "deploy_authorization_changed"
        if any(
            job.conflict_with or "semantic conflict" in note
            for job, note in zip(relevant, lowered, strict=True)
        ):
            return "semantic_conflict"
        if any("conflict" in note for note in lowered):
            return "merge_conflict"
        if any("head changed" in note or "identity" in note for note in lowered):
            return "source_identity_mismatch"
        if any("reuse" in note or "fingerprint" in note for note in lowered):
            return "validated_reuse_mismatch"
        return "merge_blocked"
    if status == "failed":
        if any(job.push_status == "failed" for job in relevant):
            return "push_failed"
        if any("timed out" in note for note in lowered):
            return "command_timeout"
        if any("gate" in note or "command failed" in note for note in lowered):
            return "gate_failed"
        return "runner_failed"
    return "unknown"


def _validated_train_summary(jobs: Sequence[Job]) -> dict[str, Any]:
    by_train: dict[str, list[Job]] = {}
    for job in jobs:
        if job.train_id:
            by_train.setdefault(job.train_id, []).append(job)
    validated = [
        members for members in by_train.values() if any(member.validated_at for member in members)
    ]
    statuses = Counter(history_status(members) for members in validated)
    deployed = statuses["deployed"]
    terminal_without_deploy = sum(statuses[status] for status in ("blocked", "failed", "canceled"))
    pending = len(validated) - deployed - terminal_without_deploy
    superseded = sum(
        history_status(members) == "canceled"
        and any("superseded by" in member.note.lower() for member in members)
        for members in validated
    )
    resolved = deployed + terminal_without_deploy
    return {
        "source": "queue_history",
        "total": len(validated),
        "deployed": deployed,
        "pending": pending,
        "terminal_without_deploy": terminal_without_deploy,
        "superseded": superseded,
        "deployment_rate": _rate(deployed, len(validated)),
        "resolved_deployment_rate": _rate(deployed, resolved),
    }


def _outcome_summary(
    jobs: Sequence[Job],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    groups = list(history_groups(jobs).values())
    train_statuses = Counter(history_status(members) for members in groups)
    job_statuses = Counter(job.status for job in jobs)
    reasons: Counter[str] = Counter()
    for members in groups:
        reason = _not_landed_reason(members)
        if reason is not None:
            reasons[reason] += 1

    landed = train_statuses["deployed"]
    terminal = sum(train_statuses[status] for status in _TERMINAL_HISTORY_STATUSES)
    merge_conflicts = reasons["merge_conflict"]
    semantic_conflicts = reasons["semantic_conflict"]
    train_additions = {
        "canceled": train_statuses["canceled"],
        "open": sum(
            train_statuses[status]
            for status in ("queued", "in_progress", "validated", "needs_reconcile")
        ),
        "terminal": terminal,
        "terminal_land_rate": _rate(landed, terminal),
        "not_landed": len(groups) - landed,
        "status_counts": {status: train_statuses[status] for status in _HISTORY_STATUSES},
    }
    job_additions = {"status_counts": {status: job_statuses[status] for status in ALL_STATUSES}}
    outcomes = {
        "source": "queue_history",
        "not_landed_reason_counts": {reason: reasons[reason] for reason in _NOT_LANDED_REASONS},
        "conflicts": {
            "terminal_trains": terminal,
            "merge_conflict_trains": merge_conflicts,
            "semantic_conflict_trains": semantic_conflicts,
            "merge_conflict_rate": _rate(merge_conflicts, terminal),
            "semantic_conflict_rate": _rate(semantic_conflicts, terminal),
        },
    }
    return train_additions, job_additions, outcomes


def _batching_summary(
    attempts: Sequence[RunAttempt],
    gate_runs: Sequence[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    with_count = [attempt for attempt in attempts if attempt.job_count is not None]
    job_counts = [int(attempt.job_count) for attempt in with_count if attempt.job_count is not None]
    multi = [attempt for attempt in with_count if int(attempt.job_count or 0) > 1]
    successful_multi = {
        attempt.claim_token: attempt for attempt in multi if attempt.outcome == "succeeded"
    }
    eligible_runs: set[str] = set()
    observed_gate_executions = 0
    avoided_executions = 0
    avoided_seconds = 0.0
    for token, gate in gate_runs:
        attempt = successful_multi.get(token)
        duration = gate.get("duration_seconds")
        if attempt is None or gate.get("state") != "success" or duration is None:
            continue
        repeated = int(attempt.job_count or 0) - 1
        eligible_runs.add(token)
        observed_gate_executions += 1
        avoided_executions += repeated
        avoided_seconds += float(duration) * repeated

    mode_counts = Counter(attempt.mode for attempt in attempts)
    return {
        "source": "retained_run_events",
        "observed_runs": len(attempts),
        "runs_with_job_count": len(with_count),
        "mode_counts": {mode: mode_counts[mode] for mode in _RUN_MODES},
        "jobs_per_run": _count_summary(job_counts),
        "multi_job_runs": len(multi),
        "multi_job_run_rate": _rate(len(multi), len(with_count)),
        "estimated_savings": {
            "eligible_successful_multi_job_runs": len(eligible_runs),
            "observed_gate_executions": observed_gate_executions,
            "estimated_gate_executions_avoided": avoided_executions,
            "estimated_gate_seconds_avoided": round(avoided_seconds, 3),
            "method": (
                "For successful multi-job runs only, sum each timed successful "
                "gate duration multiplied by claimed_jobs - 1. This assumes an "
                "equivalent per-job workflow would run that gate once per job."
            ),
        },
    }


def _recovery_operation_summary(
    events: Sequence[RecoveryOperationEvent], *, since: str
) -> dict[str, Any]:
    baselines = [event for event in events if event.operation == "tracking"]
    baseline = min(baselines, key=lambda event: event.created_at) if baselines else None
    tracking_started_at = baseline.created_at if baseline is not None else None
    baseline_covers_all_history = bool(
        baseline is not None
        and "history_complete=1" in baseline.detail.split(";")
    )
    grouped: dict[str, list[RecoveryOperationEvent]] = {}
    for event in events:
        if event.operation in _RECOVERY_OPERATIONS and event.invocation_id:
            grouped.setdefault(event.invocation_id, []).append(event)

    attempts: list[RecoveryAttempt] = []
    for invocation_id, invocation_events in grouped.items():
        ordered = sorted(invocation_events, key=lambda event: event.id)
        started = next((event for event in ordered if event.state == "started"), None)
        if started is None:
            continue
        terminal = next((event for event in ordered if event.state != "started"), None)
        outcome = terminal.state if terminal is not None else "incomplete"
        if outcome not in _RECOVERY_OUTCOMES:
            outcome = "error"
        attempts.append(
            RecoveryAttempt(
                invocation_id=invocation_id,
                operation=started.operation,
                applied=started.applied,
                outcome=outcome,
            )
        )

    operation_counts = Counter(attempt.operation for attempt in attempts)
    state_counts = Counter(attempt.outcome for attempt in attempts)
    apply_count = sum(attempt.applied for attempt in attempts)
    history_complete = bool(
        tracking_started_at is not None
        and (
            baseline_covers_all_history
            or (since and since >= tracking_started_at)
        )
    )
    return {
        "source": "recovery_operation_events",
        "tracking_started_at": tracking_started_at,
        "history_complete": history_complete,
        "observed_invocations": len(attempts),
        "terminal_invocations": len(attempts) - state_counts["incomplete"],
        "operation_counts": {
            operation: operation_counts[operation]
            for operation in _RECOVERY_OPERATIONS
        },
        "state_counts": {
            outcome: state_counts[outcome] for outcome in _RECOVERY_OUTCOMES
        },
        "apply_mode_counts": {
            "apply": apply_count,
            "dry_run": len(attempts) - apply_count,
        },
    }


def _recovery_evidence_gaps(recovery: dict[str, Any]) -> list[dict[str, str]]:
    tracking_started_at = recovery["tracking_started_at"]
    if tracking_started_at is None:
        return [
            {
                "metric": "recovery_reconcile_frequency",
                "reason": (
                    "this database has no recovery-operation tracking baseline; "
                    "run a writable mergetrain command with the current schema"
                ),
            }
        ]
    if not recovery["history_complete"]:
        return [
            {
                "metric": "recovery_reconcile_frequency_before_tracking_start",
                "reason": (
                    "reconcile and recover invocations are durable from "
                    f"{tracking_started_at}; earlier command history is unknown. "
                    "Use --since at or after that timestamp for a complete window"
                ),
            }
        ]
    return []


def product_evidence(
    jobs: Sequence[Job],
    events: Sequence[RunEvent],
    *,
    gate_runs: Sequence[tuple[str, dict[str, Any]]],
    recovery_events: Sequence[RecoveryOperationEvent],
    since: str,
) -> dict[str, Any]:
    """Build additive stats blocks from durable, privacy-preserving evidence."""

    attempts = run_attempts(events)
    train_additions, job_additions, outcomes = _outcome_summary(jobs)
    recovery = _recovery_operation_summary(recovery_events, since=since)
    return {
        "train_additions": train_additions,
        "job_additions": job_additions,
        "outcomes": outcomes,
        "validation": {
            "runs": _run_outcome_summary(attempts, mode="validate"),
            "trains": _validated_train_summary(jobs),
        },
        "batching": _batching_summary(attempts, gate_runs),
        "recovery": recovery,
        "evidence_gaps": _recovery_evidence_gaps(recovery),
    }
