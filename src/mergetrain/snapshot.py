"""Privacy-conscious read models for CLI status and the local dashboard."""

from __future__ import annotations

import re
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from .config import CONFIG_VERSION, MergetrainConfig
from .errors import redact_secrets
from .models import Job, RunEvent, RunnerLock
from .observability import _gate_runs, elapsed_seconds
from .reuse import reuse_explanation
from .store import (
    _parse_utc,
    connect,
    counts,
    get_lock,
    list_history_events,
    list_jobs,
    list_jobs_fifo,
    owner_liveness,
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

GATE_EVENT = re.compile(
    r"^(?:Running|Passed|Reused|Skipped|Failed|Canceled) gate (\d+)/(\d+): (.+)$"
)
ESTIMATE_PHASES = ("fetching", "assembling", "gating", "pushing", "verifying")
ESTIMATE_SAMPLE_LIMIT = 20

NEXT_ACTION_VALUES = frozenset(
    {
        "upgrade_mergetrain",
        "unlock_wedged_runner",
        "wait_for_runner",
        "reconcile_pending_deploy",
        "reconcile_conflict_manual",
        "fix_blocked_job",
        "verify_reconciled_deploy",
        "deploy_validated_train_when_approved",
        "cancel_and_reenqueue_legacy_validated_jobs",
        "run_daemon_or_run_batch_deploy_when_approved",
        "run_batch_validate",
        "recover_stranded_claim",
        "initialize_config",
        "gc_available",
        "enqueue_clean_branch",
    }
)


def _lock_expired(lock: dict[str, Any] | None) -> bool:
    if not lock:
        return False
    expires_at = lock.get("expires_at")
    if not expires_at:
        return True
    try:
        return _parse_utc(str(expires_at)) <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        # Corrupted state must never make observation surfaces fail. Treat an
        # unparseable lease conservatively as expired so it cannot be reported
        # as a healthy live runner.
        return True


def next_action(
    payload: dict[str, Any], *, config_version: int = CONFIG_VERSION
) -> str:
    if config_version > CONFIG_VERSION:
        return "upgrade_mergetrain"
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
    ):
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
    # Work claimed by a runner that is no longer holding the lock: a crash, or a
    # run that raised after its lease was released (queue contention does this).
    # The next deploy requeues it automatically, which also clears its
    # validated-train identity -- so an approved train can quietly become a
    # different set. Name it instead of letting doctor report an idle queue.
    if not lock and in_progress:
        return "recover_stranded_claim"
    # Every queue-advancing command refuses without a config -- the deploy path
    # is fail-closed on purpose -- so pointing at queue work here would send the
    # reader into a refusal. Ranked below the recovery actions above, which stay
    # available precisely because they do not need a config.
    if payload.get("config_exists") is False:
        return "initialize_config"
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


def refresh_dashboard_snapshot(
    payload: dict[str, Any], *, config_version: int = CONFIG_VERSION
) -> dict[str, Any]:
    """Refresh clock/process-derived fields without reopening the queue DB."""

    refreshed = deepcopy(payload)
    lock = refreshed.get("lock")
    if isinstance(lock, dict) and lock.get("owner"):
        pid_suffix = str(lock["owner"]).rsplit(":", 1)[-1]
        lock["liveness"] = owner_liveness(f"local:{pid_suffix}")
    refreshed["generated_at"] = utc_now()
    refreshed["next_action"] = next_action(
        refreshed, config_version=config_version
    )
    return refreshed


def _public_job(job: Job) -> dict[str, Any]:
    data = job.to_dict()
    worktree_path = str(data.get("worktree_path") or "")
    # The dashboard needs queue identity and reasons, not local filesystem paths.
    data.pop("worktree_path", None)
    data.pop("log_path", None)
    # Defence in depth for the network-reachable read surfaces (dashboard, hub):
    # notes are already masked at the source (errors.redact_secrets in
    # CommandFailed.__str__), but re-mask here so a note written before that
    # guard — or by any future non-CommandFailed path — is never served in clear.
    note = data.get("note")
    if note:
        public_note = redact_secrets(note)
        if worktree_path:
            public_note = public_note.replace(worktree_path, "[worktree]")
        data["note"] = public_note
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


