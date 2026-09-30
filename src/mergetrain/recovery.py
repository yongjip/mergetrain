"""Crash-safe recovery: marker-aware reconcile / unlock (0.3.0 Phase 2).

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
from dataclasses import dataclass, field, replace
from typing import Any

from .command_runner import run_command
from .config import MergetrainConfig
from .deploy_plan import verification_policy_sha
from .errors import (
    CancellationRequested,
    LockHeld,
    MergetrainError,
    QueueBusy,
    QueueError,
    RemoteUnreachable,
)
from .git_destination import ResolvedGitDestination, resolve_git_destination
from .git_ops import (
    PENDING_REF_PREFIX,
    delete_pending_ref,
    deploy_audit_ref_name,
    git_output_or_empty,
    git_ref_exists,
    git_remote_ref_sha,
    resolve_pending_ref,
)
from .models import Job, public_owner
from .persistence.events import record_run_event
from .persistence.jobs import counts, get_job, is_reconcile_conflict, list_jobs_fifo, mark_job
from .persistence.leases import (
    acquire_runner_lock,
    default_owner,
    force_clear_lock_and_split,
    get_lock,
    lock_takeover,
    release_runner_lock,
    stranded_claim_plan,
)
from .persistence.recovery import unpack_push_refs
from .persistence.transactions import immediate
from .push_liveness import push_in_flight, push_lock_path

# --------------------------------------------------------------------------- #
# git primitives — all run with check=False, so a non-zero return is a datum,
# not an exception. Remote refs are read by exact name with git_remote_ref_sha.
# --------------------------------------------------------------------------- #


def _fetch(config: MergetrainConfig, destination: ResolvedGitDestination) -> bool:
    """Probe the exact recorded endpoint; ``True`` iff it is reachable."""
    completed = run_command(
        ["git", "ls-remote", destination.remote_alias],
        cwd=config.repo,
        env=destination.command_env(),
        check=False,
    )
    return completed.returncode == 0


def _localize_ref(
    config: MergetrainConfig, destination: ResolvedGitDestination, ref: str
) -> None:
    """Best-effort: bring a push ref's current remote tip into the local object
    store so ``merge-base --is-ancestor`` can resolve it. A bare ``git fetch``
    only downloads ``refs/heads/*``; a push ref under any other namespace
    (``refs/deploy/*``, a tag, …) would otherwise be a non-local object and the
    ancestry test would error. Absent refs simply no-op (``check=False``)."""
    run_command(
        ["git", "fetch", destination.remote_alias, ref],
        cwd=config.repo,
        env=destination.command_env(),
        check=False,
    )


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
    audit_ref: str = ""
    audit_ref_sha: str = ""


def _audit_ref_for_sha(sha: str) -> str:
    """Return the content-addressed audit ref, or empty for a corrupt marker."""
    try:
        return deploy_audit_ref_name(sha)
    except MergetrainError:
        return ""


def _classify(
    config: MergetrainConfig,
    job: Job,
    ref_shas: dict[str, str],
    *,
    audit_ref_sha: str = "",
) -> JobDecision:
    pending = job.pending_deploy_sha
    audit_ref = _audit_ref_for_sha(pending)
    audit_ref_present = bool(audit_ref) and audit_ref_sha.lower() == pending.lower()
    resolvable = _resolvable(config, job)
    refs: list[RefVerdict] = []
    unknown = False
    for ref, remote_sha in ref_shas.items():
        state = _ancestor_state(config, pending, remote_sha) if resolvable else "no"
        if state == "unknown":
            unknown = True
        refs.append(RefVerdict(ref, remote_sha, state == "yes"))

    def decided(decision: str, reason: str) -> JobDecision:
        return JobDecision(
            job, pending, resolvable, refs, decision, reason, audit_ref, audit_ref_sha
        )

    if not resolvable:
        return decided(
            "blocked", "pending deploy sha is unresolvable (pin ref gone and object pruned)"
        )
    if unknown:
        return decided(
            "blocked",
            "cannot determine remote containment for a push ref (tip unresolvable); refusing to guess",
        )
    if audit_ref_sha and not audit_ref_present:
        return decided(
            "blocked", "deploy audit ref points to an unexpected sha; refusing to guess"
        )
    contained = [verdict.contains for verdict in refs]
    if refs and all(contained):
        reason = "push landed: deploy sha present on every push ref"
        if job.cancel_requested_at:
            reason += "; late cancel ignored (the push had already landed)"
        return decided("deployed", reason)
    if not any(contained):
        if audit_ref_present:
            return decided(
                "blocked",
                "deploy audit ref proves the push landed before every payload ref was rewritten; manual recovery required",
            )
        if job.cancel_requested_at:
            return decided("canceled", "push did not land; late cancel honored")
        return decided("queued", "push did not land; requeued for a fresh deploy")
    return decided(
        "blocked", "deploy sha present on some but not all push refs (mixed remote state)"
    )


def _reconciled_verify_status(config: MergetrainConfig, job: Job) -> str:
    """What a crash-free run would have recorded about verification.

    Reconcile cannot know whether verify hooks ran, so a landed push is
    ``unknown`` -- unless the policy recorded with its marker had no hooks, in
    which case there was nothing to verify and ``verify --job`` could never
    clear an ``unknown`` (#231).
    """

    without_hooks = replace(config, deploy=replace(config.deploy, verify=()))
    if job.verification_policy_sha and job.verification_policy_sha == (
        verification_policy_sha(without_hooks)
    ):
        return "not_configured"
    return "unknown"


def _apply(config: MergetrainConfig, conn: sqlite3.Connection, decision: JobDecision) -> bool:
    """Write one decision; ``False`` when a concurrent transition overtook it."""

    job = decision.job
    # Compare-and-swap on the source status. reconcile read this job as
    # needs_reconcile (or as a blocked conflict), then did seconds of remote I/O
    # holding no write lock; if a concurrent op (e.g. a cancel) moved it since,
    # mark_job raises and we leave the newer state intact rather than
    # resurrecting a stale recovery decision.
    source = job.status
    try:
        if decision.decision == "deployed":
            mark_job(
                conn,
                job.id,
                status="deployed",
                deploy_sha=decision.pending_sha,
                push_status="succeeded",
                verify_status=_reconciled_verify_status(config, job),
                note=f"reconciled: {decision.reason}",
                expected_status=source,
            )
            delete_pending_ref(config.repo, job.id)
        elif decision.decision == "queued":
            # mark_job clears pending_deploy_sha on 'queued'; delete the pin ref too.
            mark_job(
                conn, job.id, status="queued",
                note=f"reconciled: {decision.reason}", expected_status=source,
            )
            delete_pending_ref(config.repo, job.id)
        elif decision.decision == "canceled":
            mark_job(
                conn, job.id, status="canceled",
                note=f"reconciled: {decision.reason}", expected_status=source,
            )
            delete_pending_ref(config.repo, job.id)
        elif source != "blocked":  # PRESERVE the marker and pin ref for forensics.
            mark_job(
                conn, job.id, status="blocked",
                note=f"reconcile conflict: {decision.reason}", expected_status=source,
            )
        elif get_job(conn, job.id).status != "blocked":
            # A conflict that stays blocked needs no write, so no CAS notices
            # a dismiss that landed during the remote check.
            return False
    except QueueBusy:
        # Contention is not "someone else won the race": nothing was written, so
        # reporting this decision as applied would be a false success. Surface it
        # as the retryable failure it is and let the caller run reconcile again.
        raise
    except (QueueError, CancellationRequested):
        # The job was transitioned by a concurrent op after our read — do not
        # overwrite the newer state (a landed cancel must survive reconcile).
        return False
    return True


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
        "audit_ref": decision.audit_ref,
        "audit_ref_sha": decision.audit_ref_sha,
        "audit_ref_present": (
            bool(decision.audit_ref)
            and decision.audit_ref_sha.lower() == decision.pending_sha.lower()
        ),
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


def _job_push_target(
    config: MergetrainConfig, job: Job
) -> tuple[str, tuple[str, ...], str]:
    """The remote + push-ref set the job's interrupted push actually targeted.

    Read from the durable marker so reconcile asks the right remote even if the
    config's remote or push_refs changed after the crash. The caller rejects a
    legacy marker whose endpoint hash is absent: current config cannot prove
    where an older ambiguous push actually went."""
    remote = job.pending_deploy_remote or config.git.remote
    refs = unpack_push_refs(job.pending_deploy_refs) or list(config.git.push_refs)
    return remote, tuple(refs), job.pending_deploy_destination_sha


def _classify_group(
    config: MergetrainConfig,
    target: tuple[str, tuple[str, ...], str],
    group: list[Job],
) -> dict[int, JobDecision]:
    """Classify the jobs whose interrupted push shared one recorded target.

    Raises ``RemoteUnreachable`` when that target can no longer be asked for
    truth: its endpoint changed or cannot be resolved, or the remote is down.
    """
    remote, refs, recorded_destination_sha = target
    effective = replace(config, git=replace(config.git, remote=remote, push_refs=refs))
    try:
        destination = resolve_git_destination(effective)
    except MergetrainError as exc:
        raise RemoteUnreachable(
            f"cannot resolve the recorded push destination {remote!r} "
            "to reconcile; restore its exact endpoint and retry"
        ) from exc
    if destination.push_endpoint_sha != recorded_destination_sha:
        raise RemoteUnreachable(
            f"recorded push destination {remote!r} no longer matches "
            "the endpoint used before the crash; restore it and retry"
        )
    if not _fetch(effective, destination):
        raise RemoteUnreachable(f"cannot reach remote '{remote}' to reconcile")
    ref_shas: dict[str, str] = {}
    for ref in refs:
        _localize_ref(effective, destination, ref)  # bring the tip local so ancestry resolves
        reachable, remote_sha = git_remote_ref_sha(
            effective.repo, destination.remote_alias, ref, env=destination.command_env()
        )
        if not reachable:
            raise RemoteUnreachable(f"cannot ls-remote '{ref}' on '{remote}'")
        ref_shas[ref] = remote_sha
    audit_shas: dict[str, str] = {}
    for job in group:
        audit_ref = _audit_ref_for_sha(job.pending_deploy_sha)
        if not audit_ref or audit_ref in audit_shas:
            continue
        reachable, audit_sha = git_remote_ref_sha(
            effective.repo, destination.remote_alias, audit_ref, env=destination.command_env()
        )
        if not reachable:
            raise RemoteUnreachable(f"cannot ls-remote deploy audit ref on '{remote}'")
        audit_shas[audit_ref] = audit_sha
    decisions: dict[int, JobDecision] = {}
    for job in group:
        audit_ref = _audit_ref_for_sha(job.pending_deploy_sha)
        decisions[job.id] = _classify(
            effective,
            job,
            ref_shas,
            audit_ref_sha=audit_shas.get(audit_ref, ""),
        )
    return decisions


# --------------------------------------------------------------------------- #
# engine — reconcile / unlock
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ReconcileOutcome:
    jobs: list[dict[str, Any]]
    applied: bool
    summary: dict[str, int]
    exit_code: int  # 0 resolved/nothing · 10 ≥1 conflict
    # A stopped runner's claims and what the orphan split does to each.
    stranded: list[dict[str, Any]] = field(default_factory=list)


def reconcile(
    config: MergetrainConfig, conn: sqlite3.Connection, *, apply: bool
) -> ReconcileOutcome:
    """Resolve every ``needs_reconcile`` job against the remote.

    Acquires the runner lock (serialized against ``validate``/``deploy``; a live owner
    raises ``LockHeld``). Reads truth from the remote and either finalizes
    ``deployed``, requeues, honors a late cancel, or blocks — the exact writes a
    crash-free run would also have produced. Never pushes. If the remote is
    unreachable it raises ``RemoteUnreachable`` **before** any finalize write, so
    the jobs stay parked (a strict no-op for the remote verdict).

    A blocked reconcile conflict is re-checked the same way, but only as far as
    its recorded target still answers; otherwise it is reported still blocked.

    Without ``apply`` it writes nothing. Taking the runner lock would split a
    stopped runner's claims, so a preview only reads the lock and reports in
    ``stranded`` what the split will do. It classifies the claims the split
    would park, as the applied run does right after parking them.
    """
    action, refusal = lock_takeover(conn)
    if action == "refuse":
        raise LockHeld(refusal)
    stranded = stranded_claim_plan(conn) if action == "split" else []
    for entry in stranded:
        entry["applied"] = apply
    owner = default_owner()
    lock = (
        acquire_runner_lock(conn, owner=owner, ttl_minutes=config.queue.lock_ttl_minutes)
        if apply
        else None
    )
    try:
        # An earlier reconcile that could not settle a push parked it blocked
        # with its marker. Its push may have landed, so the command status
        # recommends for it must re-ask the remote too (#224). A legacy marker
        # without a destination identity can never be checked automatically.
        conflicts = [
            job
            for job in list_jobs_fifo(conn, status="blocked")
            if is_reconcile_conflict(job) and job.pending_deploy_destination_sha
        ]
        parked = [] if apply or not stranded else [
            job
            for job in list_jobs_fifo(conn, status="in_progress")
            if job.pending_deploy_sha
        ]
        jobs = sorted(
            [*list_jobs_fifo(conn, status="needs_reconcile"), *conflicts, *parked],
            key=lambda job: job.id,
        )
        if not jobs:
            return ReconcileOutcome(
                jobs=[],
                applied=apply,
                summary=_summarize([]),
                exit_code=0,
                stranded=stranded,
            )
        # A runner killed mid-push leaves the push running in its own process
        # group. Until every process of it has exited it can still land, so the
        # remote cannot yet say whether it did (#220).
        for job in jobs:
            if push_in_flight(config, job.pending_deploy_sha):
                raise LockHeld(
                    f"job {job.id}: the push of {job.pending_deploy_sha[:12]} started "
                    "by a runner that stopped is still running and may yet land; "
                    "wait for it to exit, then rerun reconcile. If no git process "
                    "of that push is left, a process it started still holds "
                    f"{push_lock_path(config, job.pending_deploy_sha)}: stop that "
                    "process or delete the file, then rerun reconcile"
                )
        # One interrupted push parks jobs bound for a single target, but two
        # separate crashes can park jobs bound for different remotes/refs. Ask
        # each group's own recorded target for truth — the refs the push actually
        # used, not whatever the current config now says (#84, defect 3).
        groups: dict[tuple[str, tuple[str, ...], str], list[Job]] = {}
        for job in jobs:
            target = _job_push_target(config, job)
            if not target[2]:
                raise RemoteUnreachable(
                    f"job {job.id} has a legacy pending-push marker without an "
                    "exact destination identity; automatic reconcile is unsafe. "
                    "Keep it parked and inspect the v2.3.0-era remote evidence "
                    "manually before changing queue state"
                )
            groups.setdefault(target, []).append(job)
        decisions_by_id: dict[int, JobDecision] = {}
        for target, group in groups.items():
            try:
                decisions_by_id.update(_classify_group(config, target, group))
            except RemoteUnreachable as exc:
                if any(job.status == "needs_reconcile" for job in group):
                    raise
                # Re-checking a conflict is best effort. It is already parked
                # blocked, and an endpoint that has since changed or gone away
                # must not keep a newer crash from being reconciled.
                for job in group:
                    decisions_by_id[job.id] = JobDecision(
                        job,
                        job.pending_deploy_sha,
                        _resolvable(config, job),
                        [],
                        "blocked",
                        f"cannot re-check this conflict: {exc}",
                        _audit_ref_for_sha(job.pending_deploy_sha),
                    )
        # Emit in the original FIFO order, independent of target grouping.
        decisions = [decisions_by_id[job.id] for job in jobs]
        # A decision a concurrent transition overtook was not written: report
        # it unapplied, and summarize (and grade) only what was.
        applied_ids: set[int] = set()
        if apply:
            for decision in decisions:
                if _apply(config, conn, decision):
                    applied_ids.add(decision.job.id)
        summary = _summarize(
            [d for d in decisions if d.job.id in applied_ids] if apply else decisions
        )
        return ReconcileOutcome(
            jobs=[_decision_dict(d, applied=d.job.id in applied_ids) for d in decisions],
            applied=apply,
            summary=summary,
            exit_code=10 if summary["conflicts"] else 0,
            stranded=stranded,
        )
    finally:
        if lock is not None:
            release_runner_lock(conn, owner=owner, token=lock.token)


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


@dataclass(slots=True)
class UnlockOutcome:
    cleared: bool
    prior_owner: str
    liveness: str
    reason: str
    audit_event_id: int | None
    context: dict[str, Any]
    exit_code: int  # 0 cleared · 4 refused · 5 no lock


# The inspected lock was replaced (or released) during the remote probe.
_LOCK_CHANGED = (
    "runner lock changed during the remote check; nothing cleared (re-run if still wedged)"
)


def _remote_reachable(config: MergetrainConfig) -> bool:
    try:
        destination = resolve_git_destination(config)
    except MergetrainError:
        return False
    return _fetch(config, destination)


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
    # The event appears in `events` and `hub status`, which never show the OS
    # username (#231); the command's own output keeps the full owner.
    audited_owner = public_owner(lock.owner)
    audited = json.dumps({**context, "owner": audited_owner}, sort_keys=True)

    def outcome(
        reason: str, *, exit_code: int = 0, audit_event_id: int | None = None
    ) -> UnlockOutcome:
        # A clear is always audited, so the lock was cleared iff there is an event.
        return UnlockOutcome(
            cleared=audit_event_id is not None,
            prior_owner=lock.owner,
            liveness=lock.liveness,
            reason=reason,
            audit_event_id=audit_event_id,
            context=context,
            exit_code=exit_code,
        )

    if lock.liveness == "dead":
        # The clear and its audit event commit together. Separately, an audit
        # write that failed after the clear left an unaudited clear that a
        # retry could not repair: it found no lock left to clear.
        with immediate(conn):
            if not force_clear_lock_and_split(conn, owner=lock.owner, token=lock.token):
                return outcome(_LOCK_CHANGED)
            event = record_run_event(
                conn,
                phase="unlock",
                state="cleared",
                message=f"cleared dead runner lock ({audited_owner})",
                detail=audited,
            )
        return outcome("dead owner lock cleared", audit_event_id=event.id)
    if not force:
        return outcome(
            f"runner lock owner is {lock.liveness}; rerun with --force to steal it",
            exit_code=4,
        )
    if not _remote_reachable(config):
        raise RemoteUnreachable(
            f"cannot reach remote '{config.git.remote}'; forced unlock aborted (nothing changed)"
        )
    # Scope the clear to the exact lock we inspected: the reachability probe above
    # touches the network, and the wedged runner could finish and a fresh runner
    # acquire the lock in that window. A scoped no-match aborts without clobbering it.
    # The steal commits with its audit event, as the dead-owner clear does.
    with immediate(conn):
        if not force_clear_lock_and_split(conn, owner=lock.owner, token=lock.token):
            return outcome(_LOCK_CHANGED)
        event = record_run_event(
            conn,
            phase="unlock",
            state="forced",
            message=f"force-cleared {lock.liveness} runner lock ({audited_owner})",
            detail=audited,
        )
    return outcome(f"forced steal of {lock.liveness} owner lock", audit_event_id=event.id)
