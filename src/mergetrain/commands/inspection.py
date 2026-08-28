"""Read-only inspection and observability commands."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .. import __version__
from ..cli_support import (
    _dump_jsonl,
    _human_category,
    _human_next_action,
    _job_result_line,
    config_from_args,
    dump_json,
)
from ..command_runner import run_command
from ..config import CONFIG_VERSION, MergetrainConfig
from ..contract import CONTRACT_VERSION
from ..errors import QueueError, redact_secrets
from ..git_ops import (
    find_worktree_gc_candidates,
    git_current_branch,
    git_ref_exists,
    git_remote_exists,
    git_remote_url,
    git_repo_root,
    git_worktree_clean,
)
from ..models import Job
from ..observability import (
    event_record,
    heartbeat_record,
    history_payload,
    inspect_job_payload,
    normalize_since,
    stats_payload,
    stream_terminal,
)
from ..snapshot import next_action as _doctor_next_action
from ..store import (
    connect,
    counts,
    get_job,
    get_lock,
    list_jobs,
    list_run_events,
    list_train_jobs,
    validated_train_summaries,
)


def cmd_status(args: argparse.Namespace) -> int:
    if args.limit < 1:
        raise QueueError("--limit must be 1 or greater")
    config = config_from_args(args)
    conn = connect(config.state.db)
    try:
        lock = get_lock(conn)
        validated_trains = validated_train_summaries(conn)
        payload: dict[str, Any] = {
            "ok": True,
            "db": str(config.state.db),
            "lock": lock.to_dict() if lock else None,
            "jobs": [job.to_dict() for job in list_jobs(conn, limit=args.limit)],
            "validated_trains": validated_trains,
            # CLAUDE.md tells agents to read status --json OR doctor --json
            # before acting; carry next_action on both so the two mandated
            # reads are symmetric.
            "next_action": _doctor_next_action(
                {
                    "lock": lock.to_dict() if lock else None,
                    "counts": counts(conn),
                    "validated_trains": validated_trains,
                    "gc": {"worktree_candidates": []},
                    "config_exists": config.config_exists,
                },
                config_version=config.config_version,
            ),
        }
    finally:
        conn.close()
    if args.json:
        dump_json(payload)
    else:
        lock_text = payload["lock"]["owner"] if payload["lock"] else "none"
        print(f"db: {payload['db']}")
        print(f"lock: {lock_text}")
        print(f"next action: {_human_next_action(payload['next_action'], config.terminology)}")
        for job in payload["jobs"]:
            print(f"{_job_result_line(job, config.terminology)} - {job['task']}")
    return 0


def _resolve_event_scope(conn, args: argparse.Namespace):
    if args.job_id is not None:
        jobs = [get_job(conn, args.job_id)]
        event_job_ids: list[int] | None = [args.job_id]
    elif args.train_id:
        jobs = list_train_jobs(conn, args.train_id)
        if not jobs:
            raise QueueError(f"train not found: {args.train_id}")
        event_job_ids = [job.id for job in jobs]
    else:
        jobs = list_jobs(conn, limit=200)
        event_job_ids = None
    return jobs, event_job_ids


def _event_scope(conn, args: argparse.Namespace, after_id: int | None, event_job_ids):
    events = list_run_events(
        conn,
        limit=args.limit,
        after_id=after_id,
        job_ids=event_job_ids,
    )
    latest = events[-1] if events else None
    if latest is None and args.follow:
        recent = list_run_events(conn, limit=1, job_ids=event_job_ids)
        latest = recent[-1] if recent else None
    return events, latest, get_lock(conn)


def _print_event_record(payload: dict[str, Any], *, jsonl: bool) -> None:
    if jsonl:
        _dump_jsonl(payload)
        return
    if payload["type"] == "event":
        gate = payload.get("gate")
        gate_text = (
            f" gate={gate['index']}/{gate['total']}:{gate['name']}" if gate else ""
        )
        job_text = f" job={payload['job_id']}" if payload.get("job_id") else ""
        print(
            f"#{payload['id']} {payload['created_at']} "
            f"{payload['phase']}/{payload['state']}{job_text}{gate_text} "
            f"{payload['message']}",
            flush=True,
        )
    elif payload["type"] == "heartbeat":
        print(
            f"heartbeat {payload['heartbeat_at']} {payload['phase']} "
            f"elapsed={payload['elapsed_seconds']}s",
            flush=True,
        )
    else:
        print(f"stream ended: {payload['reason']}", flush=True)


def cmd_events(args: argparse.Namespace) -> int:
    if args.after is not None and args.after < 0:
        raise QueueError("--after must be zero or greater")
    if not 1 <= args.limit <= 200:
        raise QueueError("--limit must be between 1 and 200")
    if not 0.05 <= args.poll_interval <= 60:
        raise QueueError("--poll-interval must be between 0.05 and 60 seconds")
    config = config_from_args(args)
    conn = None
    cursor = args.after
    last_heartbeat = ""
    scoped = args.job_id is not None or bool(args.train_id)
    if args.jsonl:
        # A stream header on every connect (including an --after resume, which
        # is a fresh and possibly different-binary process) lets a long-lived
        # consumer re-confirm the contract. It carries no event id, so id-based
        # resume dedupe is unaffected; consumers dispatch JSONL frames on `type`.
        _dump_jsonl(
            {
                "type": "stream_start",
                "contract_version": CONTRACT_VERSION,
                "after_event_id": int(cursor or 0),
            }
        )
    try:
        conn = connect(config.state.db, read_only=True)
        jobs, event_job_ids = _resolve_event_scope(conn, args)
        while True:
            if scoped:
                # The ID scope is immutable for the stream, while job state is
                # refreshed so terminal transitions are still observed.
                jobs = [get_job(conn, job_id) for job_id in event_job_ids or []]
            events, latest, lock = _event_scope(
                conn, args, cursor, event_job_ids
            )
            if not scoped:
                known_ids = {job.id for job in jobs}
                for job_id in dict.fromkeys(
                    event.job_id for event in events if event.job_id is not None
                ):
                    if job_id not in known_ids:
                        jobs.append(get_job(conn, job_id))
                        known_ids.add(job_id)
            for event in events:
                payload = event_record(event, jobs, lock)
                _print_event_record(payload, jsonl=args.jsonl)
                cursor = event.id

            if args.follow and lock and lock.heartbeat_at != last_heartbeat:
                running_tokens = {
                    job.claim_token
                    for job in jobs
                    if job.status == "in_progress" and job.claim_token
                }
                if lock.token in running_tokens:
                    payload = heartbeat_record(
                        jobs,
                        lock,
                        after_event_id=int(cursor or 0),
                        latest_event=latest,
                    )
                    _print_event_record(payload, jsonl=args.jsonl)
                    last_heartbeat = lock.heartbeat_at

            terminal = stream_terminal(jobs, lock) if scoped else None
            if args.follow and terminal is not None and (not events or len(events) < args.limit):
                payload = {
                    "type": "stream_end",
                    "after_event_id": int(cursor or 0),
                    "job_ids": [job.id for job in jobs],
                    **terminal,
                }
                _print_event_record(payload, jsonl=args.jsonl)
                return int(terminal["exit_code"])
            if not args.follow:
                return 0
            if len(events) >= args.limit:
                continue
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        payload = {
            "type": "stream_end",
            "reason": "interrupted",
            "exit_code": 130,
            "after_event_id": int(cursor or 0),
        }
        _print_event_record(payload, jsonl=args.jsonl)
        return 130
    finally:
        if conn is not None:
            conn.close()


def cmd_inspect(args: argparse.Namespace) -> int:
    if not 1 <= args.event_limit <= 200:
        raise QueueError("--event-limit must be between 1 and 200")
    config = config_from_args(args)
    payload = inspect_job_payload(
        config,
        args.job_id,
        event_limit=args.event_limit,
    )
    if args.json:
        dump_json(payload)
    else:
        progress = payload["progress"]
        gate = progress.get("gate")
        gate_text = (
            f" · gate {gate['index']}/{gate['total']} {gate['name']}" if gate else ""
        )
        print(_job_result_line(payload["job"], config.terminology))
        print(
            f"phase: {progress['phase']} · {progress['state']}{gate_text} · "
            f"elapsed {progress['elapsed_seconds']}s"
        )
        print(
            f"heartbeat: {progress['heartbeat_at'] or 'none'} "
            f"({progress['lease_liveness']})"
        )
        print(
            f"outcome: {payload['outcome']['severity']} / "
            f"{_human_category(payload['outcome']['category'], config.terminology)}"
        )
    return 0


def _normalized_since(value: str) -> str:
    try:
        return normalize_since(value)
    except ValueError as exc:
        raise QueueError(str(exc)) from exc


def cmd_history(args: argparse.Namespace) -> int:
    if not 1 <= args.limit <= 1000:
        raise QueueError("--limit must be between 1 and 1000")
    config = config_from_args(args)
    payload = history_payload(
        config,
        since=_normalized_since(args.since),
        limit=args.limit,
    )
    if args.json:
        dump_json(payload)
    else:
        if not payload["items"]:
            print("no history in the selected window")
        for item in payload["items"]:
            duration = item["duration_seconds"]
            duration_text = f"{duration:.3f}s" if duration is not None else "n/a"
            label = item["train_id"] or item["key"]
            print(
                f"{label} {item['status']} jobs={len(item['jobs'])} "
                f"duration={duration_text}"
            )
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    payload = stats_payload(config, since=_normalized_since(args.since))
    if args.json:
        dump_json(payload)
    else:
        trains = payload["trains"]
        rate = trains["land_rate"]
        rate_text = f"{rate * 100:.1f}%" if rate is not None else "n/a"
        print(
            f"trains: {trains['total']} total · {trains['landed']} landed · "
            f"{trains['blocked']} blocked · {trains['failed']} failed · "
            f"{trains['canceled']} canceled · {trains['open']} open"
        )
        print(f"land rate: {rate_text}")
        terminal_rate = trains["terminal_land_rate"]
        terminal_rate_text = (
            f"{terminal_rate * 100:.1f}%" if terminal_rate is not None else "n/a"
        )
        print(
            f"terminal land rate: {terminal_rate_text} "
            "(includes canceled outcomes)"
        )
        reasons = {
            reason: count
            for reason, count in payload["outcomes"][
                "not_landed_reason_counts"
            ].items()
            if count
        }
        if reasons:
            print(
                "not landed: "
                + " · ".join(
                    f"{reason.replace('_', ' ')}={count}"
                    for reason, count in reasons.items()
                )
            )
        print(
            f"duration: median={payload['median_duration_seconds']}s "
            f"p95={payload['p95_duration_seconds']}s · "
            f"average queue={payload['average_queue_seconds']}s"
        )
        validation_runs = payload["validation"]["runs"]
        validation_failure_rate = validation_runs["failure_rate"]
        validation_failure_text = (
            f"{validation_failure_rate * 100:.1f}%"
            if validation_failure_rate is not None
            else "n/a"
        )
        print(
            f"validation runs: attempted={validation_runs['attempted']} · "
            f"with failure={validation_runs['runs_with_failure']}/"
            f"{validation_runs['failure_rate_denominator']} "
            f"({validation_failure_text})"
        )
        validated_trains = payload["validation"]["trains"]
        deployment_rate = validated_trains["deployment_rate"]
        deployment_rate_text = (
            f"{deployment_rate * 100:.1f}%"
            if deployment_rate is not None
            else "n/a"
        )
        print(
            f"validated trains: deployed={validated_trains['deployed']}/"
            f"{validated_trains['total']} ({deployment_rate_text}) · "
            f"pending={validated_trains['pending']} · "
            f"superseded={validated_trains['superseded']}"
        )
        batching = payload["batching"]
        jobs_per_run = batching["jobs_per_run"]
        multi_rate = batching["multi_job_run_rate"]
        multi_rate_text = (
            f"{multi_rate * 100:.1f}%" if multi_rate is not None else "n/a"
        )
        print(
            f"batching: observed={batching['observed_runs']} · "
            f"jobs/run median={jobs_per_run['median']} "
            f"p95={jobs_per_run['p95']} · "
            f"multi-job={batching['multi_job_runs']}/"
            f"{batching['runs_with_job_count']} ({multi_rate_text})"
        )
        savings = batching["estimated_savings"]
        print(
            "batch savings estimate: "
            f"gate executions={savings['estimated_gate_executions_avoided']} · "
            f"gate seconds={savings['estimated_gate_seconds_avoided']}"
        )
        recovery = payload["recovery"]
        operation_counts = recovery["operation_counts"]
        print(
            "recovery operations: "
            f"observed={recovery['observed_invocations']} · "
            f"reconcile={operation_counts['reconcile']} · "
            f"recover={operation_counts['recover']} · "
            f"incomplete={recovery['state_counts']['incomplete']} · "
            f"tracking since={recovery['tracking_started_at']}"
        )
        for gate in payload["gates"]:
            print(
                f"gate {gate['name']}: runs={gate['runs']} "
                f"median={gate['median_seconds']}s p95={gate['p95_seconds']}s"
            )
        latency = payload["latency"]
        for label, timing in (
            ("queue wait", latency["queue_wait"]),
            ("approval wait", latency["approval_wait"]),
            ("validation run", latency["runs"]["validate"]),
            ("deploy run", latency["runs"]["deploy"]),
        ):
            print(
                f"{label}: samples={timing['samples']} "
                f"median={timing['median_seconds']}s "
                f"p95={timing['p95_seconds']}s"
            )
        for phase in latency["phases"]:
            print(
                f"phase {phase['mode']}/{phase['name']}: "
                f"samples={phase['samples']} "
                f"median={phase['median_seconds']}s "
                f"p95={phase['p95_seconds']}s"
            )
        for recommendation in payload["recommendations"]:
            print(
                f"recommendation {recommendation['code']}: "
                f"{recommendation['summary']}"
            )
        for gap in payload["evidence_gaps"]:
            print(f"evidence gap {gap['metric']}: {gap['reason']}")
    return 0


def _read_job_and_lock(conn, job_id: int):
    return get_job(conn, job_id), get_lock(conn)


def _safe_log_path(config: MergetrainConfig, job: Job) -> Path | None:
    if not job.log_path:
        return None
    root = config.state.logs.expanduser().resolve()
    candidate = Path(job.log_path).expanduser().resolve()
    if candidate != root and root not in candidate.parents:
        raise QueueError(
            f"refusing log path outside configured state.logs directory: {candidate}"
        )
    return candidate


def cmd_logs(args: argparse.Namespace) -> int:
    if args.tail < 0:
        raise QueueError("--tail must be zero or greater")
    if not 0.05 <= args.poll_interval <= 60:
        raise QueueError("--poll-interval must be between 0.05 and 60 seconds")
    config = config_from_args(args)
    conn = connect(config.state.db, read_only=True)
    try:
        while True:
            job, lock = _read_job_and_lock(conn, args.job_id)
            log_path = _safe_log_path(config, job)
            if log_path is not None and log_path.exists():
                break
            terminal = stream_terminal([job], lock)
            if not args.follow or terminal is not None:
                raise QueueError(f"log is not available for job {job.id}")
            time.sleep(args.poll_interval)

        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
            if args.tail:
                sys.stdout.writelines(lines[-args.tail :])
                sys.stdout.flush()
            if not args.follow:
                return 0
            while True:
                chunk = handle.read()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                job, lock = _read_job_and_lock(conn, args.job_id)
                terminal = stream_terminal([job], lock)
                if terminal is not None:
                    quiet_polls = 0
                    while quiet_polls < 2:
                        time.sleep(args.poll_interval)
                        trailing = handle.read()
                        if trailing:
                            sys.stdout.write(trailing)
                            sys.stdout.flush()
                            quiet_polls = 0
                        else:
                            quiet_polls += 1
                    return int(terminal["exit_code"])
                time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        print("mergetrain: log follow interrupted", file=sys.stderr)
        return 130
    finally:
        conn.close()


def _git_object_sha(
    repo: Path, arguments: Sequence[str]
) -> str:
    completed = run_command(
        ["git", *arguments],
        cwd=repo,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _config_drift(config: MergetrainConfig, *, repo_root: str) -> dict[str, Any]:
    local_path = config.config_path.resolve()
    local_exists = config.config_exists and local_path.is_file()
    payload: dict[str, Any] = {
        "state": "unavailable",
        "comparable": False,
        "matches": None,
        "local": {
            "exists": local_exists,
            "path": str(local_path),
            "blob_sha": "",
        },
        "integration": {
            "ref": config.git.integration_ref,
            "ref_exists": False,
            "config_exists": False,
            "blob_sha": "",
        },
    }
    if not repo_root:
        payload["state"] = "git_unavailable"
        return payload
    repo_path = Path(repo_root).resolve()
    try:
        relative_path = local_path.relative_to(repo_path).as_posix()
    except ValueError:
        payload["state"] = "config_outside_repo"
        return payload
    payload["local"]["path"] = relative_path
    if not local_exists:
        payload["state"] = "local_config_missing"
        return payload

    integration_ref = config.git.integration_ref
    ref_exists = git_ref_exists(config.repo, integration_ref)
    payload["integration"]["ref_exists"] = ref_exists
    if not ref_exists:
        payload["state"] = "integration_ref_missing"
        return payload

    local_sha = _git_object_sha(
        repo_path,
        ["hash-object", "--", relative_path],
    )
    integration_sha = _git_object_sha(
        repo_path,
        ["rev-parse", "--verify", f"{integration_ref}:{relative_path}"],
    )
    payload["local"]["blob_sha"] = local_sha
    payload["integration"]["blob_sha"] = integration_sha
    payload["integration"]["config_exists"] = bool(integration_sha)
    if not local_sha:
        payload["state"] = "local_config_unreadable"
        return payload
    if not integration_sha:
        payload["state"] = "integration_config_missing"
        return payload

    matches = local_sha == integration_sha
    payload["state"] = "in_sync" if matches else "drifted"
    payload["comparable"] = True
    payload["matches"] = matches
    return payload


def _doctor_recommendations(
    config_drift: dict[str, Any],
) -> list[dict[str, Any]]:
    if config_drift["state"] != "drifted":
        return []
    return [
        {
            "code": "operator_config_drift",
            "severity": "warning",
            "summary": (
                "The operator checkout configuration differs from the "
                "known integration-ref configuration."
            ),
            "evidence": {
                "local_blob_sha": config_drift["local"]["blob_sha"],
                "integration_ref": config_drift["integration"]["ref"],
                "integration_blob_sha": config_drift["integration"]["blob_sha"],
            },
            "actions": [
                "review the configuration diff before queue-advancing commands",
                "synchronize a clean operator checkout without discarding local work",
            ],
        }
    ]


def cmd_doctor(args: argparse.Namespace) -> int:
    from ..runtime import runtime_provenance

    config = config_from_args(args)
    db_existed_before = config.state.db.exists()
    conn = connect(config.state.db)
    try:
        lock = get_lock(conn)
        count_data = counts(conn)
        validated_trains = validated_train_summaries(conn)
    finally:
        conn.close()
    remote_url = redact_secrets(git_remote_url(config.repo, config.git.remote))
    repo_root = git_repo_root(config.repo)
    config_drift = _config_drift(config, repo_root=repo_root)
    payload: dict[str, Any] = {
        "ok": True,
        "version": __version__,
        "runtime": runtime_provenance(),
        "config": config.to_dict(),
        "config_exists": config.config_exists,
        "db": str(config.state.db),
        "db_existed_before": db_existed_before,
        "state": {
            "logs": str(config.state.logs),
            "worktree_root": str(config.state.worktree_root),
            "validation_workspace": {
                "mode": config.state.validation_workspace.mode,
                "path": str(config.validation_worktree_path),
                "exists": config.validation_worktree_path.exists(),
                "cache_key": config.state.validation_workspace.cache_key,
                "cache_paths": list(
                    config.state.validation_workspace.cache_paths
                ),
                "initialized": (
                    config.state.worktree_root
                    / f".{config.project.name}-validation-workspace.json"
                ).is_file(),
            },
        },
        "git": {
            "repo_root": repo_root,
            "current_branch": git_current_branch(config.repo),
            "worktree_clean": git_worktree_clean(config.repo) if repo_root else False,
            "remote_url": remote_url,
            "remote_exists": bool(remote_url) or git_remote_exists(config.repo, config.git.remote),
            "integration_ref": config.git.integration_ref,
            "integration_ref_exists": git_ref_exists(config.repo, config.git.integration_ref) if repo_root else False,
        },
        "config_drift": config_drift,
        "recommendations": _doctor_recommendations(config_drift),
        "lock": lock.to_dict() if lock else None,
        "counts": count_data,
        "validated_trains": validated_trains,
        "gc": {
            "worktree_candidates": find_worktree_gc_candidates(
                config,
                protect=(
                    [lock.worktree_path]
                    if lock and lock.worktree_path and lock.liveness != "dead"
                    else []
                ),
            )
        },
    }
    # `ok` means only "the command ran without an error envelope" (contract 1);
    # the repo-health verdict moves to its own field so a healthy-but-unconfigured
    # repo no longer reads as ok:false.
    payload["health"] = bool(payload["config_exists"] and payload["git"]["repo_root"])
    payload["next_action"] = _doctor_next_action(
        payload, config_version=config.config_version
    )
    if config.config_version > CONFIG_VERSION:
        # A too-new config: the deploy path is fail-closed, but doctor still
        # runs and points the operator at the fix (recovery stays permitted).
        payload["config_version_supported"] = CONFIG_VERSION
    if args.json:
        dump_json(payload)
    else:
        print(f"health: {payload['health']}")
        print(f"config: {payload['config']['config_path']} ({'found' if payload['config_exists'] else 'default'})")
        print(f"db: {payload['db']}")
        print(f"git repo: {payload['git']['repo_root'] or 'not found'}")
        runtime = payload["runtime"]
        print(
            "runtime: "
            f"{runtime['install_mode']} · {runtime['source_commit'] or 'unknown'} · "
            f"{runtime['package_path']}"
        )
        print(
            "next action: "
            f"{_human_next_action(payload['next_action'], config.terminology)}"
        )
        for recommendation in payload["recommendations"]:
            print(
                f"warning {recommendation['code']}: "
                f"{recommendation['summary']}"
            )
    return 0