def _median_samples(
    samples: list[tuple[int, float]], *, limit: int = ESTIMATE_SAMPLE_LIMIT
) -> tuple[int, float | None]:
    recent = [duration for _, duration in sorted(samples)[-limit:]]
    if not recent:
        return 0, None
    return len(recent), round(median(recent), 3)


def _sum_complete(values: list[float | None]) -> float | None:
    if not values or any(value is None for value in values):
        return None
    return round(sum(value for value in values if value is not None), 3)


def _phase_duration_samples(
    events: list[RunEvent], *, exclude_token: str = ""
) -> dict[str, list[tuple[int, float]]]:
    """Return completed phase spans grouped by phase.

    A batch emits nested per-job assembly and per-gate events. The phase span is
    deliberately the first active event through the last terminal event for the
    same claim token and phase, so those nested milestones do not shorten the
    estimate.
    """

    grouped: dict[tuple[str, str], list[RunEvent]] = defaultdict(list)
    for event in events:
        if (
            not event.claim_token
            or event.claim_token == exclude_token
            or event.phase not in ESTIMATE_PHASES
        ):
            continue
        grouped[(event.claim_token, event.phase)].append(event)

    samples: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for (_, phase), phase_events in grouped.items():
        ordered = sorted(phase_events, key=lambda event: event.id)
        started = next(
            (event for event in ordered if event.state == "active"), None
        )
        finished = next(
            (
                event
                for event in reversed(ordered)
                if event.state in {"success", "warning", "error"}
                and (started is None or event.id > started.id)
            ),
            None,
        )
        if started is None or finished is None:
            continue
        duration = elapsed_seconds(started.created_at, finished.created_at)
        if duration is not None:
            samples[phase].append((finished.id, duration))
    return samples


def _current_phase_started_at(
    run_events: list[RunEvent], phase: str
) -> str:
    return next(
        (
            event.created_at
            for event in run_events
            if event.phase == phase and event.state == "active"
        ),
        "",
    )


