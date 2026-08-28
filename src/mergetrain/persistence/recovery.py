"""Durable pre-push markers and deploy-reconciliation persistence guards."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from ..errors import LostLease, QueueError
from .leases import active_runner_lock
from .transactions import immediate


def deploy_reconcile_pending(conn: sqlite3.Connection) -> int:
    """Count jobs that make a deploy unsafe: parked reconciles plus not-yet-split
    marker-bearing orphans. A deploy targets the same push refs, so every deploy
    entrypoint (``run-batch``, ``run-next``, and the daemon) must refuse while
    this is non-zero (0.3.0 Phase 2, decision Q4)."""
    active = active_runner_lock(conn)
    active_token = active.token if active is not None else ""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM deploy_queue
        WHERE status = 'needs_reconcile'
           OR (
                status = 'in_progress'
                AND pending_deploy_sha != ''
                AND (? = '' OR claim_token != ?)
           )
        """,
        (active_token, active_token),
    ).fetchone()
    return int(row["n"])


def pack_push_refs(push_refs: Sequence[str]) -> str:
    """Normalize a push-ref set into the durable marker's newline-joined form."""
    return "\n".join(str(ref) for ref in push_refs)


def unpack_push_refs(packed: str) -> list[str]:
    """Inverse of :func:`pack_push_refs`; ``[]`` for an empty/legacy marker."""
    return [ref for ref in packed.split("\n") if ref] if packed else []


def record_pending_push(
    conn: sqlite3.Connection,
    *,
    job_ids: Sequence[int],
    deploy_sha: str,
    claim_token: str,
    remote: str = "",
    push_refs: Sequence[str] = (),
) -> None:
    """Durably record intent to push ``deploy_sha`` before the remote is touched.

    Writes ``pending_deploy_sha`` and ``push_status='pending'`` for exactly the
    in-progress jobs this runner owns, in one IMMEDIATE transaction. With
    ``PRAGMA synchronous=FULL`` the commit is fsync-durable before ``git push``,
    so a later crash can prove a push was attempted for this sha (0.3.0 Phase 1;
    see docs/proposals/0.3.0-recovery.md).

    The push *target* — the remote and the normalized push-ref set — is recorded
    alongside the sha so a later ``reconcile`` evaluates the refs the interrupted
    push actually targeted, not whatever the current config now says (#84,
    defect 3).
    """
    ids = list(dict.fromkeys(int(job_id) for job_id in job_ids))
    if not ids:
        return
    if not deploy_sha:
        raise QueueError("pending push deploy sha is missing")
    placeholders = ",".join("?" for _ in ids)
    with immediate(conn):
        cur = conn.execute(
            f"""
            UPDATE deploy_queue
            SET pending_deploy_sha = ?, push_status = 'pending',
                pending_deploy_remote = ?, pending_deploy_refs = ?
            WHERE id IN ({placeholders})
              AND status = 'in_progress'
              AND claim_token = ?
            """,
            (deploy_sha, remote, pack_push_refs(push_refs), *ids, claim_token),
        )
        if cur.rowcount != len(ids):
            raise LostLease("pending push is no longer owned by this runner")


def clear_rejected_push(
    conn: sqlite3.Connection,
    *,
    job_ids: Sequence[int],
    claim_token: str,
) -> None:
    """Clear a pending marker after an unambiguous remote rejection.

    A protected-branch or permission rejection proves that no ref update landed,
    so retaining the write-ahead marker would misclassify the eventual ``blocked``
    row as a reconcile conflict.  Fence the cleanup to the runner that recorded
    the marker; a stale owner must never erase another runner's recovery evidence.
    """

    ids = list(dict.fromkeys(int(job_id) for job_id in job_ids))
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    with immediate(conn):
        cur = conn.execute(
            f"""
            UPDATE deploy_queue
            SET pending_deploy_sha = '', pending_deploy_remote = '',
                pending_deploy_refs = '', push_status = 'failed'
            WHERE id IN ({placeholders})
              AND status = 'in_progress'
              AND claim_token = ?
            """,
            (*ids, claim_token),
        )
        if cur.rowcount != len(ids):
            raise LostLease("pending push is no longer owned by this runner")
