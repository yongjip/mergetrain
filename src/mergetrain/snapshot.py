"""Privacy-conscious read models for CLI status and the local dashboard."""

from __future__ import annotations

from typing import Any

from .config import MergetrainConfig
from .models import Job, RunnerLock
from .store import (
    connect,
    counts,
    get_lock,
    list_jobs,
    list_jobs_fifo,
    list_run_events,
    utc_now,
    validated_train_summaries,
)

PHASES = (
    "claiming",
    "fetching",
    "assembling",
    "gating",
    "ready",
    "pushing",
    "verifying",
    "complete",
)


def next_action(payload: dict[str, Any]) -> str:
    lock = payload.get("lock")
    count_data = payload.get("counts") or {}
    if lock and lock.get("liveness") == "alive":
        return "wait_for_runner"
    if count_data.get("blocked", 0) or count_data.get("failed", 0):
        return "fix_blocked_job"
    if payload.get("validated_trains"):
        if any(train.get("deploy_eligible") for train in payload["validated_trains"]):
            return "deploy_validated_train_when_approved"
        return "cancel_and_reenqueue_legacy_validated_jobs"
    if count_data.get("auto_queued", 0):
        return "run_daemon_or_run_batch_deploy_when_approved"
    if count_data.get("queued", 0):
        return "run_batch_validate"
    if payload.get("gc", {}).get("worktree_candidates"):
        return "gc_available"
    return "enqueue_clean_branch"


def _public_job(job: Job) -> dict[str, Any]:
    data = job.to_dict()
    # The dashboard needs queue identity and reasons, not local filesystem paths.
    data.pop("worktree_path", None)
    data.pop("log_path", None)
    return data


def _public_lock(lock: RunnerLock | None) -> dict[str, Any] | None:
    if lock is None:
        return None
    owner_suffix = lock.owner.rsplit(":", 1)[-1]
    return {
        "name": lock.name,
        "owner": f"local:{owner_suffix}",
        "head_sha": lock.head_sha,
        "acquired_at": lock.acquired_at,
        "heartbeat_at": lock.heartbeat_at,
        "expires_at": lock.expires_at,
        "liveness": lock.liveness,
    }


def _selected_jobs(conn) -> tuple[list[Job], str]:
    in_progress = list_jobs_fifo(conn, status="in_progress")
    if in_progress:
        return in_progress, "running"
    validated = list_jobs_fifo(conn, status="validated")
    if validated:
        train_id = validated[0].train_id
        if train_id:
            return [job for job in validated if job.train_id == train_id], "validated"
        return validated, "validated"
    queued = list_jobs_fifo(conn, status="queued")
    if queued:
        return queued[:8], "queued"
    return [], "idle"


def _progress(selected_jobs: list[Job], events, selection: str) -> dict[str, Any]:
    token = next((job.claim_token for job in selected_jobs if job.claim_token), "")
    run_events = [event for event in events if token and event.claim_token == token]
    latest = run_events[-1] if run_events else None
    if latest:
        phase = latest.phase
        state = latest.state
        message = latest.message
        updated_at = latest.created_at
    elif selection == "validated":
        phase, state = "ready", "success"
        message = "Validated train is waiting for deploy approval"
        updated_at = selected_jobs[0].validated_at if selected_jobs else ""
    elif selection == "queued":
        phase, state = "claiming", "queued"
        message = "Jobs are waiting for a runner"
        updated_at = selected_jobs[0].requested_at if selected_jobs else ""
    else:
        phase, state = "claiming", "idle"
        message = "No active train"
        updated_at = ""

    completed: list[str] = []
    for event in run_events:
        if event.state == "success" and event.phase in PHASES and event.phase not in completed:
            completed.append(event.phase)
    started_at = next((job.started_at for job in selected_jobs if job.started_at), "")
    return {
        "phase": phase,
        "state": state,
        "message": message,
        "job_id": latest.job_id if latest else None,
        "started_at": started_at,
        "updated_at": updated_at,
        "completed_phases": completed,
    }


def build_dashboard_snapshot(
    config: MergetrainConfig,
    *,
    job_limit: int = 50,
    event_limit: int = 40,
) -> dict[str, Any]:
    """Build one stable, read-only payload for the browser."""

    conn = connect(config.state.db)
    try:
        recent_jobs = list_jobs(conn, limit=job_limit)
        selected_jobs, selection = _selected_jobs(conn)
        raw_events = list_run_events(conn, limit=event_limit)
        lock = _public_lock(get_lock(conn))
        payload: dict[str, Any] = {
            "ok": True,
            "generated_at": utc_now(),
            "project": {
                "name": config.project.name,
                "integration_ref": config.git.integration_ref,
                "config_exists": config.config_exists,
                "gate_count": 1 + len(config.gates),
                "verify_count": len(config.deploy.verify),
            },
            "counts": counts(conn),
            "lock": lock,
            "jobs": [_public_job(job) for job in recent_jobs],
            "train": {
                "selection": selection,
                "jobs": [_public_job(job) for job in selected_jobs],
            },
            "events": [event.to_dict() for event in raw_events],
            "validated_trains": validated_train_summaries(conn),
        }
        payload["progress"] = _progress(selected_jobs, raw_events, selection)
        payload["next_action"] = next_action(payload)
        return payload
    finally:
        conn.close()
