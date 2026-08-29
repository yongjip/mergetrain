"""Validation and deploy commands."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from typing import Any

from ..cli_support import (
    _error_payload,
    _job_result_line,
    _preflight_config,
    config_from_args,
    dump_json,
)
from ..errors import QueueError
from ..git_ops import DEPLOY_AUDIT_REF_PREFIX
from ..git_runner import GitRunner
from ..models import Job
from ..observability import _gate_runs
from ..reuse import reuse_explanation
from ..store import (
    claim_all_queued,
    claim_deploy_batch,
    claim_next_job,
    connect,
    default_owner,
    deploy_reconcile_pending,
    list_history_events,
    release_runner_lock,
    select_validated_train,
    validated_train_summaries,
)


def _mode_from_args(args: argparse.Namespace) -> bool:
    if args.deploy == args.validate_only:
        raise QueueError("choose exactly one: --validate-only or --deploy")
    return bool(args.deploy)


def _emit_deploy_reconcile_block(args: argparse.Namespace, pending: int) -> int:
    note = (
        f"deploy hard-blocked: {pending} job(s) pending reconcile — run "
        "'mergetrain reconcile --apply' first"
    )
    if args.json:
        dump_json(
            _error_payload(
                "reconcile_pending_deploy",
                note,
                next_action="reconcile_pending_deploy",
                needs_reconcile=pending,
            )
        )
    else:
        print(note, file=sys.stderr)
    return 1


def _emit_validated_train_block(
    args: argparse.Namespace, trains: list[dict[str, Any]]
) -> int:
    """Refuse a one-job push while an approved train is still pending.

    ``run-next`` claims the next *queued* job, so it never picks up the
    ``validated`` members of a pending train — it would push a different commit
    and move the integration ref out from under the exact train a human
    approved, invalidating that validation without saying so. docs/cli.md
    already directs validated work through ``run-batch --deploy``; this makes it
    true instead of advisory.
    """

    ids = [str(train.get("train_id")) for train in trains]
    listed = ", ".join(ids)
    note = (
        f"deploy refused: {len(ids)} validated train(s) pending "
        f"({listed}). run-next claims a queued job instead, which would move the "
        f"integration ref and invalidate that validation — "
        f"'mergetrain run-batch --deploy --train-id {ids[0]}' "
        "ships the approved train, or dismiss it first to ship something else"
    )
    if args.json:
        dump_json(
            _error_payload(
                "validated_train_pending",
                note,
                next_action="deploy_validated_train_when_approved",
                pending_train_ids=ids,
            )
        )
    else:
        print(note, file=sys.stderr)
    return 1


def _results_payload(results: list[Job]) -> dict[str, Any]:
    status_counts = Counter(job.status for job in results)
    push_counts = Counter(job.push_status for job in results)
    verify_counts = Counter(job.verify_status for job in results)
    reused_validation_shas = sorted(
        {job.reused_validation_sha for job in results if job.reused_validation_sha}
    )
    successful = sum(status_counts[status] for status in ("validated", "deployed"))
    warnings = sum(
        job.status == "deployed" and job.verify_status == "failed" for job in results
    )
    if successful == len(results) and warnings:
        result = "warning"
    elif successful == len(results):
        result = "success"
    elif successful:
        result = "partial"
    else:
        result = "failed"
    return {
        # ok = the run executed and produced a result; the graded outcome
        # (success/warning/partial/failed) lives in `result`. A completed run
        # with a post-push verify warning is ok:true, result:"warning".
        "ok": True,
        "result": result,
        "counts": dict(sorted(status_counts.items())),
        "push_counts": dict(sorted(push_counts.items())),
        "verify_counts": dict(sorted(verify_counts.items())),
        "reused_validation_shas": reused_validation_shas,
        "jobs": [job.to_dict() for job in results],
    }


def _run_exit_code(payload: dict[str, Any]) -> int:
    # "warning" = the train shipped but a post-push verify hook failed. The push
    # already landed and cannot be un-shipped, so this is exit 0 (a caller reads
    # `result` to notice the warning). Only "partial"/"failed" — where something
    # did NOT ship — is exit 1. Exit 1 therefore never means "did not ship".
    return 0 if payload["result"] in ("success", "warning") else 1


def _print_run_payload(payload: dict[str, Any]) -> None:
    if payload.get("jobs"):
        for job_data in payload["jobs"]:
            print(_job_result_line(job_data))
        if payload.get("result") != "success":
            print(f"result: {payload['result']}")
    else:
        print(payload.get("note", "done"))


def cmd_run_next(args: argparse.Namespace) -> int:
    deploy = _mode_from_args(args)
    config = config_from_args(args)
    _preflight_config(config)
    owner = default_owner()
    lease_token = ""
    conn = connect(config.state.db)
    try:
        if deploy:
            pending = deploy_reconcile_pending(conn)
            if pending:
                return _emit_deploy_reconcile_block(args, pending)
            eligible = [
                train
                for train in validated_train_summaries(conn)
                if train.get("deploy_eligible")
            ]
            if eligible:
                return _emit_validated_train_block(args, eligible)
        job = claim_next_job(
            conn,
            owner=owner,
            ttl_minutes=config.queue.lock_ttl_minutes,
            deploy=deploy,
        )
        if job is None:
            if deploy:
                pending = deploy_reconcile_pending(conn)
                if pending:
                    return _emit_deploy_reconcile_block(args, pending)
            payload = {**_results_payload([]), "note": "no queued jobs"}
        else:
            lease_token = job.claim_token
            result = GitRunner(config).process_one(
                conn,
                job,
                deploy=deploy,
                keep_worktree=args.keep_worktree,
                owner=owner,
                ttl_minutes=config.queue.lock_ttl_minutes,
            )
            payload = _results_payload([result])
    finally:
        if lease_token:
            release_runner_lock(conn, owner=owner, token=lease_token)
        conn.close()
    if args.json:
        dump_json(payload)
    else:
        _print_run_payload(payload)
    return _run_exit_code(payload)


def cmd_run_batch(args: argparse.Namespace) -> int:
    deploy = _mode_from_args(args)
    if args.train_id and not deploy:
        raise QueueError("--train-id requires --deploy")
    if args.reuse_validated and not deploy:
        raise QueueError("--reuse-validated requires --deploy")
    if args.preview and not deploy:
        raise QueueError("--preview requires --deploy")
    config = config_from_args(args)
    _preflight_config(config)
    if args.preview:
        conn = connect(config.state.db)
        try:
            selected, jobs = select_validated_train(
                conn, train_id=args.train_id or ""
            )
            history_events = list_history_events(conn)
        finally:
            conn.close()
        if selected is None or not jobs:
            raise QueueError("no validated train is ready to preview")
        decision = GitRunner(config).preview_validated_reuse(
            jobs,
            authorized=args.reuse_validated,
        )
        payload = {
            "ok": True,
            "preview": True,
            "mode": "deploy",
            "push_plan": {
                "atomic": True,
                "remote": config.git.remote,
                "refs": [
                    {"source": "HEAD", "target": ref, "spec": f"HEAD:{ref}"}
                    for ref in config.git.push_refs
                ],
                "audit_ref": {
                    "source": "DEPLOY_SHA",
                    "target": f"{DEPLOY_AUDIT_REF_PREFIX}<DEPLOY_SHA>",
                    "spec": f"DEPLOY_SHA:{DEPLOY_AUDIT_REF_PREFIX}<DEPLOY_SHA>",
                    "retention": "permanent",
                },
            },
            "train_id": selected["train_id"],
            "reuse": reuse_explanation(
                config,
                jobs,
                decision=decision,
                gate_runs=_gate_runs(history_events),
            ),
            "jobs": [job.to_dict() for job in jobs],
        }
        if args.json:
            dump_json(payload)
        else:
            targets = ", ".join(
                f"HEAD:{ref}" for ref in config.git.push_refs
            )
            if decision.eligible:
                print(
                    "preview: deploy validated commit "
                    f"{decision.reused_validation_sha} by atomic push to "
                    f"{config.git.remote}: {targets}"
                )
            else:
                print(
                    f"preview: {decision.action} full gates, then "
                    "deploy by atomic push to "
                    f"{config.git.remote}: {targets} - {'; '.join(decision.reasons)}"
                )
        return 0
    owner = default_owner()
    lease_token = ""
    conn = connect(config.state.db)
    try:
        if deploy:
            pending = deploy_reconcile_pending(conn)
            if pending:
                return _emit_deploy_reconcile_block(args, pending)
            jobs = claim_deploy_batch(
                conn,
                owner=owner,
                ttl_minutes=config.queue.lock_ttl_minutes,
                train_id=args.train_id or "",
            )
        else:
            jobs = claim_all_queued(conn, owner=owner, ttl_minutes=config.queue.lock_ttl_minutes)
        if not jobs:
            payload = {**_results_payload([]), "note": "no queued jobs"}
        else:
            lease_token = jobs[0].claim_token
            results = GitRunner(config).process_batch(
                conn,
                jobs,
                deploy=deploy,
                keep_worktree=args.keep_worktree,
                owner=owner,
                ttl_minutes=config.queue.lock_ttl_minutes,
                reuse_validated=args.reuse_validated,
            )
            payload = _results_payload(results)
    finally:
        if lease_token:
            release_runner_lock(conn, owner=owner, token=lease_token)
        conn.close()
    if args.json:
        dump_json(payload)
    else:
        _print_run_payload(payload)
    return _run_exit_code(payload)
