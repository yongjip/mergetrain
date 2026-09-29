"""Atomic queue claims that compose jobs, leases, events, and recovery guards."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence

from ..errors import DeployPlanChanged, QueueError
from ..models import Job
from .events import _record_run_event
from .jobs import get_job, list_jobs_fifo, select_validated_train
from .leases import _acquire_runner_lock, _release_lock_token, default_owner
from .recovery import deploy_reconcile_pending
from .transactions import immediate, utc_now

# A queued --auto job whose recorded approval no longer matches the current
# destination or execution policy is blocked, destination first. The notes and
# event messages are read back by evidence and observability; keep them exact.
_APPROVAL_CHECKS = (
    (
        "approval_destination_sha",
        "approval_destination_changed: unattended deploy approval does not match "
        "the current remote or push refs; enqueue again with --auto only after "
        "approving this destination",
        "Unattended deploy destination changed",
        "approval_destination_changed",
    ),
    (
        "approval_execution_policy_sha",
        "approval_execution_policy_changed: unattended deploy approval does not "
        "match the configured gates, validation reuse, or verify hooks; enqueue "
        "again with --auto only after approving this execution policy",
        "Unattended deploy execution policy changed",
        "approval_execution_policy_changed",
    ),
)


def _claim(
    conn: sqlite3.Connection,
    *,
    job_ids: list[int],
    expected_status: str,
    token: str,
    note: str,
    mode: str,
) -> list[Job]:
    """Move exactly these rows to in_progress under one claim token."""

    placeholders = ",".join("?" for _ in job_ids)
    cur = conn.execute(
        f"""
        UPDATE deploy_queue
        SET status = 'in_progress', started_at = ?, note = ?, claim_token = ?,
            cancel_requested_at = ''
        WHERE id IN ({placeholders}) AND status = ?
        """,
        (utc_now(), note, token, *job_ids, expected_status),
    )
    if cur.rowcount != len(job_ids):
        raise QueueError(
            "validated train changed while it was being claimed"
            if expected_status == "validated"
            else "queued jobs changed while they were being claimed"
        )
    _record_run_event(
        conn,
        claim_token=token,
        phase="claiming",
        state="active",
        message=(
            f"{'Deploy' if mode == 'deploy' else 'Validation'} runner claimed "
            f"{len(job_ids)} job(s)"
        ),
        detail=f"mode={mode}",
    )
    return [get_job(conn, job_id) for job_id in job_ids]


def claim_all_queued(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    ttl_minutes: int = 30,
    auto_only: bool = False,
    manual_only: bool = False,
    approval_destination_sha: str = "",
    approval_execution_policy_sha: str = "",
) -> list[Job]:
    """Claim queued jobs: all of them, only --auto ones (deploy), or only manual ones."""

    if auto_only and manual_only:
        raise QueueError("auto_only and manual_only are mutually exclusive")
    owner = owner or default_owner()
    with immediate(conn):
        lock = _acquire_runner_lock(conn, owner=owner, ttl_minutes=ttl_minutes)
        if (auto_only or manual_only) and deploy_reconcile_pending(conn):
            # Acquiring the lock can itself park marker-bearing orphans as
            # needs_reconcile (dead-owner requeue). Both daemon policies pause
            # for that state, so observe it inside the same claim transaction —
            # checking only before the claim leaves a TOCTOU window where the
            # selected batch runs past a newly created reconcile boundary.
            _release_lock_token(conn, owner=owner, token=lock.token)
            return []
        if manual_only:
            # The validation daemon is intentionally a one-train-at-a-time
            # workflow. Check inside the claim transaction, after acquiring the
            # shared runner lock, so another runner cannot create a validated
            # train between the daemon's read-only probe and this claim.
            validated = conn.execute(
                "SELECT 1 FROM deploy_queue WHERE status = 'validated' LIMIT 1"
            ).fetchone()
            if validated is not None:
                _release_lock_token(conn, owner=owner, token=lock.token)
                return []
        if auto_only:
            approvals = {
                "approval_destination_sha": approval_destination_sha,
                "approval_execution_policy_sha": approval_execution_policy_sha,
            }
            for column, note, message, detail in _APPROVAL_CHECKS:
                current = approvals[column]
                if not current:
                    continue
                mismatched = conn.execute(
                    f"""
                    SELECT id FROM deploy_queue
                    WHERE status = 'queued' AND auto_deploy = 1 AND {column} != ?
                    ORDER BY id ASC
                    """,
                    (current,),
                ).fetchall()
                for row in mismatched:
                    job_id = int(row["id"])
                    conn.execute(
                        """
                        UPDATE deploy_queue
                        SET status = 'blocked', finished_at = ?, note = ?
                        WHERE id = ? AND status = 'queued' AND auto_deploy = 1
                        """,
                        (utc_now(), note, job_id),
                    )
                    _record_run_event(
                        conn,
                        job_id=job_id,
                        phase="claiming",
                        state="error",
                        message=message,
                        detail=detail,
                    )
            rows = conn.execute(
                """
                SELECT id FROM deploy_queue
                WHERE status = 'queued' AND auto_deploy = 1
                  AND (? = '' OR approval_destination_sha = ?)
                  AND (? = '' OR approval_execution_policy_sha = ?)
                ORDER BY id ASC
                """,
                (
                    approval_destination_sha,
                    approval_destination_sha,
                    approval_execution_policy_sha,
                    approval_execution_policy_sha,
                ),
            ).fetchall()
        elif manual_only:
            rows = conn.execute(
                "SELECT id FROM deploy_queue WHERE status = 'queued' AND auto_deploy = 0 ORDER BY id ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM deploy_queue WHERE status = 'queued' ORDER BY id ASC"
            ).fetchall()
        job_ids = [int(row["id"]) for row in rows]
        if not job_ids:
            _release_lock_token(conn, owner=owner, token=lock.token)
            return []
        return _claim(
            conn,
            job_ids=job_ids,
            expected_status="queued",
            token=lock.token,
            note="claimed by mergetrain batch runner",
            mode="deploy" if auto_only else "validate",
        )


def claim_deploy_batch(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    ttl_minutes: int = 30,
    train_id: str = "",
    confirm_plan: Callable[[Sequence[Job]], None] | None = None,
) -> list[Job]:
    """Claim one exact validated train, or queued jobs when none is pending.

    With ``confirm_plan``, only a validated train may be claimed, and the
    callback checks that exact train inside the claim transaction; it raises
    to refuse. A concurrent cancel or supersede therefore cannot slip between
    the plan check and the claim, and queued jobs are never substituted. A
    reconcile that is pending inside the transaction still claims nothing and
    returns an empty list, which the caller must report as a refusal.
    """

    owner = owner or default_owner()
    with immediate(conn):
        lock = _acquire_runner_lock(conn, owner=owner, ttl_minutes=ttl_minutes)
        # Acquiring the lock can reap a dead owner and park a marker-bearing
        # orphan as needs_reconcile *inside this same transaction*. A deploy
        # targets the same push refs, so re-check here — not only in the CLI
        # pre-check — and refuse fail-closed if a reconcile is now pending
        # (mirrors claim_all_queued's guard, closing the claim/reconcile TOCTOU).
        if deploy_reconcile_pending(conn):
            _release_lock_token(conn, owner=owner, token=lock.token)
            return []
        selected, validated_jobs = select_validated_train(conn, train_id=train_id)
        if confirm_plan is not None:
            if selected is None or not validated_jobs:
                raise DeployPlanChanged(
                    "deploy_plan_changed: the confirmed validated train is no "
                    "longer deploy-eligible; nothing was pushed"
                )
            confirm_plan(validated_jobs)
        if selected is not None:
            jobs = validated_jobs
        else:
            jobs = list_jobs_fifo(conn, status="queued")
        if not jobs:
            _release_lock_token(conn, owner=owner, token=lock.token)
            return []
        return _claim(
            conn,
            job_ids=[job.id for job in jobs],
            expected_status="validated" if selected is not None else "queued",
            token=lock.token,
            note="claimed by mergetrain deploy runner",
            mode="deploy",
        )