def _eta_payload(
    *,
    events: list[RunEvent],
    selected_jobs: list[Job],
    progress: dict[str, Any],
    selection: str,
    gate_names: tuple[str, ...],
    calculated_at: str,
) -> dict[str, Any]:
    token = next((job.claim_token for job in selected_jobs if job.claim_token), "")
    run_events = [
        event for event in events if token and event.claim_token == token
    ]
    phase_samples = _phase_duration_samples(events, exclude_token=token)
    phases: list[dict[str, Any]] = []
    phase_estimates: dict[str, tuple[int, float | None]] = {}
    for phase in ESTIMATE_PHASES:
        sample_count, estimate = _median_samples(phase_samples.get(phase, []))
        phase_estimates[phase] = (sample_count, estimate)
        phases.append(
            {
                "name": phase,
                "sample_count": sample_count,
                "median_seconds": estimate,
            }
        )

    historical_events = [
        event for event in events if not token or event.claim_token != token
    ]
    gate_samples: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for ordinal, run in enumerate(_gate_runs(historical_events), start=1):
        duration = run.get("duration_seconds")
        if duration is None:
            continue
        gate_samples[str(run["name"])].append(
            (ordinal, float(duration))
        )

    progress_gates = {
        str(gate.get("name")): gate for gate in progress.get("gates", [])
    }
    gates: list[dict[str, Any]] = []
    gate_remaining: list[float | None] = []
    gate_sample_counts: list[int] = []
    for index, name in enumerate(gate_names, start=1):
        sample_count, estimate = _median_samples(gate_samples.get(name, []))
        current = progress_gates.get(name, {})
        state = str(current.get("state") or "waiting")
        started_at = str(current.get("started_at") or "")
        current_elapsed = (
            elapsed_seconds(started_at, calculated_at) if started_at else None
        )
        if state in {"success", "reused", "skipped"}:
            remaining: float | None = 0.0
        elif estimate is None:
            remaining = None
        elif state == "active" and current_elapsed is not None:
            remaining = round(max(0.0, estimate - current_elapsed), 3)
        else:
            remaining = estimate
        if state not in {"success", "reused", "skipped"}:
            gate_remaining.append(remaining)
            if remaining is not None:
                gate_sample_counts.append(sample_count)
        gates.append(
            {
                "index": index,
                "name": name,
                "state": state,
                "sample_count": sample_count,
                "median_seconds": estimate,
                "elapsed_seconds": current_elapsed,
                "remaining_seconds": remaining,
            }
        )

    remaining_parts: list[float | None] = []
    used_sample_counts: list[int] = []
    current_phase = str(progress.get("phase") or "")
    deploying = any(
        job.status == "in_progress" and bool(job.train_id)
        for job in selected_jobs
    ) or current_phase in {"pushing", "verifying", "complete"}
    target_phases = list(ESTIMATE_PHASES[:3])
    if deploying:
        target_phases.extend(ESTIMATE_PHASES[3:])

    if selection == "running":
        try:
            current_index = target_phases.index(current_phase)
        except ValueError:
            current_index = 0
        for phase in target_phases[current_index:]:
            sample_count, estimate = phase_estimates[phase]
            if phase == "gating" and current_phase == "gating" and gate_remaining:
                gate_total = _sum_complete(gate_remaining)
                if gate_total is not None:
                    remaining_parts.append(gate_total)
                    used_sample_counts.extend(gate_sample_counts)
                else:
                    remaining_parts.append(None)
                continue
            if estimate is None:
                remaining_parts.append(None)
                continue
            if phase == current_phase:
                started_at = _current_phase_started_at(run_events, phase)
                current_elapsed = (
                    elapsed_seconds(started_at, calculated_at)
                    if started_at
                    else None
                )
                remaining_parts.append(
                    round(max(0.0, estimate - float(current_elapsed or 0.0)), 3)
                )
            else:
                remaining_parts.append(estimate)
            used_sample_counts.append(sample_count)

    estimated_remaining = _sum_complete(remaining_parts)
    available = estimated_remaining is not None
    expected_at = ""
    if estimated_remaining is not None:
        expected_at = (
            _parse_utc(calculated_at) + timedelta(seconds=estimated_remaining)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
    has_history = any(item["sample_count"] for item in (*phases, *gates))
    return {
        "basis": "median_recent_completed_runs",
        "sample_limit": ESTIMATE_SAMPLE_LIMIT,
        "available": available,
        "coverage": "complete" if available else "partial" if has_history else "none",
        "sample_count": min(used_sample_counts) if used_sample_counts else 0,
        "calculated_at": calculated_at,
        "expected_at": expected_at,
        "estimated_remaining_seconds": estimated_remaining,
        "phases": phases,
        "gates": gates,
    }


def _progress(
    selected_jobs: list[Job],
    events,
    selection: str,
    gate_names: tuple[str, ...],
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
        message = "Validated train is waiting for deployment approval"
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
            previous = gate_events.get(gate_index)
            started_at = (
                event.created_at
                if event.state == "active"
                else str(previous.get("started_at") or "")
                if previous
                else ""
            )
            finished_at = "" if event.state == "active" else event.created_at
            latest_gate = {
                "index": gate_index,
                "total": int(gate_match.group(2)),
                "name": gate_match.group(3),
                "state": event.state,
                "command": event.detail,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_seconds": (
                    elapsed_seconds(started_at, finished_at)
                    if started_at and finished_at
                    else None
                ),
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
        if observed and observed["state"] in {"reused", "skipped"}:
            gate_state = observed["state"]
        elif all_gates_passed:
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
                "started_at": observed["started_at"] if observed else "",
                "finished_at": observed["finished_at"] if observed else "",
                "duration_seconds": (
                    observed["duration_seconds"] if observed else None
                ),
            }
        )

    current_gate = None
    if (
        latest
        and latest.phase == "gating"
        and latest_gate
        and latest_gate["state"] == "active"
    ):
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
        history_events = list_history_events(conn)
        raw_events = history_events[-max(1, min(int(event_limit), 200)):]
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
            "reuse": reuse_explanation(
                config,
                selected_jobs,
                decision=None,
                gate_runs=_gate_runs(history_events),
            ),
        }
        payload["progress"] = _progress(
            selected_jobs,
            raw_events,
            selection,
            gate_names,
        )
        payload["eta"] = _eta_payload(
            events=history_events,
            selected_jobs=selected_jobs,
            progress=payload["progress"],
            selection=selection,
            gate_names=gate_names,
            calculated_at=payload["generated_at"],
        )
        payload["next_action"] = next_action(
            payload, config_version=config.config_version
        )
        return payload
    finally:
        conn.close()
