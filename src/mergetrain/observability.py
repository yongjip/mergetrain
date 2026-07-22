"""Stable, secret-conscious read models for agent-oriented CLI observability."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone
from math import ceil
from statistics import mean, median
from typing import Any

from .config import MergetrainConfig
from .errors import redact_secrets
from .models import Job, RunEvent, RunnerLock
from .store import (
    connect,
    get_job,
    get_lock,
    list_history_events,
    list_history_jobs,
    list_run_events,
    list_train_jobs,
    utc_now,
)

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


def normalize_since(value: str) -> str:
    if not value:
        return ""
    parsed = _timestamp(value)
    if parsed is None:
        raise ValueError("--since must be an ISO-8601 timestamp")
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _time_edge(values: Sequence[str], *, latest: bool = False) -> str:
    valid: list[tuple[str, datetime]] = []
    for stamp in values:
        instant = _timestamp(stamp)
        if instant is not None:
            valid.append((stamp, instant))
    if not valid:
        return ""
    return (max if latest else min)(valid, key=lambda item: item[1])[0]


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _gate_runs(events: Sequence[RunEvent]) -> list[dict[str, Any]]:
    active: dict[tuple[str, int, str], tuple[RunEvent, int]] = {}
    last_by_token: dict[str, RunEvent] = {}
    runs: list[dict[str, Any]] = []
    for event in events:
        if event.claim_token:
            last_by_token[event.claim_token] = event
        if event.phase != "gating":
            continue
        matched = GATE_EVENT.match(event.message)
        if matched is None:
            continue
        index, total, name = int(matched.group(1)), int(matched.group(2)), matched.group(3)
        key = (event.claim_token, index, name)
        if event.state == "active":
            previous = active.get(key)
            if previous is not None:
                prior, prior_total = previous
                runs.append(
                    {
                        "name": name,
                        "index": index,
                        "total": prior_total,
                        "state": "failed",
                        "started_at": prior.created_at,
                        "finished_at": event.created_at,
                        "elapsed_seconds": elapsed_seconds(
                            prior.created_at, event.created_at
                        ),
                    }
                )
            active[key] = (event, total)
            continue
        active_run = active.pop(key, None)
        started = active_run[0] if active_run else None
        runs.append(
            {
                "name": name,
                "index": index,
                "total": total,
                "state": "reused" if event.state == "reused" else event.state,
                "started_at": started.created_at if started else "",
                "finished_at": event.created_at,
                "elapsed_seconds": (
                    elapsed_seconds(started.created_at, event.created_at)
                    if started
                    else None
                ),
            }
        )
    for (token, index, name), (started, total) in active.items():
        terminal = last_by_token.get(token)
        failed = bool(
            terminal
            and terminal.id > started.id
            and terminal.state in {"error", "warning"}
        )
        runs.append(
            {
                "name": name,
                "index": index,
                "total": total,
                "state": "failed" if failed else "incomplete",
                "started_at": started.created_at,
                "finished_at": terminal.created_at if failed and terminal else "",
                "elapsed_seconds": (
                    elapsed_seconds(started.created_at, terminal.created_at)
                    if failed and terminal
                    else None
                ),
            }
        )
    return runs


def _history_status(jobs: Sequence[Job]) -> str:
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


def _group_history(
    jobs: Sequence[Job], events: Sequence[RunEvent]
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Job]] = {}
    for job in jobs:
        key = f"train:{job.train_id}" if job.train_id else f"job:{job.id}"
        grouped.setdefault(key, []).append(job)

    event_times = [(event, _timestamp(event.created_at)) for event in events]
    items: list[dict[str, Any]] = []
    for key, members in grouped.items():
        requested_at = _time_edge([job.requested_at for job in members])
        started_at = _time_edge([job.started_at for job in members])
        finished_at = _time_edge(
            [job.finished_at for job in members], latest=True
        )
        start = _timestamp(started_at)
        end = _timestamp(finished_at or utc_now())
        scoped_events = [
            event
            for event, created in event_times
            if start is not None
            and created is not None
            and created >= start
            and (end is None or created <= end)
        ]
        items.append(
            {
                "key": key,
                "train_id": members[0].train_id,
                "status": _history_status(members),
                "requested_at": requested_at,
                "started_at": started_at,
                "finished_at": finished_at,
                "queue_seconds": elapsed_seconds(requested_at, started_at)
                if started_at
                else None,
                "duration_seconds": elapsed_seconds(started_at, finished_at)
                if started_at
                else None,
                "outcome": train_outcome(members),
                "gates": _gate_runs(scoped_events),
                "jobs": [
                    {
                        "id": job.id,
                        "task": job.task,
                        "branch": job.branch,
                        "status": job.status,
                        "requested_at": job.requested_at,
                        "started_at": job.started_at,
                        "finished_at": job.finished_at,
                        "queue_seconds": elapsed_seconds(
                            job.requested_at, job.started_at
                        )
                        if job.started_at
                        else None,
                        "duration_seconds": elapsed_seconds(
                            job.started_at, job.finished_at
                        )
                        if job.started_at
                        else None,
                        "outcome": job_outcome(job),
                    }
                    for job in members
                ],
            }
        )
    return items


def history_payload(
    config: MergetrainConfig, *, since: str = "", limit: int = 50
) -> dict[str, Any]:
    conn = connect(config.state.db, read_only=True)
    try:
        jobs = list_history_jobs(conn, since=since, limit=limit)
        events = list_history_events(conn, since=since)
    finally:
        conn.close()
    return {
        "ok": True,
        "since": since,
        "limit": limit,
        "items": _group_history(jobs, events),
        "coverage": {
            "queue_history": "unbounded",
            "gate_events": "latest_5000",
        },
    }


def stats_payload(
    config: MergetrainConfig, *, since: str = ""
) -> dict[str, Any]:
    conn = connect(config.state.db, read_only=True)
    try:
        jobs = list_history_jobs(conn, since=since)
        events = list_history_events(conn, since=since)
    finally:
        conn.close()
    items = _group_history(jobs, events)
    landed = sum(item["status"] == "deployed" for item in items)
    blocked = sum(item["status"] == "blocked" for item in items)
    failed = sum(item["status"] == "failed" for item in items)
    completed = landed + blocked + failed
    durations = [
        float(item["duration_seconds"])
        for item in items
        if item["duration_seconds"] is not None
        and item["status"] in {"deployed", "blocked", "failed"}
    ]
    queue_times = [
        float(item["queue_seconds"])
        for item in items
        if item["queue_seconds"] is not None
    ]
    gate_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in _gate_runs(events):
        gate_groups[str(run["name"])].append(run)
    per_gate: list[dict[str, Any]] = []
    for name in sorted(gate_groups):
        runs = gate_groups[name]
        elapsed = [
            float(run["elapsed_seconds"])
            for run in runs
            if run["elapsed_seconds"] is not None
        ]
        states = Counter(str(run["state"]) for run in runs)
        per_gate.append(
            {
                "name": name,
                "runs": len(runs),
                "states": dict(sorted(states.items())),
                "median_seconds": round(median(elapsed), 3) if elapsed else None,
                "p95_seconds": _percentile(elapsed, 0.95),
            }
        )
    return {
        "ok": True,
        "since": since,
        "trains": {
            "total": len(items),
            "landed": landed,
            "blocked": blocked,
            "failed": failed,
            "completed": completed,
            "land_rate": round(landed / completed, 4) if completed else None,
        },
        "jobs": {"total": len(jobs)},
        "duration_seconds": {
            "median": round(median(durations), 3) if durations else None,
            "p95": _percentile(durations, 0.95),
        },
        "average_queue_seconds": round(mean(queue_times), 3) if queue_times else None,
        "gates": per_gate,
        "coverage": {
            "queue_history": "unbounded",
            "gate_events": "latest_5000",
        },
    }


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
    message = redact_secrets(job.note or job.status)

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
        if job.push_status == "failed":
            # Blocked at the push, not the merge/gates: the remote refused the
            # ref update (protected branch / required PR / permission). A
            # repo-config action, not a code fix — agents branch on this
            # category instead of regexing the note.
            category = "push_rejected"
        elif "conflict" in lowered:
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
        if job.push_status == "failed":
            # Rely on the structured push_status, not a note substring: a gate
            # named e.g. "no-force-push" fails before any push is attempted
            # (push_status stays not_run) and must not be mislabeled push_failed.
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
        phase, state, message, updated_at = (
            "ready",
            "success",
            f"Waiting for {config.terminology.noun} approval",
            job.validated_at,
        )
    elif job.status == "deployed":
        phase, state, message, updated_at = (
            "complete",
            "success",
            f"Git {config.terminology.noun} complete",
            job.finished_at,
        )
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
        "terminology": config.terminology.to_dict(),
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
    if any(job.status == "needs_reconcile" for job in jobs):
        reason, exit_code = "needs_reconcile", 1
    elif any(job.status in {"failed", "blocked"} for job in jobs):
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
