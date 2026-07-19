"""Stable, secret-conscious read models for agent-oriented CLI observability."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Sequence

from .config import MergetrainConfig
from .models import Job, RunEvent, RunnerLock
from .store import connect, get_job, get_lock, list_run_events, list_train_jobs, utc_now

GATE_EVENT = re.compile(r"^(?:Running|Passed|Reused) gate (\d+)/(\d+): (.+)$")
COMPLETED_STATUSES = {"validated", "deployed", "blocked", "failed", "canceled"}


def _timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def elapsed_seconds(start: str, end: str = "") -> float | None:
    started = _timestamp(start)
    finished = _timestamp(end or utc_now())
    if started is None or finished is None:
        return None
    return round(max(0.0, (finished - started).total_seconds()), 3)


def gate_details(event: RunEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    matched = GATE_EVENT.match(event.message)
    if matched is None:
        return None
    return {
        "index": int(matched.group(1)),
        "total": int(matched.group(2)),
        "name": matched.group(3),
        "state": event.state,
    }


def job_outcome(job: Job) -> dict[str, Any]:
    category = job.status
    severity = "pending"
    message = job.note or job.status

    if job.status == "deployed":
        severity = "success"
        category = "deployed"
        if job.verify_status == "failed":
            severity = "warning"
            category = "post_push_verification_failed"
    elif job.status == "validated":
        severity = "success"
        category = "validated"
    elif job.status == "canceled":
        severity = "failure"
        category = "canceled"
    elif job.status == "blocked":
        severity = "failure"
        lowered = message.lower()
        if "conflict" in lowered:
            category = "merge_conflict"
        elif "head changed" in lowered or "identity" in lowered:
            category = "source_identity_mismatch"
        elif "reuse" in lowered or "fingerprint" in lowered:
            category = "validated_reuse_mismatch"
        else:
            category = "merge_blocked"
    elif job.status == "failed":
        severity = "failure"
        lowered = message.lower()
        if job.push_status == "failed" or "push" in lowered:
            category = "push_failed"
        elif "timed out" in lowered:
            category = "command_timeout"
        elif "gate" in lowered or "command failed" in lowered:
            category = "gate_failed"
        else:
            category = "runner_failed"
    elif job.status == "in_progress":
        category = "running"

    return {
        "severity": severity,
        "category": category,
        "failure_category": category if severity == "failure" else None,
        "warning_categories": [category] if severity == "warning" else [],
        "message": message,
    }


def train_outcome(jobs: Sequence[Job]) -> dict[str, Any]:
    outcomes = [(job, job_outcome(job)) for job in jobs]
    failures = [
        {"job_id": job.id, "category": outcome["category"], "message": outcome["message"]}
        for job, outcome in outcomes
        if outcome["severity"] == "failure"
    ]
    warnings = [
        {"job_id": job.id, "category": outcome["category"], "message": outcome["message"]}
        for job, outcome in outcomes
        if outcome["severity"] == "warning"
    ]
    if failures:
        severity, category = "failure", "train_failed"
    elif warnings:
        severity, category = "warning", "train_completed_with_warnings"
    elif jobs and all(job.status in {"validated", "deployed"} for job in jobs):
        severity, category = "success", "train_completed"
    else:
        severity, category = "pending", "train_pending"
    return {
        "severity": severity,
        "category": category,
        "failure_categories": sorted({item["category"] for item in failures}),
        "warning_categories": sorted({item["category"] for item in warnings}),
        "failures": failures,
        "warnings": warnings,
        "status_counts": dict(sorted(Counter(job.status for job in jobs).items())),
    }


def _latest_run_events(job: Job, events: Sequence[RunEvent]) -> list[RunEvent]:
    latest_token = job.claim_token
    if not latest_token:
        latest_token = next(
            (event.claim_token for event in reversed(events) if event.claim_token),
            "",
        )
    if not latest_token:
        return list(events)
    return [event for event in events if event.claim_token == latest_token]


def _lease_context(job: Job, lock: RunnerLock | None) -> dict[str, Any]:
    matches = bool(
        job.status == "in_progress"
        and job.claim_token
        and lock
        and lock.token == job.claim_token
        and lock.liveness != "dead"
    )
    return {
        "heartbeat_at": lock.heartbeat_at if matches and lock else "",
        "expires_at": lock.expires_at if matches and lock else "",
        "liveness": lock.liveness if matches and lock else ("lost" if job.status == "in_progress" else "inactive"),
        "lost": bool(job.status == "in_progress" and not matches),
    }


def inspect_job_payload(
    config: MergetrainConfig,
    job_id: int,
    *,
    event_limit: int = 100,
) -> dict[str, Any]:
    conn = connect(config.state.db)
    try:
        job = get_job(conn, job_id)
        lock = get_lock(conn)
        events = list_run_events(conn, limit=event_limit, job_ids=[job.id])
        train_jobs = list_train_jobs(conn, job.train_id) if job.train_id else []
    finally:
        conn.close()

    run_events = _latest_run_events(job, events)
    latest = run_events[-1] if run_events else None
    if latest is not None:
        phase, state, message, updated_at = (
            latest.phase,
            latest.state,
            latest.message,
            latest.created_at,
        )
    elif job.status == "queued":
        phase, state, message, updated_at = "claiming", "queued", "Waiting for a runner", job.requested_at
    elif job.status == "validated":
        phase, state, message, updated_at = "ready", "success", "Waiting for deploy approval", job.validated_at
    elif job.status == "deployed":
        phase, state, message, updated_at = "complete", "success", "Deployment complete", job.finished_at
    else:
        phase, state, message, updated_at = job.status, job.status, job.note or job.status, job.finished_at

    end_at = "" if job.status == "in_progress" else (job.finished_at or updated_at)
    lease = _lease_context(job, lock)
    current_gate = gate_details(latest) if phase == "gating" else None
    progress = {
        "phase": phase,
        "state": state,
        "message": message,
        "detail": latest.detail if latest else "",
        "gate": current_gate,
        "started_at": job.started_at,
        "updated_at": updated_at,
        "elapsed_seconds": elapsed_seconds(job.started_at, end_at),
        "latest_event_id": latest.id if latest else None,
        "heartbeat_at": lease["heartbeat_at"],
        "lease_expires_at": lease["expires_at"],
        "lease_liveness": lease["liveness"],
        "lost_lease": lease["lost"],
    }
    train = None
    if train_jobs:
        train = {
            "train_id": job.train_id,
            "train_size": len(train_jobs),
            "outcome": train_outcome(train_jobs),
            "jobs": [
                {
                    "id": item.id,
                    "branch": item.branch,
                    "status": item.status,
                    "outcome": job_outcome(item),
                }
                for item in train_jobs
            ],
        }
    return {
        "ok": True,
        "job": job.to_dict(),
        "outcome": job_outcome(job),
        "progress": progress,
        "train": train,
        "events": [event.to_dict() for event in events],
    }


def event_record(
    event: RunEvent,
    jobs: Sequence[Job],
    lock: RunnerLock | None,
) -> dict[str, Any]:
    matching = next((job for job in jobs if job.id == event.job_id), None)
    start = matching.started_at if matching else next((job.started_at for job in jobs if job.started_at), "")
    tokens = {job.claim_token for job in jobs if job.claim_token}
    lease_matches = bool(lock and lock.token in tokens and lock.liveness != "dead")
    return {
        "type": "event",
        **event.to_dict(),
        "gate": gate_details(event),
        "elapsed_seconds": elapsed_seconds(start, event.created_at),
        "heartbeat_at": lock.heartbeat_at if lease_matches and lock else "",
        "lease_liveness": lock.liveness if lease_matches and lock else "inactive",
    }


def heartbeat_record(
    jobs: Sequence[Job],
    lock: RunnerLock,
    *,
    after_event_id: int,
    latest_event: RunEvent | None,
) -> dict[str, Any]:
    started_at = next((job.started_at for job in jobs if job.started_at), "")
    return {
        "type": "heartbeat",
        "after_event_id": after_event_id,
        "heartbeat_at": lock.heartbeat_at,
        "lease_liveness": lock.liveness,
        "phase": latest_event.phase if latest_event else "claiming",
        "gate": gate_details(latest_event),
        "elapsed_seconds": elapsed_seconds(started_at),
        "job_ids": [job.id for job in jobs],
    }


def stream_terminal(
    jobs: Sequence[Job],
    lock: RunnerLock | None,
) -> dict[str, Any] | None:
    if not jobs:
        return None
    running = [job for job in jobs if job.status == "in_progress"]
    if running:
        tokens = {job.claim_token for job in running if job.claim_token}
        lease_matches = bool(
            len(tokens) == 1
            and lock
            and lock.token in tokens
            and lock.liveness != "dead"
        )
        if not lease_matches:
            return {
                "reason": "lost_lease",
                "exit_code": 1,
                "outcome": train_outcome(jobs),
            }
        return None
    if any(job.status == "queued" for job in jobs):
        return None
    if any(job.status in {"failed", "blocked"} for job in jobs):
        reason, exit_code = "failure", 1
    elif any(job.status == "canceled" for job in jobs):
        reason, exit_code = "canceled", 1
    elif all(job.status in {"validated", "deployed"} for job in jobs):
        reason, exit_code = "success", 0
    elif all(job.status in COMPLETED_STATUSES for job in jobs):
        reason, exit_code = "completed", 0
    else:
        return None
    return {"reason": reason, "exit_code": exit_code, "outcome": train_outcome(jobs)}
