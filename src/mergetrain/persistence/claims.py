"""Atomic queue claims that compose jobs, leases, events, and recovery guards."""

from __future__ import annotations

import sqlite3

from ..errors import QueueError
from ..models import Job
from .events import _record_run_event
from .jobs import get_job, list_jobs_fifo, select_validated_train
from .leases import _acquire_runner_lock, _release_lock_token, default_owner
from .recovery import deploy_reconcile_pending
from .transactions import immediate, utc_now


def claim_next_job(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    ttl_minutes: int = 30,
    deploy: bool = False,
) -> Job | None:
    owner = owner or default_owner()
    with immediate(conn):
        lock = _acquire_runner_lock(conn, owner=owner, ttl_minutes=ttl_minutes)
        if deploy and deploy_reconcile_pending(conn):
            # Lock acquisition can park a marker-bearing orphan in this same
            # transaction. Refuse the new deploy claim after that state change,
            # just like the daemon and batch claim paths do.
            _release_lock_token(conn, owner=owner, token=lock.token)
            return None
        row = conn.execute(
            "SELECT * FROM deploy_queue WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            _release_lock_token(conn, owner=owner, token=lock.token)
            return None
        job_id = int(row["id"])
        conn.execute(
            """
            UPDATE deploy_queue
            SET status = 'in_progress', started_at = ?, note = ?, claim_token = ?,
                cancel_requested_at = ''
            WHERE id = ? AND status = 'queued'
            """,
            (utc_now(), "claimed by mergetrain runner", lock.token, job_id),
        )
        _record_run_event(
            conn,
            claim_token=lock.token,
            job_id=job_id,
            phase="claiming",
            state="active",
            message=(
                f"{'Deploy' if deploy else 'Validation'} runner claimed 1 job"
            ),
            detail=f"mode={'deploy' if deploy else 'validate'}",
        )
    return get_job(conn, job_id)


def claim_all_queued(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    ttl_minutes: int = 30,
    auto_only: bool = False,
    deploy: bool = False,
) -> list[Job]:
    owner = owner or default_owner()
    with immediate(conn):
        lock = _acquire_runner_lock(conn, owner=owner, ttl_minutes=ttl_minutes)
        if auto_only and deploy_reconcile_pending(conn):
            # Acquiring the lock can itself park marker-bearing orphans as
            # needs_reconcile (dead-owner requeue). An unattended deploy must
            # observe that inside the same claim transaction — checking only
            # before the claim leaves a TOCTOU window where the daemon pushes
            # over a pending reconcile.
            _release_lock_token(conn, owner=owner, token=lock.token)
            return []
        if auto_only:
            rows = conn.execute(
                "SELECT id FROM deploy_queue WHERE status = 'queued' AND auto_deploy = 1 ORDER BY id ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM deploy_queue WHERE status = 'queued' ORDER BY id ASC"
            ).fetchall()
        job_ids = [int(row["id"]) for row in rows]
        if not job_ids:
            _release_lock_token(conn, owner=owner, token=lock.token)
            return []
        placeholders = ",".join("?" for _ in job_ids)
        conn.execute(
            f"""
            UPDATE deploy_queue
            SET status = 'in_progress', started_at = ?, note = ?, claim_token = ?,
                cancel_requested_at = ''
            WHERE id IN ({placeholders}) AND status = 'queued'
            """,
            (utc_now(), "claimed by mergetrain batch runner", lock.token, *job_ids),
        )
        _record_run_event(
            conn,
            claim_token=lock.token,
            phase="claiming",
            state="active",
            message=(
                f"{'Deploy' if deploy else 'Validation'} runner claimed "
                f"{len(job_ids)} job(s)"
            ),
            detail=f"mode={'deploy' if deploy else 'validate'}",
        )
    return [get_job(conn, job_id) for job_id in job_ids]


def claim_deploy_batch(
    conn: sqlite3.Connection,
    *,
    owner: str | None = None,
    ttl_minutes: int = 30,
    train_id: str = "",
) -> list[Job]:
    """Claim one exact validated train, or queued jobs when none is pending."""

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
        if selected is not None:
            jobs = validated_jobs
        else:
            jobs = list_jobs_fifo(conn, status="queued")
        if not jobs:
            _release_lock_token(conn, owner=owner, token=lock.token)
            return []
        job_ids = [job.id for job in jobs]
        expected_status = "validated" if selected is not None else "queued"
        placeholders = ",".join("?" for _ in job_ids)
        cur = conn.execute(
            f"""
            UPDATE deploy_queue
            SET status = 'in_progress', started_at = ?, note = ?, claim_token = ?,
                cancel_requested_at = ''
            WHERE id IN ({placeholders}) AND status = ?
            """,
            (
                utc_now(),
                "claimed by mergetrain deploy runner",
                lock.token,
                *job_ids,
                expected_status,
            ),
        )
        if cur.rowcount != len(job_ids):
            raise QueueError("validated train changed while it was being claimed")
        _record_run_event(
            conn,
            claim_token=lock.token,
            phase="claiming",
            state="active",
            message=f"Deploy runner claimed {len(job_ids)} job(s)",
            detail="mode=deploy",
        )
    return [get_job(conn, job_id) for job_id in job_ids]
