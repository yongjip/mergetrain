"""Recovery, verification, and cleanup commands."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

from ..cli_support import (
    _error_payload,
    _recovery_next_action,
    config_from_args,
    dump_json,
)
from ..errors import ConfigError, LockHeld, QueueError, RemoteUnreachable
from ..git_ops import (
    apply_gc,
    branch_exists,
    find_worktree_gc_candidates,
    git_current_branch,
)
from ..git_runner import GitRunner
from ..recovery import force_unlock, reconcile, recover, sweep_pending_refs
from ..store import (
    connect,
    finish_recovery_operation,
    get_job,
    get_lock,
    list_verify_unknown_jobs,
    live_worktree_path,
    resolve_verify_status,
    start_recovery_operation,
    terminal_branch_candidates,
)


def cmd_gc(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    conn = connect(config.state.db)
    try:
        branch_candidates_raw = terminal_branch_candidates(conn)
        # Protect the worktree of a live runner from removal (Blocker: gc
        # --apply must never destroy a worktree a running deploy is inside).
        lock = get_lock(conn)
        protect_worktrees = (
            [lock.worktree_path] if lock and lock.worktree_path and lock.liveness != "dead" else []
        )
        protected = set(config.git.push_refs) | {
            config.git.integration_branch,
            git_current_branch(config.repo),
        }
        branch_candidates: list[dict[str, Any]] = []
        delete_branch_names: list[str] = []
        for candidate in branch_candidates_raw:
            branch = candidate["branch"]
            exists = branch_exists(config.repo, branch)
            eligible = exists and branch not in protected
            item = {**candidate, "exists": exists, "eligible": eligible}
            branch_candidates.append(item)
            if eligible:
                delete_branch_names.append(branch)
        payload: dict[str, Any] = {
            "ok": True,
            "apply": bool(args.apply),
            "delete_branches": bool(args.delete_branches),
            "worktree_candidates": find_worktree_gc_candidates(config, protect=protect_worktrees),
            "branch_candidates": branch_candidates,
            "result": None,
        }
        if args.apply:
            # Keep the connection open across apply_gc so it can re-read the live
            # lock immediately before each removal — a runner that acquired the
            # lock after the protect snapshot must still be spared (#84, defect 5).
            result = apply_gc(
                config,
                delete_branches=delete_branch_names if args.delete_branches else (),
                protect=protect_worktrees,
                live_worktree_now=lambda: live_worktree_path(conn),
            )
            result["swept_pending_refs"] = sweep_pending_refs(config, conn)
            payload["result"] = result
    finally:
        conn.close()
    if args.json:
        dump_json(payload)
    else:
        print(f"worktree candidates: {len(payload['worktree_candidates'])}")
        print(f"branch candidates: {len(payload['branch_candidates'])}")
        if args.apply:
            print(f"swept pending refs: {len(payload['result']['swept_pending_refs'])}")
        if args.apply:
            print("applied")
        else:
            print("dry-run; pass --apply to remove candidates")
    return 0


def _emit_recovery_error(
    args: argparse.Namespace, message: str, exit_code: int, *, error_code: str
) -> int:
    if getattr(args, "json", False):
        dump_json(_error_payload(error_code, message, retryable=exit_code in (3, 7)))
    else:
        print(f"mergetrain: {message}", file=sys.stderr)
    return exit_code


def _finish_recovery_evidence(
    conn: sqlite3.Connection,
    invocation_id: str,
    *,
    state: str,
    detail: dict[str, Any],
) -> None:
    """Do not let an evidence-tail write replace the recovery command result."""

    try:
        finish_recovery_operation(
            conn,
            invocation_id,
            state=state,
            detail=json.dumps(detail, sort_keys=True),
        )
    except (QueueError, sqlite3.Error):
        # The durable started event remains visible as incomplete. Recovery
        # truth and exit semantics take precedence over analytical completeness.
        pass


def cmd_reconcile(args: argparse.Namespace) -> int:
    try:
        config = config_from_args(args)
    except ConfigError as exc:
        return _emit_recovery_error(args, str(exc), 2, error_code="config_error")
    conn = connect(config.state.db)
    operation = start_recovery_operation(
        conn,
        operation="reconcile",
        applied=bool(args.apply),
    )
    try:
        # v3 has one recovery entrypoint. Applying reconciliation first heals
        # safely recoverable dead-owner claims, then classifies pending pushes
        # against the remote. Cleanup and non-repeatable verify remain separate
        # explicit operations.
        if args.apply:
            outcome = recover(config, conn, gc=False, apply=True).reconcile
        else:
            outcome = reconcile(config, conn, apply=False)
        next_action = _recovery_next_action(conn, config)
    except LockHeld as exc:
        _finish_recovery_evidence(
            conn,
            operation.invocation_id,
            state="lock_held",
            detail={"error_code": "lock_held"},
        )
        return _emit_recovery_error(args, str(exc), 3, error_code="lock_held")
    except RemoteUnreachable as exc:
        _finish_recovery_evidence(
            conn,
            operation.invocation_id,
            state="remote_unreachable",
            detail={"error_code": "remote_unreachable"},
        )
        return _emit_recovery_error(args, str(exc), 7, error_code="remote_unreachable")
    except Exception as exc:
        _finish_recovery_evidence(
            conn,
            operation.invocation_id,
            state="error",
            detail={"error_type": type(exc).__name__},
        )
        raise
    else:
        _finish_recovery_evidence(
            conn,
            operation.invocation_id,
            state="conflict" if outcome.summary.get("conflicts") else "success",
            detail={"summary": outcome.summary},
        )
    finally:
        conn.close()
    payload = {
        # Contract 1: ok = the command ran; the graded outcome is in `result`.
        # A reconcile that finds conflicts still ran (exit 10 carries that).
        "ok": True,
        "result": "conflict" if outcome.summary.get("conflicts") else "success",
        "applied": outcome.applied,
        "jobs": outcome.jobs,
        "summary": outcome.summary,
        "next_action": next_action,
    }
    if args.json:
        dump_json(payload)
    else:
        summary = outcome.summary
        verb = "applied" if outcome.applied else "dry-run"
        print(
            f"reconcile ({verb}): {summary['reconciled_deployed']} deployed, "
            f"{summary['requeued']} requeued, {summary['canceled']} canceled, "
            f"{summary['conflicts']} conflict(s)"
        )
        for job in outcome.jobs:
            print(f"  #{job['job_id']} {job['decision']}: {job['reason']}")
        print(f"next action: {next_action}")
    return outcome.exit_code


def cmd_unlock(args: argparse.Namespace) -> int:
    try:
        config = config_from_args(args)
    except ConfigError as exc:
        return _emit_recovery_error(args, str(exc), 2, error_code="config_error")
    conn = connect(config.state.db)
    try:
        outcome = force_unlock(config, conn, force=args.force)
        next_action = _recovery_next_action(conn, config)
    except RemoteUnreachable as exc:
        return _emit_recovery_error(args, str(exc), 7, error_code="remote_unreachable")
    finally:
        conn.close()
    payload = {
        # ok = the command ran; `cleared` carries whether a lock was removed
        # (mirrors hub remove's ok:true + removed). The exit code carries the
        # machine signal for no-lock (5) / refused-without-force (4).
        "ok": True,
        "cleared": outcome.cleared,
        "prior_owner": outcome.prior_owner,
        "liveness": outcome.liveness,
        "reason": outcome.reason,
        "audit_event_id": outcome.audit_event_id,
        "lock_context": outcome.context,
        "next_action": next_action,
    }
    if args.json:
        dump_json(payload)
    else:
        state = "cleared" if outcome.cleared else "unchanged"
        print(f"unlock: {state} — {outcome.reason}")
        print(f"next action: {next_action}")
    return outcome.exit_code


def cmd_verify(args: argparse.Namespace) -> int:
    """Discharge deployed jobs left verify_status='unknown' by a crash.

    Re-runs the configured deploy.verify hooks against the recorded deploy_sha
    and records the result, or accepts an explicit --ack for hooks that cannot
    be re-run. This clears the otherwise-permanent verify_reconciled_deploy
    next_action.
    """

    config = config_from_args(args)
    conn = connect(config.state.db)
    try:
        if args.job is not None:
            job = get_job(conn, args.job)
            if job.status != "deployed" or job.verify_status != "unknown":
                raise QueueError(
                    f"job {args.job} is not an unresolved verify "
                    f"(status={job.status}, verify_status={job.verify_status})"
                )
            targets = [job]
        else:
            targets = list_verify_unknown_jobs(conn)
        resolved: list[dict[str, Any]] = []
        for job in targets:
            if args.ack:
                outcome = args.ack
                note = f"verify {outcome} by operator --ack"
            else:
                log_path = config.state.logs / f"verify-{job.id}.log"
                config.state.logs.mkdir(parents=True, exist_ok=True)
                with log_path.open("w", encoding="utf-8") as log:
                    passed = GitRunner(config).reverify_deploy(deploy_sha=job.deploy_sha, log=log)
                outcome = "succeeded" if passed else "failed"
                note = f"verify re-run against {job.deploy_sha}: {outcome}"
            updated = resolve_verify_status(conn, job.id, verify_status=outcome, note=note)
            resolved.append({"job_id": updated.id, "verify_status": updated.verify_status})
        next_action = _recovery_next_action(conn, config)
    finally:
        conn.close()
    result = "failed" if any(item["verify_status"] == "failed" for item in resolved) else "success"
    payload = {
        "ok": True,
        "result": result,
        "resolved": resolved,
        "next_action": next_action,
    }
    if args.json:
        dump_json(payload)
    else:
        if not resolved:
            print("no deployed jobs awaiting verify")
        for item in resolved:
            print(f"job {item['job_id']}: verify {item['verify_status']}")
        print(f"next action: {next_action}")
    # Exit 1 if any re-run verify failed, so scripts can react.
    return 0 if all(item["verify_status"] == "succeeded" for item in resolved) else 1
