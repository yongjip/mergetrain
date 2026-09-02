"""Queue/job reads, mutations, and validated-train persistence."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import (
    CancellationRequested,
    DuplicateActiveBranch,
    LockHeld,
    LostLease,
    QueueError,
)
from ..models import (
    ACTIVE_STATUSES,
    ALL_STATUSES,
    PUSH_STATUSES,
    TERMINAL_STATUSES,
    VERIFY_STATUSES,
    Job,
)
from .events import _record_run_event
from .leases import active_runner_lock
from .transactions import immediate, utc_now


@dataclass(frozen=True, slots=True)
class SupersedeReplacement:
    task: str
    branch: str
    worktree_path: str
    base_sha: str
    head_sha: str
    note: str = ""


def _status_placeholders(statuses: tuple[str, ...] | list[str]) -> str:
    return ",".join("?" for _ in statuses)


def _active_branch_count(conn: sqlite3.Connection, branch: str) -> int:
    placeholders = _status_placeholders(ACTIVE_STATUSES)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM deploy_queue WHERE branch = ? AND status IN ({placeholders})",
        (branch, *ACTIVE_STATUSES),
    ).fetchone()
    return int(row["n"])


def enqueue_job(
    conn: sqlite3.Connection,
    *,
    task: str,
    branch: str,
    worktree_path: str = "",
    base_sha: str = "",
    head_sha: str = "",
    note: str = "",
    allow_duplicate: bool = False,
    auto_deploy: bool = False,
    approval_destination_sha: str = "",
) -> Job:
    task = task.strip()
    branch = branch.strip()
    if not task:
        raise QueueError("--task is required")
    if not branch:
        raise QueueError("--branch is required")
    with immediate(conn):
        if not allow_duplicate and _active_branch_count(conn, branch):
            raise DuplicateActiveBranch(
                f"branch '{branch}' already has an active job. If a job on it is "
                "blocked/failed, dismiss it first (mergetrain dismiss <id>, "
                "non-destructive) then enqueue the fix, or re-enqueue with "
                "--allow-duplicate."
            )
        now = utc_now()
        cur = conn.execute(
            """
            INSERT INTO deploy_queue (
              task, branch, worktree_path, status, base_sha, head_sha,
              requested_at, note, auto_deploy, approval_destination_sha
            ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
            """,
            (
                task,
                branch,
                worktree_path,
                base_sha,
                head_sha,
                now,
                note,
                1 if auto_deploy else 0,
                approval_destination_sha if auto_deploy else "",
            ),
        )
        job_id = cur.lastrowid
        assert job_id is not None  # an INSERT always assigns a rowid
    return get_job(conn, job_id)


def retry_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    base_sha: str,
    head_sha: str,
    current_approval_destination_sha: str | None = None,
) -> tuple[Job, Job]:
    """Atomically dismiss a failed outcome and enqueue its fresh replacement."""

    with immediate(conn):
        row = conn.execute(
            "SELECT * FROM deploy_queue WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise QueueError(f"job not found: {job_id}")
        original = Job.from_row(row)
        if original.status not in {"blocked", "failed"}:
            raise QueueError(
                f"only a blocked or failed job can be retried (job {job_id} is "
                f"{original.status})"
            )
        placeholders = _status_placeholders(ACTIVE_STATUSES)
        other_active = conn.execute(
            f"SELECT COUNT(*) AS n FROM deploy_queue "
            f"WHERE branch = ? AND id != ? AND status IN ({placeholders})",
            (original.branch, original.id, *ACTIVE_STATUSES),
        ).fetchone()
        if int(other_active["n"]):
            raise DuplicateActiveBranch(
                f"branch '{original.branch}' already has an active replacement job"
            )

        now = utc_now()
        approval_matches = (
            current_approval_destination_sha is None
            or (
                bool(original.approval_destination_sha)
                and original.approval_destination_sha
                == current_approval_destination_sha
            )
        )
        inherit_auto = original.auto_deploy and approval_matches
        inherited_destination = (
            original.approval_destination_sha if inherit_auto else ""
        )
        cur = conn.execute(
            """
            INSERT INTO deploy_queue (
              task, branch, worktree_path, status, base_sha, head_sha,
              requested_at, note, auto_deploy, approval_destination_sha
            ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
            """,
            (
                original.task,
                original.branch,
                original.worktree_path,
                base_sha,
                head_sha,
                now,
                original.note,
                1 if inherit_auto else 0,
                inherited_destination,
            ),
        )
        replacement_id = cur.lastrowid
        assert replacement_id is not None
        dismissed = conn.execute(
            """
            UPDATE deploy_queue
            SET status = 'canceled', finished_at = ?,
                note = ?, claim_token = '', cancel_requested_at = '',
                pending_deploy_sha = '', pending_deploy_remote = '',
                pending_deploy_refs = '', conflict_with = ''
            WHERE id = ? AND status = ?
            """,
            (
                now,
                f"retried as job {replacement_id}",
                job_id,
                original.status,
            ),
        )
        if dismissed.rowcount != 1:
            raise QueueError(
                f"job {job_id} left '{original.status}' before retry was recorded"
            )
    return get_job(conn, job_id), get_job(conn, replacement_id)


def supersede_validated_train(
    conn: sqlite3.Connection,
    train_id: str,
    replacements: Sequence[SupersedeReplacement],
) -> dict[str, Any]:
    """Atomically retire one immutable validation identity and enqueue another.

    Git readiness and SHA capture happen before this store boundary. This
    transaction re-checks queue ownership, validated membership, and active
    branch uniqueness before changing any row. Approval and validation fields
    are deliberately not copied to replacement jobs.
    """

    train_id = train_id.strip()
    if not train_id:
        raise QueueError("--train-id is required")
    if not replacements:
        raise QueueError("at least one --replacement is required")
    normalized: list[SupersedeReplacement] = []
    seen_branches: set[str] = set()
    for replacement in replacements:
        task = replacement.task.strip()
        branch = replacement.branch.strip()
        if not task:
            raise QueueError("replacement task is required")
        if not branch:
            raise QueueError("replacement branch is required")
        if branch in seen_branches:
            raise DuplicateActiveBranch(
                f"replacement branch '{branch}' is listed more than once"
            )
        seen_branches.add(branch)
        if not replacement.base_sha or not replacement.head_sha:
            raise QueueError(
                f"replacement branch '{branch}' needs captured base and head SHAs"
            )
        normalized.append(
            SupersedeReplacement(
                task=task,
                branch=branch,
                worktree_path=replacement.worktree_path,
                base_sha=replacement.base_sha,
                head_sha=replacement.head_sha,
                note=replacement.note,
            )
        )

    with immediate(conn):
        lock = active_runner_lock(conn)
        if lock is not None:
            raise LockHeld(
                f"runner lock held by {lock.owner}; wait before superseding "
                f"validated train {train_id}"
            )
        rows = conn.execute(
            "SELECT * FROM deploy_queue WHERE train_id = ? ORDER BY id ASC",
            (train_id,),
        ).fetchall()
        if not rows:
            raise QueueError(f"validated train not found: {train_id}")
        old_jobs = [Job.from_row(row) for row in rows]
        if any(job.status != "validated" for job in old_jobs):
            states = ", ".join(
                f"#{job.id}={job.status}" for job in old_jobs
            )
            raise QueueError(
                f"train {train_id} is not wholly validated ({states})"
            )
        old_ids = [job.id for job in old_jobs]
        old_placeholders = ",".join("?" for _ in old_ids)
        active_placeholders = _status_placeholders(ACTIVE_STATUSES)
        for replacement in normalized:
            row = conn.execute(
                f"SELECT id FROM deploy_queue "
                f"WHERE branch = ? AND id NOT IN ({old_placeholders}) "
                f"AND status IN ({active_placeholders}) LIMIT 1",
                (
                    replacement.branch,
                    *old_ids,
                    *ACTIVE_STATUSES,
                ),
            ).fetchone()
            if row is not None:
                raise DuplicateActiveBranch(
                    f"branch '{replacement.branch}' already has active job "
                    f"{int(row['id'])}"
                )

        supersession_id = uuid.uuid4().hex
        now = utc_now()
        replacement_ids: list[int] = []
        for replacement in normalized:
            cur = conn.execute(
                """
                INSERT INTO deploy_queue (
                  task, branch, worktree_path, status, base_sha, head_sha,
                  requested_at, note, auto_deploy, supersession_id,
                  supersedes_train_id
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    replacement.task,
                    replacement.branch,
                    replacement.worktree_path,
                    replacement.base_sha,
                    replacement.head_sha,
                    now,
                    replacement.note,
                    supersession_id,
                    train_id,
                ),
            )
            replacement_id = cur.lastrowid
            assert replacement_id is not None
            replacement_ids.append(int(replacement_id))

        replacement_text = ",".join(str(job_id) for job_id in replacement_ids)
        audit_note = (
            f"superseded by job(s) {replacement_text}; "
            f"supersession {supersession_id}"
        )
        updated = conn.execute(
            f"""
            UPDATE deploy_queue
            SET status = 'canceled', finished_at = ?,
                note = CASE WHEN note = '' THEN ? ELSE note || '\n' || ? END,
                supersession_id = ?
            WHERE id IN ({old_placeholders}) AND status = 'validated'
            """,
            (now, audit_note, audit_note, supersession_id, *old_ids),
        )
        if updated.rowcount != len(old_ids):
            raise QueueError(
                f"validated train {train_id} changed while being superseded"
            )

        detail = (
            f"supersession_id={supersession_id};"
            f"superseded_train_id={train_id};"
            f"replacement_job_ids={replacement_text}"
        )
        for old_job in old_jobs:
            _record_run_event(
                conn,
                job_id=old_job.id,
                phase="superseding",
                state="success",
                message=(
                    f"Validated train {train_id} superseded by "
                    f"job(s) {replacement_text}"
                ),
                detail=detail,
            )
        for replacement_id in replacement_ids:
            _record_run_event(
                conn,
                job_id=replacement_id,
                phase="superseding",
                state="queued",
                message=(
                    f"Job #{replacement_id} replaces validated train "
                    f"{train_id}"
                ),
                detail=detail,
            )

    return {
        "supersession_id": supersession_id,
        "superseded_train_id": train_id,
        "superseded_jobs": [get_job(conn, job_id) for job_id in old_ids],
        "replacement_jobs": [
            get_job(conn, job_id) for job_id in replacement_ids
        ],
    }


def get_job(conn: sqlite3.Connection, job_id: int) -> Job:
    row = conn.execute("SELECT * FROM deploy_queue WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise QueueError(f"job not found: {job_id}")
    return Job.from_row(row)


def list_jobs(conn: sqlite3.Connection, *, limit: int = 50) -> list[Job]:
    rows = conn.execute(
        "SELECT * FROM deploy_queue ORDER BY id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def list_attention_jobs(conn: sqlite3.Connection) -> list[Job]:
    """Return every job that may require a runner or operator decision.

    This intentionally has no display limit. A recent-history cap must not
    hide an older blocked train, pending reconcile, or unresolved post-push
    verification from the compact status view.
    """

    rows = conn.execute(
        """
        SELECT * FROM deploy_queue
        WHERE status IN (
          'queued', 'in_progress', 'blocked', 'failed', 'validated',
          'needs_reconcile'
        )
        OR (status = 'deployed' AND verify_status = 'unknown')
        ORDER BY id DESC
        """
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def list_history_jobs(
    conn: sqlite3.Connection,
    *,
    since: str = "",
    limit: int | None = None,
) -> list[Job]:
    """Read durable job history, optionally keeping complete recent trains."""

    observed_at = (
        "COALESCE(NULLIF(finished_at, ''), NULLIF(started_at, ''), requested_at)"
    )
    if limit is None:
        where = f"WHERE {observed_at} >= ?" if since else ""
        values: tuple[Any, ...] = (since,) if since else ()
        rows = conn.execute(
            f"SELECT * FROM deploy_queue {where} ORDER BY id DESC",
            values,
        ).fetchall()
        return [Job.from_row(row) for row in rows]

    since_clause = "WHERE observed_at >= ?" if since else ""
    values = (since, int(limit)) if since else (int(limit),)
    rows = conn.execute(
        f"""
        WITH history AS (
          SELECT *, {observed_at} AS observed_at,
                 CASE WHEN train_id != '' THEN 'train:' || train_id
                      ELSE 'job:' || id END AS history_key
          FROM deploy_queue
        ), recent AS (
          SELECT history_key, MAX(id) AS latest_id
          FROM history
          {since_clause}
          GROUP BY history_key
          ORDER BY latest_id DESC
          LIMIT ?
        )
        SELECT history.*
        FROM history JOIN recent USING (history_key)
        ORDER BY recent.latest_id DESC, history.id ASC
        """,
        values,
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def list_dismissable_jobs(conn: sqlite3.Connection) -> list[Job]:
    """Return every blocked/failed job, without the status display cap."""

    rows = conn.execute(
        "SELECT * FROM deploy_queue WHERE status IN ('blocked', 'failed') "
        "ORDER BY id ASC"
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def list_verify_unknown_jobs(conn: sqlite3.Connection) -> list[Job]:
    """Deployed jobs whose post-push verify never resolved (crash recovery)."""
    rows = conn.execute(
        "SELECT * FROM deploy_queue WHERE status = 'deployed' AND verify_status = 'unknown' "
        "ORDER BY id ASC"
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def resolve_verify_status(
    conn: sqlite3.Connection, job_id: int, *, verify_status: str, note: str = ""
) -> Job:
    """Discharge a deployed job's unresolved post-push verify (``mergetrain
    verify``). Only moves a deployed+unknown job to succeeded/failed — never
    reopens a terminal job or touches its deployed status."""
    if verify_status not in {"succeeded", "failed"}:
        raise QueueError(f"verify_status must be 'succeeded' or 'failed', got {verify_status!r}")
    with immediate(conn):
        row = conn.execute(
            "SELECT status, verify_status FROM deploy_queue WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise QueueError(f"job not found: {job_id}")
        if str(row["status"]) != "deployed" or str(row["verify_status"]) != "unknown":
            raise QueueError(
                f"job {job_id} is not an unresolved verify (status={row['status']}, "
                f"verify_status={row['verify_status']})"
            )
        conn.execute(
            "UPDATE deploy_queue SET verify_status = ?, note = COALESCE(NULLIF(?, ''), note) "
            "WHERE id = ?",
            (verify_status, note, job_id),
        )
    return get_job(conn, job_id)


def list_train_jobs(conn: sqlite3.Connection, train_id: str) -> list[Job]:
    if not train_id:
        return []
    rows = conn.execute(
        "SELECT * FROM deploy_queue WHERE train_id = ? ORDER BY id ASC",
        (train_id,),
    ).fetchall()
    return [Job.from_row(row) for row in rows]


def list_jobs_fifo(conn: sqlite3.Connection, *, status: str = "queued", auto_only: bool = False) -> list[Job]:
    if auto_only:
        rows = conn.execute(
            "SELECT * FROM deploy_queue WHERE status = ? AND auto_deploy = 1 ORDER BY id ASC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM deploy_queue WHERE status = ? ORDER BY id ASC",
            (status,),
        ).fetchall()
    return [Job.from_row(row) for row in rows]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued,
          SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) AS in_progress,
          SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked,
          SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
          SUM(CASE WHEN status = 'validated' THEN 1 ELSE 0 END) AS validated,
          SUM(CASE WHEN status = 'needs_reconcile' THEN 1 ELSE 0 END) AS needs_reconcile,
          SUM(CASE WHEN status = 'deployed' THEN 1 ELSE 0 END) AS deployed,
          SUM(CASE WHEN status = 'canceled' THEN 1 ELSE 0 END) AS canceled,
          SUM(CASE WHEN status = 'queued' AND auto_deploy = 1 THEN 1 ELSE 0 END)
            AS auto_queued,
          SUM(CASE WHEN status = 'queued' AND auto_deploy = 0 THEN 1 ELSE 0 END)
            AS manual_queued,
          SUM(CASE WHEN status = 'in_progress' AND pending_deploy_sha != ''
              THEN 1 ELSE 0 END) AS in_progress_with_marker,
          SUM(CASE WHEN status = 'blocked' AND pending_deploy_sha != ''
              THEN 1 ELSE 0 END) AS blocked_with_marker,
          SUM(CASE WHEN status = 'deployed' AND verify_status = 'unknown'
              THEN 1 ELSE 0 END) AS deployed_verify_unknown
        FROM deploy_queue
        """
    ).fetchone()
    keys = (
        *ALL_STATUSES,
        "auto_queued",
        "manual_queued",
        "in_progress_with_marker",
        "blocked_with_marker",
        "deployed_verify_unknown",
    )
    return {key: int(row[key] or 0) for key in keys}


def has_queued_auto(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM deploy_queue WHERE status = 'queued' AND auto_deploy = 1 LIMIT 1"
    ).fetchone()
    return row is not None


def has_queued_manual(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM deploy_queue WHERE status = 'queued' AND auto_deploy = 0 LIMIT 1"
    ).fetchone()
    return row is not None


def validated_train_summaries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Describe pending validated trains and whether their identity is complete."""

    rows = conn.execute(
        "SELECT * FROM deploy_queue WHERE status = 'validated' ORDER BY id ASC"
    ).fetchall()
    groups: dict[str, list[Job]] = {}
    for row in rows:
        job = Job.from_row(row)
        key = job.train_id or f"legacy-job-{job.id}"
        groups.setdefault(key, []).append(job)

    summaries: list[dict[str, Any]] = []
    for jobs in groups.values():
        first = jobs[0]
        train_sizes = {job.train_size for job in jobs}
        validated_times = {job.validated_at for job in jobs}
        base_shas = {job.validation_base_sha for job in jobs}
        validation_shas = {job.validation_sha for job in jobs}
        validation_tree_shas = {job.validation_tree_sha for job in jobs}
        gate_policy_shas = {job.validation_gate_policy_sha for job in jobs}
        environment_shas = {job.validation_environment_sha for job in jobs}
        train_identity_shas = {job.validation_train_sha for job in jobs}
        expected_size = first.train_size
        complete = bool(
            first.train_id
            and expected_size == len(jobs)
            and len(train_sizes) == 1
            and len(validated_times) == 1
            and len(base_shas) == 1
            and len(validation_shas) == 1
            and first.validated_at
            and first.validation_base_sha
            and first.validation_sha
            and all(job.validated_head_sha for job in jobs)
        )
        reuse_identity_complete = bool(
            complete
            and len(validation_tree_shas) == 1
            and len(gate_policy_shas) == 1
            and len(environment_shas) == 1
            and len(train_identity_shas) == 1
            and first.validation_tree_sha
            and first.validation_gate_policy_sha
            and first.validation_environment_sha
            and first.validation_train_sha
        )
        summaries.append(
            {
                "train_id": first.train_id or None,
                "train_size": expected_size,
                "job_ids": [job.id for job in jobs],
                "branches": [
                    {
                        "job_id": job.id,
                        "branch": job.branch,
                        "validated_head_sha": job.validated_head_sha,
                    }
                    for job in jobs
                ],
                "validated_at": first.validated_at,
                "validation_base_sha": first.validation_base_sha,
                "validation_sha": first.validation_sha,
                "deploy_eligible": complete,
                "reuse_identity_complete": reuse_identity_complete,
                "validation_tree_sha": first.validation_tree_sha,
                "validation_gate_policy_sha": first.validation_gate_policy_sha,
                "validation_environment_sha": first.validation_environment_sha,
                "validation_train_sha": first.validation_train_sha,
            }
        )
    return summaries


def select_validated_train(
    conn: sqlite3.Connection,
    *,
    train_id: str = "",
) -> tuple[dict[str, Any] | None, list[Job]]:
    """Select one complete validated train without claiming or mutating it."""

    summaries = validated_train_summaries(conn)
    if train_id:
        matches = [summary for summary in summaries if summary["train_id"] == train_id]
        if not matches:
            raise QueueError(f"validated train not found: {train_id}")
        selected: dict[str, Any] | None = matches[0]
    else:
        deployable = [summary for summary in summaries if summary["deploy_eligible"]]
        if len(deployable) > 1:
            ids = ", ".join(str(summary["train_id"]) for summary in deployable)
            raise QueueError(
                f"multiple validated trains are ready; pass --train-id with one of: {ids}"
            )
        selected = deployable[0] if deployable else None
        if selected is None and summaries:
            raise QueueError(
                "validated jobs lack complete train identity; cancel and enqueue a fresh train"
            )
    if selected is None:
        return None, []
    if not selected["deploy_eligible"]:
        raise QueueError(
            f"validated train has incomplete identity: {selected['train_id']}"
        )
    jobs = list_jobs_fifo(conn, status="validated")
    return selected, [job for job in jobs if job.train_id == selected["train_id"]]


def mark_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    status: str,
    deploy_sha: str = "",
    log_path: str = "",
    note: str = "",
    push_status: str = "",
    verify_status: str = "",
    train_id: str = "",
    train_size: int = 0,
    validated_at: str = "",
    validation_base_sha: str = "",
    validation_sha: str = "",
    validated_head_sha: str = "",
    validation_tree_sha: str = "",
    validation_gate_policy_sha: str = "",
    validation_environment_sha: str = "",
    validation_train_sha: str = "",
    reused_validation_sha: str = "",
    conflict_with: str = "",
    expected_claim_token: str | None = None,
    expected_status: str = "",
) -> Job:
    if status not in ALL_STATUSES:
        raise QueueError(f"unknown job status: {status}")
    if push_status and push_status not in PUSH_STATUSES:
        raise QueueError(f"unknown push status: {push_status}")
    if verify_status and verify_status not in VERIFY_STATUSES:
        raise QueueError(f"unknown verify status: {verify_status}")
    finished_at = utc_now() if status in TERMINAL_STATUSES or status in {"blocked", "failed"} else ""
    where = "id = ?"
    where_values: list[Any] = [job_id]
    if expected_claim_token is not None:
        if not expected_claim_token:
            raise LostLease("job claim token is missing")
        where += " AND status = 'in_progress' AND claim_token = ?"
        where_values.append(expected_claim_token)
        # A marker-bearing ambiguous push must park for reconcile even when a
        # cancel arrived during remote I/O.  Reconcile, not the cancel request,
        # decides whether the push landed.  Preserve the request below so an
        # unlanded push can still honor it.
        if status not in {"canceled", "deployed", "needs_reconcile"}:
            where += " AND cancel_requested_at = ''"
    if expected_status:
        # Compare-and-swap on the source status, so a concurrent transition (e.g.
        # a cancel landing during reconcile's multi-second remote I/O) is never
        # silently overwritten by a stale recovery decision.
        where += " AND status = ?"
        where_values.append(expected_status)
    with immediate(conn):
        cur = conn.execute(
            f"""
            UPDATE deploy_queue
            SET status = ?, deploy_sha = COALESCE(NULLIF(?, ''), deploy_sha),
                log_path = COALESCE(NULLIF(?, ''), log_path), note = ?, finished_at = ?,
                push_status = COALESCE(NULLIF(?, ''), push_status),
                verify_status = COALESCE(NULLIF(?, ''), verify_status),
                train_id = COALESCE(NULLIF(?, ''), train_id),
                train_size = COALESCE(NULLIF(?, 0), train_size),
                validated_at = COALESCE(NULLIF(?, ''), validated_at),
                validation_base_sha = COALESCE(NULLIF(?, ''), validation_base_sha),
                validation_sha = COALESCE(NULLIF(?, ''), validation_sha),
                validated_head_sha = COALESCE(NULLIF(?, ''), validated_head_sha),
                validation_tree_sha = COALESCE(NULLIF(?, ''), validation_tree_sha),
                validation_gate_policy_sha = COALESCE(NULLIF(?, ''), validation_gate_policy_sha),
                validation_environment_sha = COALESCE(NULLIF(?, ''), validation_environment_sha),
                validation_train_sha = COALESCE(NULLIF(?, ''), validation_train_sha),
                reused_validation_sha = COALESCE(NULLIF(?, ''), reused_validation_sha),
                conflict_with = ?,
                claim_token = CASE WHEN ? = 'in_progress' THEN claim_token ELSE '' END,
                cancel_requested_at = CASE
                    WHEN ? IN ('in_progress', 'canceled', 'needs_reconcile')
                    THEN cancel_requested_at
                    ELSE ''
                END,
                pending_deploy_sha = CASE
                    WHEN ? IN ('deployed', 'canceled', 'queued') THEN ''
                    ELSE pending_deploy_sha
                END,
                pending_deploy_remote = CASE
                    WHEN ? IN ('deployed', 'canceled', 'queued') THEN ''
                    ELSE pending_deploy_remote
                END,
                pending_deploy_refs = CASE
                    WHEN ? IN ('deployed', 'canceled', 'queued') THEN ''
                    ELSE pending_deploy_refs
                END
            WHERE {where}
            """,
            (
                status,
                deploy_sha,
                log_path,
                note,
                finished_at,
                push_status,
                verify_status,
                train_id,
                train_size,
                validated_at,
                validation_base_sha,
                validation_sha,
                validated_head_sha,
                validation_tree_sha,
                validation_gate_policy_sha,
                validation_environment_sha,
                validation_train_sha,
                reused_validation_sha,
                conflict_with,
                status,
                status,
                status,
                status,
                status,
                *where_values,
            ),
        )
        if cur.rowcount != 1:
            row = conn.execute(
                "SELECT status, claim_token, cancel_requested_at FROM deploy_queue WHERE id = ?",
                (job_id,),
            ).fetchone()
            if (
                row is not None
                and expected_claim_token is not None
                and str(row["status"]) == "in_progress"
                and str(row["claim_token"]) == expected_claim_token
                and str(row["cancel_requested_at"] or "")
            ):
                raise CancellationRequested(f"cancellation requested for job {job_id}")
            if expected_status and expected_claim_token is None:
                raise QueueError(
                    f"job {job_id} left '{expected_status}' before the write "
                    "(raced by a concurrent transition)"
                )
            raise LostLease(f"job {job_id} is no longer owned by this runner")
        if status == "queued":
            # Queued means a fresh attempt. Validation identity and outcome
            # fields belong to the previous attempt and must not leak into a
            # later batch, where a stale train_id can collateral-block new work.
            conn.execute(
                """
                UPDATE deploy_queue
                SET started_at = '', deploy_sha = '',
                    push_status = 'not_run', verify_status = 'not_run',
                    train_id = '', train_size = 0, validated_at = '',
                    validation_base_sha = '', validation_sha = '',
                    validated_head_sha = '', validation_tree_sha = '',
                    validation_gate_policy_sha = '',
                    validation_environment_sha = '', validation_train_sha = '',
                    reused_validation_sha = ''
                WHERE id = ?
                """,
                (job_id,),
            )
    return get_job(conn, job_id)


def cancel_job(conn: sqlite3.Connection, job_id: int, *, note: str = "") -> Job:
    job = get_job(conn, job_id)
    if job.status in TERMINAL_STATUSES:
        raise QueueError(f"terminal job cannot be canceled: {job_id}")
    if job.status == "needs_reconcile":
        raise QueueError(
            f"job {job_id} has an unresolved push; run 'mergetrain reconcile --apply' "
            "before canceling"
        )
    if job.status == "in_progress":
        requested_at = utc_now()
        cancel_note = note or "cancellation requested by user"
        with immediate(conn):
            current = conn.execute(
                "SELECT status, claim_token FROM deploy_queue WHERE id = ?",
                (job_id,),
            ).fetchone()
            if (
                current is None
                or str(current["status"]) != "in_progress"
                or str(current["claim_token"] or "") != job.claim_token
            ):
                raise QueueError(
                    f"job {job_id} left 'in_progress' before the cancellation "
                    "request was recorded"
                )
            if job.claim_token:
                expected = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM deploy_queue "
                        "WHERE status = 'in_progress' AND claim_token = ?",
                        (job.claim_token,),
                    ).fetchone()[0]
                )
                cur = conn.execute(
                    """
                    UPDATE deploy_queue SET cancel_requested_at = ?,
                        note = CASE WHEN id = ? THEN ? ELSE note END
                    WHERE status = 'in_progress' AND claim_token = ?
                    """,
                    (requested_at, job_id, cancel_note, job.claim_token),
                )
            else:
                expected = 1
                cur = conn.execute(
                    """
                    UPDATE deploy_queue
                    SET cancel_requested_at = ?, note = ?
                    WHERE id = ? AND status = 'in_progress'
                    """,
                    (requested_at, cancel_note, job_id),
                )
            if cur.rowcount != expected:
                raise QueueError(
                    f"active train changed while canceling job {job_id}"
                )
        return get_job(conn, job_id)
    if job.status == "validated" and job.train_id:
        cancel_note = note or f"validated train {job.train_id} canceled by user"
        with immediate(conn):
            conn.execute(
                """
                UPDATE deploy_queue
                SET status = 'canceled', note = ?, finished_at = ?
                WHERE status = 'validated' AND train_id = ?
                """,
                (cancel_note, utc_now(), job.train_id),
            )
        return get_job(conn, job_id)
    return mark_job(
        conn,
        job_id,
        status="canceled",
        note=note or "canceled by user",
        expected_status=job.status,
    )


def dismiss_job(conn: sqlite3.Connection, job_id: int, *, note: str = "") -> Job:
    """Non-destructively clear a blocked/failed job that has been superseded.

    A blocked/failed job never lands and never self-clears, yet it keeps
    ``doctor``'s ``next_action`` pinned to ``fix_blocked_job`` — hiding a
    ready validated train — and blocks re-enqueue of its branch. Once its work
    is fixed (and enqueued afresh, or abandoned), dismissing it moves it to the
    terminal ``canceled`` state so the queue reflects reality. Unlike
    ``cancel``, this only ever touches an already-failed outcome — never queued
    or in-progress work — so it is safe for an agent to run unattended.
    """

    job = get_job(conn, job_id)
    if job.status not in {"blocked", "failed"}:
        raise QueueError(
            f"only a blocked or failed job can be dismissed (job {job_id} is "
            f"{job.status}); use cancel for queued/in-progress work"
        )
    return mark_job(
        conn,
        job_id,
        status="canceled",
        note=note or f"dismissed superseded {job.status} job",
        expected_status=job.status,
    )


def terminal_branch_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    terminal_placeholders = _status_placeholders(TERMINAL_STATUSES)
    active_placeholders = _status_placeholders(ACTIVE_STATUSES)
    rows = conn.execute(
        f"""
        SELECT terminal.branch, terminal.id AS job_id, terminal.status
        FROM deploy_queue AS terminal
        WHERE terminal.status IN ({terminal_placeholders})
          AND terminal.id = (
            SELECT MAX(latest.id)
            FROM deploy_queue AS latest
            WHERE latest.branch = terminal.branch
              AND latest.status IN ({terminal_placeholders})
          )
          AND NOT EXISTS (
            SELECT 1
            FROM deploy_queue AS active
            WHERE active.branch = terminal.branch
              AND active.status IN ({active_placeholders})
          )
        ORDER BY terminal.id ASC
        """,
        (*TERMINAL_STATUSES, *TERMINAL_STATUSES, *ACTIVE_STATUSES),
    ).fetchall()
    # job_id is an int here to match the same key under gc's own
    # result.swept_pending_refs; a string made a join across one payload
    # return nothing, and the fingerprint gate cannot see value types.
    return [
        {"branch": str(row["branch"]), "job_id": int(row["job_id"]), "status": str(row["status"])}
        for row in rows
    ]
