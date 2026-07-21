"""Crash-safe recovery: marker-aware reconcile / recover / unlock (0.3.0 Phase 2).

The one irreversible deploy step is ``git push --atomic``. Between that push and
the final ``mark_job(deployed)`` there is a window where a crash leaves the
remote advanced but the DB still saying ``in_progress``. Phase 1 writes a
durable ``pending_deploy_sha`` marker (and a ``refs/mergetrain/pending/<id>``
pin ref) *before* the push. This module reads that marker back and asks the
**remote** for truth — never guessing, never re-pushing a deploy that already
landed, never marking ``deployed`` unless a configured push ref actually carries
the sha. See docs/proposals/0.3.0-recovery.md (§4, §5, §6).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .config import MergetrainConfig
from .errors import RemoteUnreachable
from .git_runner import (
    PENDING_REF_PREFIX,
    apply_gc,
    delete_pending_ref,
    git_output_or_empty,
    git_ref_exists,
    resolve_pending_ref,
    run_command,
)
from .models import Job
from .store import (
    acquire_runner_lock,
    counts,
    default_owner,
    force_clear_lock_and_split,
    get_lock,
    list_jobs_fifo,
    mark_job,
    record_run_event,
    release_runner_lock,
)

# --------------------------------------------------------------------------- #
# git primitives — all reuse run_command(check=False) so a non-zero return is a
# datum, not an exception (git_runner has no ls-remote / merge-base wrappers).
# --------------------------------------------------------------------------- #


def _fetch(config: MergetrainConfig) -> bool:
    """``git fetch`` the configured remote; ``True`` iff it is reachable."""
    completed = run_command(
        ["git", "fetch", config.git.remote], cwd=config.repo, check=False
    )
    return completed.returncode == 0


def _localize_ref(config: MergetrainConfig, ref: str) -> None:
    """Best-effort: bring a push ref's current remote tip into the local object
    store so ``merge-base --is-ancestor`` can resolve it. A bare ``git fetch``
    only downloads ``refs/heads/*``; a push ref under any other namespace
    (``refs/deploy/*``, a tag, …) would otherwise be a non-local object and the
    ancestry test would error. Absent refs simply no-op (``check=False``)."""
    run_command(
        ["git", "fetch", config.git.remote, ref], cwd=config.repo, check=False
    )


def _ls_remote(config: MergetrainConfig, ref: str) -> tuple[bool, str]:
    """Resolve ``ref`` on the remote by **exact** name.

    Returns ``(reachable, remote_sha)``. A reachable remote that does not carry
    the exact push ref yields ``(True, "")`` — never a sibling ref's sha.
    ``git ls-remote <remote> main`` is a tail/suffix match, so it can also return
    ``refs/tags/main`` when ``refs/heads/main`` is absent; attributing that sha to
    the push ref would let reconcile mark a job ``deployed`` off a ref the deploy
    never touched. Only an exact match on ``refs/heads/<ref>`` (or a
    fully-qualified ``ref``) counts; peeled ``^{}`` tag lines are skipped.
    """
    completed = run_command(
        ["git", "ls-remote", config.git.remote, ref], cwd=config.repo, check=False
    )
    if completed.returncode != 0:
        return False, ""
    target = ref if ref.startswith("refs/") else f"refs/heads/{ref}"
    for line in completed.stdout.strip().splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 2:
            continue
        sha, name = parts[0].strip(), parts[1].strip()
        if name.endswith("^{}"):  # peeled tag object, not the ref itself
            continue
        if name == target:
            return True, sha
    return True, ""  # reachable, but the exact push ref is absent


def _ancestor_state(config: MergetrainConfig, sha: str, remote_sha: str) -> str:
    """Whether ``remote_sha``'s history contains ``sha``: ``yes`` / ``no`` / ``unknown``.

    ``git merge-base --is-ancestor`` inverts intuition — rc 0 = ancestor, rc 1 =
    not — and returns rc >1 (e.g. 128) when an operand is not a local object. That
    error is **not** a definitive "no": treating it as such could requeue and
    re-push a deploy that already landed. It maps to ``unknown`` so reconcile
    refuses to guess (routes the job to ``blocked``) rather than lie.
    """
    if not remote_sha:
        return "no"  # the push ref is absent on the remote → it does not carry sha
    if not sha or not git_ref_exists(config.repo, remote_sha):
        return "unknown"  # remote tip not resolvable locally → cannot determine
    completed = run_command(
        ["git", "merge-base", "--is-ancestor", sha, remote_sha],
        cwd=config.repo,
        check=False,
    )
    if completed.returncode == 0:
        return "yes"
    if completed.returncode == 1:
        return "no"
    return "unknown"


def _resolvable(config: MergetrainConfig, job: Job) -> bool:
    """Whether the pending sha is still an object in the store.

    The pin ref keeps it alive across a ``git gc``; if both the pin ref is gone
    and the sha is unresolvable, reconcile refuses to guess (routes to blocked).
    """
    if resolve_pending_ref(config.repo, job.id):
        return True
    return bool(job.pending_deploy_sha) and git_ref_exists(
        config.repo, job.pending_deploy_sha
    )


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RefVerdict:
    ref: str
    remote_sha: str
    contains: bool


@dataclass(slots=True)
class JobDecision:
    job: Job
    pending_sha: str
    resolvable: bool
    refs: list[RefVerdict]
    decision: str  # deployed | queued | canceled | blocked
    reason: str


def _classify(
    config: MergetrainConfig, job: Job, ref_shas: dict[str, str]
) -> JobDecision:
    pending = job.pending_deploy_sha
    resolvable = _resolvable(config, job)
    refs: list[RefVerdict] = []
    unknown = False
    for ref, remote_sha in ref_shas.items():
        state = _ancestor_state(config, pending, remote_sha) if resolvable else "no"
        if state == "unknown":
            unknown = True
        refs.append(RefVerdict(ref, remote_sha, state == "yes"))
    if not resolvable:
        return JobDecision(
            job,
            pending,
            False,
            refs,
            "blocked",
            "pending deploy sha is unresolvable (pin ref gone and object pruned)",
        )
    if unknown:
        return JobDecision(
            job,
            pending,
            True,
            refs,
            "blocked",
            "cannot determine remote containment for a push ref (tip unresolvable); refusing to guess",
        )
    contained = [verdict.contains for verdict in refs]
    if refs and all(contained):
        reason = "push landed: deploy sha present on every push ref"
        if job.cancel_requested_at:
            reason += "; late cancel ignored (the push had already landed)"
        return JobDecision(job, pending, True, refs, "deployed", reason)
    if not any(contained):
        if job.cancel_requested_at:
            return JobDecision(
                job,
                pending,
                True,
                refs,
                "canceled",
                "push did not land; late cancel honored",
            )
        return JobDecision(
            job,
            pending,
            True,
            refs,
            "queued",
            "push did not land; requeued for a fresh deploy",
        )
    return JobDecision(
        job,
        pending,
        True,
        refs,
        "blocked",
        "deploy sha present on some but not all push refs (mixed remote state)",
    )


def _apply(config: MergetrainConfig, conn: sqlite3.Connection, decision: JobDecision) -> None:
    job = decision.job
    if decision.decision == "deployed":
        mark_job(
            conn,
            job.id,
            status="deployed",
            deploy_sha=decision.pending_sha,
            push_status="succeeded",
            verify_status="unknown",
            note=f"reconciled: {decision.reason}",
        )
        delete_pending_ref(config.repo, job.id)
    elif decision.decision == "queued":
        # mark_job clears pending_deploy_sha on 'queued'; delete the pin ref too.
        mark_job(conn, job.id, status="queued", note=f"reconciled: {decision.reason}")
        delete_pending_ref(config.repo, job.id)
    elif decision.decision == "canceled":
        mark_job(conn, job.id, status="canceled", note=f"reconciled: {decision.reason}")
        delete_pending_ref(config.repo, job.id)
    else:  # blocked — PRESERVE the marker and pin ref for forensics.
        mark_job(conn, job.id, status="blocked", note=f"reconcile conflict: {decision.reason}")


def _decision_dict(decision: JobDecision, *, applied: bool) -> dict[str, Any]:
    return {
        "job_id": decision.job.id,
        "branch": decision.job.branch,
        "train_id": decision.job.train_id,
        "pending_deploy_sha": decision.pending_sha,
        "resolvable": decision.resolvable,
        "push_refs": [
            {"ref": v.ref, "remote_sha": v.remote_sha, "contains": v.contains}
            for v in decision.refs
        ],
        "decision": decision.decision,
        "reason": decision.reason,
        "applied": applied,
    }


def _summarize(decisions: list[JobDecision]) -> dict[str, int]:
    return {
        "reconciled_deployed": sum(d.decision == "deployed" for d in decisions),
        "requeued": sum(d.decision == "queued" for d in decisions),
        "canceled": sum(d.decision == "canceled" for d in decisions),
        "conflicts": sum(d.decision == "blocked" for d in decisions),
    }


# --------------------------------------------------------------------------- #
# engine — reconcile / recover / unlock
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ReconcileOutcome:
    jobs: list[dict[str, Any]]
    applied: bool
    summary: dict[str, int]
    exit_code: int  # 0 resolved/nothing · 10 ≥1 conflict


def reconcile(
    config: MergetrainConfig, conn: sqlite3.Connection, *, apply: bool
) -> ReconcileOutcome:
    """Resolve every ``needs_reconcile`` job against the remote.

    Acquires the runner lock (serialized against ``run-batch``; a live owner
    raises ``LockHeld``). Reads truth from the remote and either finalizes
    ``deployed``, requeues, honors a late cancel, or blocks — the exact writes a
    crash-free run would also have produced. Never pushes. If the remote is
    unreachable it raises ``RemoteUnreachable`` **before** any finalize write, so
    the jobs stay parked (a strict no-op for the remote verdict).
    """
    owner = default_owner()
    lock = acquire_runner_lock(
        conn, owner=owner, ttl_minutes=config.queue.lock_ttl_minutes
    )
    try:
        jobs = list_jobs_fifo(conn, status="needs_reconcile")
        if not jobs:
            return ReconcileOutcome(jobs=[], applied=apply, summary=_summarize([]), exit_code=0)
        if not _fetch(config):
            raise RemoteUnreachable(
                f"cannot reach remote '{config.git.remote}' to reconcile"
            )
        ref_shas: dict[str, str] = {}
        for ref in config.git.push_refs:
            _localize_ref(config, ref)  # bring the tip local so ancestry resolves
            reachable, remote_sha = _ls_remote(config, ref)
            if not reachable:
                raise RemoteUnreachable(
                    f"cannot ls-remote '{ref}' on '{config.git.remote}'"
                )
            ref_shas[ref] = remote_sha
        decisions = [_classify(config, job, ref_shas) for job in jobs]
        if apply:
            for decision in decisions:
                _apply(config, conn, decision)
        summary = _summarize(decisions)
        return ReconcileOutcome(
            jobs=[_decision_dict(d, applied=apply) for d in decisions],
            applied=apply,
            summary=summary,
            exit_code=10 if summary["conflicts"] else 0,
        )
    finally:
        release_runner_lock(conn, owner=owner, token=lock.token)


@dataclass(slots=True)
class RecoverOutcome:
    reconcile: ReconcileOutcome
    gc: dict[str, Any] | None
    exit_code: int


# Statuses whose pin ref is still load-bearing: blocked keeps it for
# reconcile-conflict forensics, needs_reconcile is still being reconciled, and
# in-flight/queued/validated rows may yet be pushed. Everything else
# (deployed/canceled/failed/missing) is a stale pin that would keep its commit
# object un-gc-able forever (0.3.0 decision Q6).
_PIN_KEEP_STATUSES = frozenset(
    {"blocked", "needs_reconcile", "in_progress", "queued", "validated"}
)


def sweep_pending_refs(
    config: MergetrainConfig, conn: sqlite3.Connection
) -> list[dict[str, Any]]:
    """Delete ``refs/mergetrain/pending/<id>`` pins whose owning job no longer
    needs them, so the namespace and the objects they pin do not grow without
    bound (0.3.0 decision Q6). Returns the swept refs for reporting."""
    swept: list[dict[str, Any]] = []
    listing = git_output_or_empty(
        ["for-each-ref", "--format=%(refname)", PENDING_REF_PREFIX], cwd=config.repo
    )
    for ref in listing.splitlines():
        ref = ref.strip()
        if not ref.startswith(PENDING_REF_PREFIX):
            continue
        try:
            job_id = int(ref[len(PENDING_REF_PREFIX) :])
        except ValueError:
            continue
        row = conn.execute(
            "SELECT status FROM deploy_queue WHERE id = ?", (job_id,)
        ).fetchone()
        status = str(row["status"]) if row is not None else "missing"
        if status in _PIN_KEEP_STATUSES:
            continue
        delete_pending_ref(config.repo, job_id)
        swept.append({"job_id": job_id, "ref": ref, "status": status})
    return swept


def recover(
    config: MergetrainConfig,
    conn: sqlite3.Connection,
    *,
    gc: bool,
    apply: bool = True,
) -> RecoverOutcome:
    """One-button restart heal: split orphans, then ``reconcile --apply``.

    The marker-aware orphan split runs as a side effect of acquiring the runner
    lock inside :func:`reconcile`. Never ships queued/validated work (no deploy
    as a side effect). Optionally gc's crashed worktrees + stale pin refs.
    """
    outcome = reconcile(config, conn, apply=apply)
    gc_result = None
    if gc:
        # Never gc a live runner's worktree, even if a runner started between
        # reconcile releasing its lock and this sweep.
        live = get_lock(conn)
        protect = (
            [live.worktree_path]
            if live and live.worktree_path and live.liveness != "dead"
            else []
        )
        gc_result = apply_gc(config, protect=protect)
        gc_result["swept_pending_refs"] = sweep_pending_refs(config, conn)
    return RecoverOutcome(reconcile=outcome, gc=gc_result, exit_code=outcome.exit_code)


@dataclass(slots=True)
class UnlockOutcome:
    cleared: bool
    prior_owner: str
    liveness: str
    reason: str
    audit_event_id: int | None
    context: dict[str, Any]
    exit_code: int  # 0 cleared · 4 refused · 5 no lock


def _remote_reachable(config: MergetrainConfig) -> bool:
    completed = run_command(
        ["git", "ls-remote", config.git.remote], cwd=config.repo, check=False
    )
    return completed.returncode == 0


def force_unlock(
    config: MergetrainConfig, conn: sqlite3.Connection, *, force: bool
) -> UnlockOutcome:
    """Clear a wedged runner lock (the expired-but-ALIVE/UNKNOWN + in_progress P5 case).

    Without ``--force`` only a DEAD/absent owner's lock is cleared. With
    ``--force`` the ordering is load-bearing: (1) confirm the remote is reachable
    first — if not, abort and change nothing; (2) only then delete the lock and
    run the marker-aware split. It never itself writes ``deployed``/``failed`` —
    that verdict comes solely from the subsequent remote-verified ``reconcile``.
    """
    lock = get_lock(conn)
    if lock is None:
        return UnlockOutcome(
            cleared=False,
            prior_owner="",
            liveness="",
            reason="no runner lock to clear",
            audit_event_id=None,
            context={},
            exit_code=5,
        )
    count_data = counts(conn)
    # The lease token is captured for the lock's identity but deliberately NOT
    # echoed — mergetrain never exposes claim tokens in readable output.
    context: dict[str, Any] = {
        "owner": lock.owner,
        "liveness": lock.liveness,
        "acquired_at": lock.acquired_at,
        "heartbeat_at": lock.heartbeat_at,
        "expires_at": lock.expires_at,
        "in_progress": count_data.get("in_progress", 0),
        "in_progress_with_marker": count_data.get("in_progress_with_marker", 0),
        "forced": bool(force),
    }
    if lock.liveness == "dead":
        if not force_clear_lock_and_split(conn, owner=lock.owner, token=lock.token):
            return _lock_changed(lock, context)
        event = record_run_event(
            conn,
            phase="unlock",
            state="cleared",
            message=f"cleared dead runner lock ({lock.owner})",
            detail=json.dumps(context, sort_keys=True),
        )
        return UnlockOutcome(
            cleared=True,
            prior_owner=lock.owner,
            liveness=lock.liveness,
            reason="dead owner lock cleared",
            audit_event_id=event.id,
            context=context,
            exit_code=0,
        )
    if not force:
        return UnlockOutcome(
            cleared=False,
            prior_owner=lock.owner,
            liveness=lock.liveness,
            reason=f"runner lock owner is {lock.liveness}; rerun with --force to steal it",
            audit_event_id=None,
            context=context,
            exit_code=4,
        )
    if not _remote_reachable(config):
        raise RemoteUnreachable(
            f"cannot reach remote '{config.git.remote}'; forced unlock aborted (nothing changed)"
        )
    # Scope the clear to the exact lock we inspected: the reachability probe above
    # touches the network, and the wedged runner could finish and a fresh runner
    # acquire the lock in that window. A scoped no-match aborts without clobbering it.
    if not force_clear_lock_and_split(conn, owner=lock.owner, token=lock.token):
        return _lock_changed(lock, context)
    event = record_run_event(
        conn,
        phase="unlock",
        state="forced",
        message=f"force-cleared {lock.liveness} runner lock ({lock.owner})",
        detail=json.dumps(context, sort_keys=True),
    )
    return UnlockOutcome(
        cleared=True,
        prior_owner=lock.owner,
        liveness=lock.liveness,
        reason=f"forced steal of {lock.liveness} owner lock",
        audit_event_id=event.id,
        context=context,
        exit_code=0,
    )


def _lock_changed(lock: Any, context: dict[str, Any]) -> UnlockOutcome:
    """The inspected lock was replaced (or released) during the remote probe."""
    return UnlockOutcome(
        cleared=False,
        prior_owner=lock.owner,
        liveness=lock.liveness,
        reason="runner lock changed during the remote check; nothing cleared (re-run if still wedged)",
        audit_event_id=None,
        context=context,
        exit_code=0,
    )
