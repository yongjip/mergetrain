"""Validation and deploy commands."""

from __future__ import annotations

import argparse
import hmac
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
from ..deploy_plan import deploy_plan_sha
from ..errors import DeployPlanChanged, QueueError, redact_secrets
from ..git_destination import resolve_git_destination
from ..git_ops import DEPLOY_AUDIT_REF_PREFIX
from ..git_runner import GitRunner
from ..models import Job
from ..observability import _gate_runs
from ..reuse import reuse_explanation
from ..store import (
    claim_all_queued,
    claim_deploy_batch,
    connect,
    counts,
    default_owner,
    deploy_reconcile_pending,
    list_history_events,
    release_runner_lock,
    select_validated_train,
    validated_train_summaries,
)


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


def _results_payload(results: list[Job]) -> dict[str, Any]:
    status_counts = Counter(job.status for job in results)
    push_counts = Counter(job.push_status for job in results)
    verify_counts = Counter(job.verify_status for job in results)
    reused_validation_shas = sorted(
        {job.reused_validation_sha for job in results if job.reused_validation_sha}
    )
    successful = sum(status_counts[status] for status in ("validated", "deployed"))
    warnings = sum(job.status == "deployed" and job.verify_status == "failed" for job in results)
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


def _execute_batch(
    args: argparse.Namespace,
    *,
    deploy: bool,
    expected_plan: str = "",
) -> int:
    """Run the shared train engine behind the two public execution verbs."""

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
            if expected_plan:
                selected, selected_jobs = select_validated_train(
                    conn,
                    train_id=getattr(args, "train_id", ""),
                )
                if selected is None or not selected_jobs:
                    raise DeployPlanChanged(
                        "deploy_plan_changed: the confirmed validated train is no "
                        "longer deploy-eligible; nothing was pushed"
                    )
                current_plan_sha = deploy_plan_sha(
                    config,
                    selected_jobs,
                    reuse_validated=False,
                )
                if not hmac.compare_digest(current_plan_sha, expected_plan):
                    raise DeployPlanChanged(
                        "deploy_plan_changed: the confirmed train, destination, "
                        "gates, reuse, or verify policy changed; nothing was pushed"
                    )
            jobs = claim_deploy_batch(
                conn,
                owner=owner,
                ttl_minutes=config.queue.lock_ttl_minutes,
                train_id=getattr(args, "train_id", ""),
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
                reuse_validated=False,
                expected_plan_sha=expected_plan,
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


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate the queued train without exposing runner implementation modes."""

    config = config_from_args(args)
    _preflight_config(config)
    conn = connect(config.state.db)
    try:
        ready = any(train.get("deploy_eligible") for train in validated_train_summaries(conn))
    finally:
        conn.close()
    if ready:
        note = "a validated train is already ready; deploy it before validating more work"
        if args.json:
            dump_json(
                _error_payload(
                    "validated_train_pending",
                    note,
                    next_action="deploy_when_approved",
                )
            )
        else:
            print(note, file=sys.stderr)
        return 1
    return _execute_batch(args, deploy=False)


def _render_v3_preview(payload: dict[str, Any]) -> None:
    jobs = payload.get("jobs", [])
    tasks = ", ".join(str(job.get("task") or job.get("branch")) for job in jobs)
    push_plan = payload["push_plan"]
    refs = ", ".join(item["target"] for item in push_plan["refs"])
    print(f"Ready to deploy {len(jobs)} job(s): {tasks}")
    print(f"Destination: {push_plan['url']} ({refs})")
    reuse = payload.get("reuse") or {}
    decision = reuse.get("decision") or {}
    action = decision.get("action")
    if action:
        print(f"Gate plan: {action}")
    print(
        "The exact train, destination, gates, reuse policy, and verify hooks "
        "will be checked again before push."
    )


def cmd_deploy(args: argparse.Namespace) -> int:
    """Validate if needed, present one exact plan, then deploy after approval."""

    if args.expected_plan:
        return _execute_batch(
            args,
            deploy=True,
            expected_plan=args.expected_plan,
        )

    config = config_from_args(args)
    _preflight_config(config)
    conn = connect(config.state.db)
    try:
        pending_reconcile = deploy_reconcile_pending(conn)
        ready = any(train.get("deploy_eligible") for train in validated_train_summaries(conn))
        queue_counts = counts(conn)
        queued = bool(queue_counts.get("queued", 0))
        in_progress = bool(queue_counts.get("in_progress", 0))
    finally:
        conn.close()
    if pending_reconcile:
        return _emit_deploy_reconcile_block(args, pending_reconcile)

    if not ready:
        if not queued and not in_progress:
            payload = {**_results_payload([]), "note": "no queued or ready jobs"}
            if args.json:
                dump_json(payload)
            else:
                _print_run_payload(payload)
            return 0

        # A deploy command is explicit deploy intent, but no push can occur
        # until the combined result has passed validation and the resulting
        # exact plan is shown. Keep JSON to one document by capturing the
        # validation output internally.
        owner = default_owner()
        lease_token = ""
        conn = connect(config.state.db)
        try:
            jobs = claim_all_queued(
                conn,
                owner=owner,
                ttl_minutes=config.queue.lock_ttl_minutes,
            )
            if not jobs:
                validation_payload = {
                    **_results_payload([]),
                    "note": "no queued jobs",
                }
            else:
                lease_token = jobs[0].claim_token
                results = GitRunner(config).process_batch(
                    conn,
                    jobs,
                    deploy=False,
                    keep_worktree=args.keep_worktree,
                    owner=owner,
                    ttl_minutes=config.queue.lock_ttl_minutes,
                    reuse_validated=False,
                    expected_plan_sha="",
                )
                validation_payload = _results_payload(results)
        finally:
            if lease_token:
                release_runner_lock(conn, owner=owner, token=lease_token)
            conn.close()
        if _run_exit_code(validation_payload):
            if args.json:
                dump_json(validation_payload)
            else:
                _print_run_payload(validation_payload)
            return _run_exit_code(validation_payload)
        if not args.json:
            _print_run_payload(validation_payload)

    # Build the preview directly so the command emits exactly one JSON object.
    conn = connect(config.state.db)
    try:
        selected, jobs = select_validated_train(
            conn,
            train_id=getattr(args, "train_id", ""),
        )
        history_events = list_history_events(conn)
    finally:
        conn.close()
    if selected is None or not jobs:
        raise QueueError("no validated train is ready to deploy")
    decision = GitRunner(config).preview_validated_reuse(jobs, authorized=False)
    destination = resolve_git_destination(config)
    plan_sha = deploy_plan_sha(
        config,
        jobs,
        reuse_validated=False,
        destination=destination,
    )
    preview = {
        "ok": True,
        "result": "confirmation_required",
        "push_plan": {
            "atomic": True,
            "remote": config.git.remote,
            "url": destination.display_url,
            "fetch_url": redact_secrets(destination.fetch_url),
            "destination_sha": destination.destination_sha,
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
        "deploy_plan_sha": plan_sha,
        "reuse": reuse_explanation(
            config,
            jobs,
            decision=decision,
            gate_runs=_gate_runs(history_events),
        ),
        "jobs": [job.to_dict() for job in jobs],
    }
    if args.json:
        dump_json(preview)
        return 0

    _render_v3_preview(preview)
    if not sys.stdin.isatty():
        print(
            "Deploy confirmation requires an interactive terminal; nothing was pushed.",
            file=sys.stderr,
        )
        return 2
    accepted = input("Deploy this exact plan? [y/N] ").strip().lower()
    if accepted not in {"y", "yes"}:
        print("Deploy declined; the validated train remains ready.")
        return 0
    return _execute_batch(args, deploy=True, expected_plan=plan_sha)
