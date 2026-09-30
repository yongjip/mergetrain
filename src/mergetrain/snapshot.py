"""Privacy-conscious read models for CLI status and the hub."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from shlex import join as shell_join
from typing import Any

from .config import CONFIG_VERSION, MergetrainConfig, effective_gates
from .errors import PUBLIC_TEXT_LIMIT, redact_and_bound
from .git_ops import git_ref_exists, git_remote_exists, git_repo_root
from .models import Job, RunEvent, RunnerLock, public_owner
from .observability import _gate_runs
from .persistence.connection import connect
from .persistence.events import list_history_events
from .persistence.jobs import counts, list_jobs, list_jobs_fifo, validated_train_summaries
from .persistence.leases import get_lock
from .persistence.transactions import _parse_utc, read_snapshot, utc_now
from .reuse import reuse_explanation

PUBLIC_REASON_LIMIT = PUBLIC_TEXT_LIMIT

NEXT_ACTION_VALUES = frozenset(
    {
        "upgrade_mergetrain",
        "unlock_wedged_runner",
        "wait_for_runner",
        "reconcile_pending_deploy",
        "reconcile_conflict_manual",
        "fix_blocked_job",
        "resolve_failed_verification",
        "verify_reconciled_deploy",
        "deploy_when_approved",
        "cancel_and_reenqueue_legacy_validated_jobs",
        "run_daemon_when_approved",
        "validate_queued_jobs",
        "reconcile_stranded_claim",
        "initialize_config",
        "open_git_repository",
        "configure_git_remote",
        "fetch_integration_ref",
        "gc_available",
        "enqueue_clean_branch",
    }
)


@dataclass(frozen=True, slots=True)
class NextActionPlan:
    """One internally consistent recommendation for an operator or agent."""

    code: str
    command: str | None = None
    requires_approval: str = "none"
    target_job_id: int | None = None
    reason_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "command": self.command,
            "requires_approval": self.requires_approval,
            "target_job_id": self.target_job_id,
            "reason_code": self.reason_code,
        }


def attention_reason_code(job: Job) -> str | None:
    """Return a stable reason without interpreting free-form operator notes."""

    if job.status == "needs_reconcile":
        return "pending_reconcile"
    if job.status == "blocked" and job.pending_deploy_sha:
        return "reconcile_conflict"
    if job.status == "deployed" and job.verify_status == "failed":
        return "post_push_verification_failed"
    if job.status == "deployed" and job.verify_status == "unknown":
        return "post_push_verification_unknown"
    if job.conflict_with:
        return "semantic_conflict"
    if job.status in {"blocked", "failed"}:
        return job.status
    return None


def public_reason(job: Job) -> tuple[str | None, bool]:
    """Return a bounded, secret-masked explanation for public projections."""

    raw = (
        job.note
        or job.conflict_with
        or attention_reason_code(job)
        or job.status
    )
    redacted, truncated = redact_and_bound(raw, limit=PUBLIC_REASON_LIMIT)
    return redacted or None, truncated


def _attention_priority(job: Job) -> tuple[int, int]:
    reason = attention_reason_code(job)
    order = {
        "pending_reconcile": 0,
        "reconcile_conflict": 1,
        # A push already happened: failed production verification takes
        # precedence over work that never deployed.
        "post_push_verification_failed": 2,
        "semantic_conflict": 3,
        "blocked": 4,
        "failed": 5,
        "post_push_verification_unknown": 6,
    }
    return (order.get(reason or "", 99), -job.id)


def _plan_for_code(
    code: str,
    *,
    job: Job | None = None,
    remote_name: str = "origin",
) -> NextActionPlan:
    job_id = job.id if job else None
    mapping: dict[str, tuple[str | None, str]] = {
        "upgrade_mergetrain": (None, "none"),
        "unlock_wedged_runner": ("mergetrain unlock --force", "recovery"),
        "wait_for_runner": (None, "none"),
        "reconcile_pending_deploy": ("mergetrain reconcile --apply", "recovery"),
        "reconcile_conflict_manual": ("mergetrain reconcile", "recovery"),
        "fix_blocked_job": (
            f"mergetrain inspect {job_id}" if job_id is not None else None,
            "none",
        ),
        "resolve_failed_verification": (
            f"mergetrain verify --job {job_id}" if job_id is not None else None,
            "recovery",
        ),
        "verify_reconciled_deploy": (
            f"mergetrain verify --job {job_id}" if job_id is not None else "mergetrain verify",
            "recovery",
        ),
        "deploy_when_approved": ("mergetrain deploy", "deploy"),
        "cancel_and_reenqueue_legacy_validated_jobs": (None, "runner"),
        "run_daemon_when_approved": ("mergetrain daemon", "deploy"),
        "validate_queued_jobs": ("mergetrain validate", "runner"),
        "reconcile_stranded_claim": ("mergetrain reconcile --apply", "recovery"),
        "initialize_config": ("mergetrain init --write", "none"),
        "open_git_repository": (None, "none"),
        "configure_git_remote": (None, "none"),
        "fetch_integration_ref": (shell_join(["git", "fetch", remote_name]), "none"),
        "gc_available": ("mergetrain gc", "none"),
        "enqueue_clean_branch": (None, "none"),
    }
    command, approval = mapping.get(code, (None, "none"))
    return NextActionPlan(
        code=code,
        command=command,
        requires_approval=approval,
        target_job_id=job_id,
        reason_code=attention_reason_code(job) if job else None,
    )


def claim_is_stranded(lock: dict[str, Any] | None, in_progress: int) -> bool:
    """Whether in-progress work has no live runner: no lock, or a dead owner."""

    return bool(in_progress) and (not lock or lock.get("liveness") == "dead")


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


def plan_next_action(
    payload: dict[str, Any],
    *,
    config_version: int = CONFIG_VERSION,
    attention_jobs: Sequence[Job] = (),
) -> NextActionPlan:
    if config_version > CONFIG_VERSION:
        return _plan_for_code("upgrade_mergetrain")
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
        return _plan_for_code("unlock_wedged_runner")
    if lock and liveness == "alive" and not expired:
        return _plan_for_code("wait_for_runner")

    candidates = sorted(
        (job for job in attention_jobs if attention_reason_code(job)),
        key=_attention_priority,
    )
    selected = candidates[0] if candidates else None
    selected_reason = attention_reason_code(selected) if selected else None

    # A crash may have parked jobs (needs_reconcile), or left a marker-bearing
    # orphan a dead/absent runner never got to reconcile. Deploy is hard-blocked
    # until reconcile resolves it, so this dominates the deploy/validate tail.
    if count_data.get("needs_reconcile", 0) or (
        count_data.get("in_progress_with_marker", 0) and liveness != "alive"
    ):
        target = selected if selected_reason == "pending_reconcile" else None
        return _plan_for_code("reconcile_pending_deploy", job=target)
    # A blocked job that still carries its marker is a reconcile conflict needing
    # git inspection, distinct from a plain gate/assembly failure.
    if count_data.get("blocked_with_marker", 0):
        target = selected if selected_reason == "reconcile_conflict" else None
        return _plan_for_code("reconcile_conflict_manual", job=target)
    if selected_reason == "post_push_verification_failed" or count_data.get(
        "deployed_verify_failed", 0
    ):
        target = (
            selected
            if selected_reason == "post_push_verification_failed"
            else None
        )
        return _plan_for_code("resolve_failed_verification", job=target)
    if count_data.get("blocked", 0) or count_data.get("failed", 0):
        target = (
            selected
            if selected_reason in {"semantic_conflict", "blocked", "failed"}
            else None
        )
        return _plan_for_code("fix_blocked_job", job=target)
    # A reconcile-finalized deploy whose post-push verify could not be proven.
    if count_data.get("deployed_verify_unknown", 0):
        target = selected if selected_reason == "post_push_verification_unknown" else None
        return _plan_for_code("verify_reconciled_deploy", job=target)
    # Work claimed by a runner that no longer holds the lock: a crash, or a run
    # that raised after its lease was released (queue contention does this).
    # The next deploy requeues it automatically, which also clears its
    # validated-train identity -- so an approved train can quietly become a
    # different set. Name it instead of letting doctor report an idle queue.
    # A crash usually leaves its lock row behind, so a provably dead owner
    # counts the same as no lock at all (#227).
    if claim_is_stranded(lock, in_progress):
        return _plan_for_code("reconcile_stranded_claim")
    # Every queue-advancing command refuses without a config -- the deploy path
    # is fail-closed on purpose -- so pointing at queue work here would send the
    # reader into a refusal. Ranked below the recovery actions above, which stay
    # available precisely because they do not need a config.
    if payload.get("config_exists") is False:
        return _plan_for_code("initialize_config")
    if payload.get("repo_ready") is False:
        return _plan_for_code("open_git_repository")
    if payload.get("remote_ready") is False:
        return _plan_for_code("configure_git_remote")
    if payload.get("integration_ref_ready") is False:
        return _plan_for_code(
            "fetch_integration_ref",
            remote_name=str(payload.get("remote_name") or "origin"),
        )
    if payload.get("validated_trains"):
        if any(train.get("deploy_eligible") for train in payload["validated_trains"]):
            return _plan_for_code("deploy_when_approved")
        return _plan_for_code("cancel_and_reenqueue_legacy_validated_jobs")
    if count_data.get("auto_queued", 0):
        return _plan_for_code("run_daemon_when_approved")
    if count_data.get("queued", 0):
        return _plan_for_code("validate_queued_jobs")
    if payload.get("gc", {}).get("worktree_candidates"):
        return _plan_for_code("gc_available")
    return _plan_for_code("enqueue_clean_branch")


def next_action(payload: dict[str, Any], *, config_version: int = CONFIG_VERSION) -> str:
    """Compatibility wrapper for read models that consume only the code."""

    return plan_next_action(payload, config_version=config_version).code


def _readiness(config: MergetrainConfig) -> dict[str, Any]:
    """The config and Git preconditions that ``status`` weighs before queue work."""

    repo_ready = config.repo.is_dir() and bool(git_repo_root(config.repo))
    return {
        "config_exists": config.config_exists,
        "repo_ready": repo_ready,
        "remote_ready": repo_ready and git_remote_exists(config.repo, config.git.remote),
        "integration_ref_ready": repo_ready
        and git_ref_exists(config.repo, config.git.integration_tracking_ref),
        "remote_name": config.git.remote,
    }


def _public_job(job: Job, *, worktree_root: str = "", repo: str = "") -> dict[str, Any]:
    data = job.to_dict()
    worktree_path = str(data.get("worktree_path") or "")
    # A snapshot needs queue identity and reasons, not local filesystem paths.
    data.pop("worktree_path", None)
    data.pop("log_path", None)
    # Defence in depth for the read surfaces that other processes consume:
    # notes are already masked at the source (errors.redact_secrets in
    # CommandFailed.__str__), but re-mask here so a note written before that
    # guard — or by any future non-CommandFailed path — is never served in clear.
    # The paths are masked in the whole note before it is bounded; masked
    # after, a path cut at the limit kept its prefix.
    if job.note:
        public_note = job.note
        if worktree_path:
            public_note = public_note.replace(worktree_path, "[worktree]")
        if worktree_root:
            # A failed command's note names the integration worktree it ran in,
            # and that absolute path carries the user's home directory (#231).
            public_note = public_note.replace(worktree_root, "[worktrees]")
        if repo:
            # So does one that ran in the checkout itself.
            public_note = public_note.replace(repo, "[repo]")
        data["note"], data["note_truncated"] = redact_and_bound(public_note)
    return data


_EVENT_OWNER = re.compile(r"\(([^()\s]+):(\d+)\)")


def _public_event(event: RunEvent) -> dict[str, Any]:
    """An event without the OS username a runner-lock audit event records."""

    data = event.to_dict()
    if event.phase != "unlock":
        return data
    # Events written before owners were masked at the source still carry it.
    data["message"] = _EVENT_OWNER.sub(r"(local:\2)", str(data.get("message") or ""))
    try:
        detail = json.loads(str(data.get("detail") or ""))
    except ValueError:
        detail = None
    if isinstance(detail, dict) and isinstance(detail.get("owner"), str):
        detail["owner"] = public_owner(detail["owner"])
        data["detail"] = json.dumps(detail, sort_keys=True)
    return data


def _public_lock(lock: RunnerLock | None) -> dict[str, Any] | None:
    if lock is None:
        return None
    return {
        "name": lock.name,
        "owner": public_owner(lock.owner),
        "head_sha": lock.head_sha,
        "acquired_at": lock.acquired_at,
        "heartbeat_at": lock.heartbeat_at,
        "expires_at": lock.expires_at,
        "liveness": lock.liveness,
    }


def build_queue_summary(config: MergetrainConfig) -> dict[str, Any]:
    """Build the small queue truth needed by agents and Hub status.

    Unlike the full repo snapshot this does not load job history, events, or
    reuse analysis. Like it, it opens the queue database read-only.
    """

    conn = connect(config.state.db, read_only=True)
    try:
        with read_snapshot(conn):
            count_data = counts(conn)
            lock = _public_lock(get_lock(conn))
            validated_trains = validated_train_summaries(conn)
    finally:
        conn.close()
    payload: dict[str, Any] = {
        "counts": count_data,
        "lock": lock,
        "validated_trains": validated_trains,
    }
    payload["next_action"] = next_action(
        {
            **payload,
            **_readiness(config),
            "gc": {"worktree_candidates": []},
        },
        config_version=config.config_version,
    )
    return payload


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


def build_repo_snapshot(config: MergetrainConfig) -> dict[str, Any]:
    """Build one repository's full read-only queue snapshot.

    ``hub status --json`` reports one per registered repo. The queue database
    is opened read-only, without creating or migrating anything — the hub's
    contract when observing other repos.
    """

    worktree_root = str(config.state.worktree_root)
    repo = str(config.repo)
    conn = connect(config.state.db, read_only=True)
    try:
        with read_snapshot(conn):
            recent_jobs = list_jobs(conn, limit=50)
            selected_jobs, selection = _selected_jobs(conn)
            history_events = list_history_events(conn)
            # The 40 newest events.
            raw_events = history_events[-40:]
            lock = _public_lock(get_lock(conn))
            count_data = counts(conn)
            validated_trains = validated_train_summaries(conn)
        configured_gates = effective_gates(config)
        gate_names = ("diff-check", *(gate.name for gate in configured_gates))
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
                # Only the removed web dashboard's --preview set this; the key
                # stays because `hub status --json` publishes it.
                "preview": False,
                "gate_count": len(gate_names),
                "gates": [
                    {
                        "index": index,
                        "name": name,
                        "kind": "built-in" if index == 1 else "configured",
                    }
                    for index, name in enumerate(gate_names, start=1)
                ],
                "verify_count": len(config.deploy.verify),
                "reuse": {
                    "enabled": config.deploy.reuse.enabled,
                    "max_age_minutes": config.deploy.reuse.max_age_minutes,
                    "on_mismatch": config.deploy.reuse.on_mismatch,
                    "fingerprint_count": len(config.deploy.reuse.fingerprints),
                    "always_rerun_gates": [
                        gate.name for gate in configured_gates if gate.always_rerun_on_deploy
                    ],
                },
            },
            "counts": count_data,
            "lock": lock,
            "jobs": [
                _public_job(job, worktree_root=worktree_root, repo=repo) for job in recent_jobs
            ],
            "train": {
                "selection": selection,
                "jobs": [
                    _public_job(job, worktree_root=worktree_root, repo=repo)
                    for job in selected_jobs
                ],
            },
            "events": [_public_event(event) for event in raw_events],
            "validated_trains": validated_trains,
            "reuse": reuse_explanation(
                config,
                selected_jobs,
                decision=None,
                gate_runs=_gate_runs(history_events),
            ),
        }
        payload["next_action"] = next_action(
            {**payload, **_readiness(config)}, config_version=config.config_version
        )
        return payload
    finally:
        conn.close()
