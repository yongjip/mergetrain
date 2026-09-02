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
    manual_only: bool = False,
    deploy: bool = False,
    approval_destination_sha: str = "",
) -> list[Job]:
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
            if approval_destination_sha:
                mismatched = conn.execute(
                    """
                    SELECT id FROM deploy_queue
                    WHERE status = 'queued' AND auto_deploy = 1
                      AND approval_destination_sha != ?
                    ORDER BY id ASC
                    """,
                    (approval_destination_sha,),
                ).fetchall()
                for row in mismatched:
                    job_id = int(row["id"])
                    note = (
                        "approval_destination_changed: unattended deploy approval "
                        "does not match the current remote or push refs; enqueue "
                        "again with --auto only after approving this destination"
                    )
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
                        message="Unattended deploy destination changed",
                        detail="approval_destination_changed",
                    )
            rows = conn.execute(
                """
                SELECT id FROM deploy_queue
                WHERE status = 'queued' AND auto_deploy = 1
                  AND (? = '' OR approval_destination_sha = ?)
                ORDER BY id ASC
                """,
                (approval_destination_sha, approval_destination_sha),
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
