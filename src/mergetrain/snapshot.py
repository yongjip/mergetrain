"""Privacy-conscious read models for CLI status and the local dashboard."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .config import MergetrainConfig
from .errors import redact_secrets
from .models import Job, RunnerLock
from .store import (
    _parse_utc,
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

GATE_EVENT = re.compile(r"^(?:Running|Passed|Reused) gate (\d+)/(\d+): (.+)$")


def _lock_expired(lock: dict[str, Any] | None) -> bool:
    if not lock or not lock.get("expires_at"):
        return False
    return _parse_utc(str(lock["expires_at"])) <= datetime.now(timezone.utc)


def next_action(payload: dict[str, Any]) -> str:
    lock = payload.get("lock")
    count_data = payload.get("counts") or {}
    liveness = lock.get("liveness") if lock else None
    expired = _lock_expired(lock)
    in_progress = count_data.get("in_progress", 0)
    # A wedge: the lease lapsed but the owner still looks alive/unknown and work
    # is mid-flight. A healthy runner would have refreshed its lease; this one
    # cannot be auto-stolen (it may still be pushing) — the operator must run
    # `unlock --force` (0.3.0 Phase 2, RFC §7).
    if lock and expired and liveness in {"alive", "unknown"} and in_progress > 0:
        return "unlock_wedged_runner"
    if lock and liveness == "alive" and not expired:
        return "wait_for_runner"
    # A crash may have parked jobs (needs_reconcile), or left a marker-bearing
    # orphan a dead/absent runner never got to reconcile. Deploy is hard-blocked
    # until reconcile resolves it, so this dominates the deploy/validate tail.
    if count_data.get("needs_reconcile", 0) or (
        count_data.get("in_progress_with_marker", 0) and liveness != "alive"
    ) or count_data.get("failed_with_marker", 0):
        return "reconcile_pending_deploy"
    # A blocked job that still carries its marker is a reconcile conflict needing
    # git inspection, distinct from a plain gate/assembly failure.
    if count_data.get("blocked_with_marker", 0):
        return "reconcile_conflict_manual"
    if count_data.get("blocked", 0) or count_data.get("failed", 0):
        return "fix_blocked_job"
    # A reconcile-finalized deploy whose post-push verify could not be proven.
    if count_data.get("deployed_verify_unknown", 0):
        return "verify_reconciled_deploy"
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
    # Defence in depth for the network-reachable read surfaces (dashboard, hub):
    # notes are already masked at the source (errors.redact_secrets in
    # CommandFailed.__str__), but re-mask here so a note written before that
    # guard — or by any future non-CommandFailed path — is never served in clear.
    note = data.get("note")
    if note:
        data["note"] = redact_secrets(note)
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


def _progress(
    selected_jobs: list[Job],
    events,
    selection: str,
    gate_names: tuple[str, ...],
    git_noun: str,
) -> dict[str, Any]:
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
        message = f"Validated train is waiting for {git_noun} approval"
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
    completed_job_ids: list[int] = []
    gate_events: dict[int, dict[str, Any]] = {}
    latest_gate: dict[str, Any] | None = None
    all_gates_passed = False
    for event in run_events:
        gate_match = GATE_EVENT.match(event.message)
        if gate_match:
            gate_index = int(gate_match.group(1))
            latest_gate = {
                "index": gate_index,
                "total": int(gate_match.group(2)),
                "name": gate_match.group(3),
                "state": event.state,
                "command": event.detail,
                "started_at": event.created_at,
            }
            gate_events[gate_index] = latest_gate
        if event.phase == "gating" and event.state == "success" and event.message == "All train gates passed":
            all_gates_passed = True
        phase_completed = event.state == "success" and event.phase in PHASES
        if event.phase == "gating" and not all_gates_passed:
            phase_completed = False
        if event.phase == "assembling" and event.job_id is not None and len(selected_jobs) > 1:
            phase_completed = False
        if phase_completed and event.phase not in completed:
            completed.append(event.phase)
        if (
            event.state == "success"
            and event.phase == "assembling"
            and event.job_id is not None
            and event.job_id not in completed_job_ids
        ):
            completed_job_ids.append(event.job_id)
    gate_progress: list[dict[str, Any]] = []
    for index, name in enumerate(gate_names, start=1):
        observed = gate_events.get(index)
        if all_gates_passed:
            gate_state = "success"
        elif observed:
            gate_state = observed["state"]
        elif latest_gate and index < latest_gate["index"]:
            gate_state = "success"
        else:
            gate_state = "waiting"
        gate_progress.append(
            {
                "index": index,
                "total": len(gate_names),
                "name": name,
                "state": gate_state,
                "command": observed["command"] if observed else "",
            }
        )

    current_gate = None
    if latest and latest.phase == "gating" and latest.message != "All train gates passed":
        current_gate = latest_gate

    started_at = next((job.started_at for job in selected_jobs if job.started_at), "")
    return {
        "phase": phase,
        "state": state,
        "message": message,
        "detail": latest.detail if latest else "",
        "job_id": latest.job_id if latest else None,
        "started_at": started_at,
        "updated_at": updated_at,
        "completed_phases": completed,
        "completed_job_ids": completed_job_ids,
        "gates": gate_progress,
        "current_gate": current_gate,
    }


def build_dashboard_snapshot(
    config: MergetrainConfig,
    *,
    job_limit: int = 50,
    event_limit: int = 40,
    preview: bool = False,
    read_only: bool = False,
) -> dict[str, Any]:
    """Build one stable, read-only payload for the browser.

    With ``read_only`` the queue database is opened without creating or
    migrating anything — the hub's contract when observing other repos.
    """

    conn = connect(config.state.db, read_only=read_only)
    try:
        recent_jobs = list_jobs(conn, limit=job_limit)
        selected_jobs, selection = _selected_jobs(conn)
        raw_events = list_run_events(conn, limit=event_limit)
        lock = _public_lock(get_lock(conn))
        gate_names = ("diff-check", *(gate.name for gate in config.gates))
        payload: dict[str, Any] = {
            "ok": True,
            "generated_at": utc_now(),
            "project": {
                "name": config.project.name,
                "integration_ref": config.git.integration_ref,
                "remote": config.git.remote,
                "push_refs": list(config.git.push_refs),
                "push_specs": [f"HEAD:{ref}" for ref in config.git.push_refs],
                "terminology": config.terminology.to_dict(),
                "config_exists": config.config_exists,
                "preview": preview,
                "gate_count": len(gate_names),
                "gates": [
                    {"index": index, "name": name, "kind": "built-in" if index == 1 else "configured"}
                    for index, name in enumerate(gate_names, start=1)
                ],
                "verify_count": len(config.deploy.verify),
                "reuse": {
                    "enabled": config.deploy.reuse.enabled,
                    "max_age_minutes": config.deploy.reuse.max_age_minutes,
                    "on_mismatch": config.deploy.reuse.on_mismatch,
                    "fingerprint_count": len(config.deploy.reuse.fingerprints),
                    "always_rerun_gates": [
                        gate.name
                        for gate in config.gates
                        if gate.always_rerun_on_deploy
                    ],
                },
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
        payload["progress"] = _progress(
            selected_jobs,
            raw_events,
            selection,
            gate_names,
            config.terminology.noun,
        )
        payload["next_action"] = next_action(payload)
        return payload
    finally:
        conn.close()
