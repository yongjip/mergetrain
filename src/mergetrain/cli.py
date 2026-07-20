"""Command-line interface for mergetrain."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import MergetrainConfig, TerminologyConfig, load_config, render_default_config
from .daemon import daemon_loop
from .errors import (
    CommandFailed,
    ConfigError,
    LockHeld,
    MergetrainError,
    QueueError,
    RemoteUnreachable,
)
from .git_runner import (
    GitRunner,
    apply_gc,
    branch_exists,
    find_worktree_gc_candidates,
    git_current_branch,
    git_ref_exists,
    git_remote_exists,
    git_remote_url,
    git_repo_root,
    git_rev_parse,
    git_worktree_clean,
)
from .models import Job
from .observability import (
    event_record,
    heartbeat_record,
    inspect_job_payload,
    stream_terminal,
)
from .recovery import force_unlock, recover, reconcile, sweep_pending_refs
from .runtime import runtime_provenance
from .snapshot import next_action as _doctor_next_action
from .store import (
    cancel_job,
    claim_all_queued,
    claim_deploy_batch,
    claim_next_job,
    connect,
    counts,
    default_owner,
    deploy_reconcile_pending,
    enqueue_job,
    get_job,
    get_lock,
    list_jobs,
    list_run_events,
    list_train_jobs,
    release_runner_lock,
    select_validated_train,
    terminal_branch_candidates,
    validated_train_summaries,
)

GLOBAL_OPTIONS_WITH_VALUES = {"--config", "--repo", "--db"}


def normalize_global_options(argv: Sequence[str]) -> list[str]:
    """Allow global options before or after the subcommand.

    Many coding agents place ``--repo`` or ``--config`` after the subcommand.
    argparse normally rejects that. This lightweight normalizer moves known
    global options to the front while leaving command-specific arguments intact.
    """

    moved: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        matched_equals = False
        for option in GLOBAL_OPTIONS_WITH_VALUES:
            if token.startswith(option + "="):
                moved.append(token)
                matched_equals = True
                break
        if matched_equals:
            index += 1
            continue
        if token in GLOBAL_OPTIONS_WITH_VALUES and index + 1 < len(argv):
            moved.extend([token, argv[index + 1]])
            index += 2
            continue
        rest.append(token)
        index += 1
    return moved + rest


def dump_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def config_from_args(args: argparse.Namespace) -> MergetrainConfig:
    return load_config(config_path=args.config, repo=args.repo, db_override=args.db)


def agent_contract_payload(
    terminology: TerminologyConfig | None = None,
) -> dict[str, Any]:
    words = terminology or TerminologyConfig()
    return {
        "name": "mergetrain agent contract",
        "purpose": "Serialize committed local task branches through one merge/test/push/verify runner.",
        "rules": [
            "Work on a task-specific branch and worktree.",
            "Commit all changes before enqueueing.",
            "Do not push configured Git refs directly; enqueue the branch instead.",
            "Read doctor --json or status --json before deciding the next action.",
            f"Use --auto only after explicit unattended-{words.noun} approval from the user/operator.",
            "Reuse validated gates only after explicit deploy.reuse configuration or --reuse-validated authorization.",
            "Let one runner or daemon own merge, test, push, and verify.",
            "Fix blocked or failed work in the owning branch, commit a clean result, then enqueue a new job.",
            "After a crash, run reconcile/recover to resolve needs_reconcile jobs against the remote before deploying; run reconcile before any manual force-push.",
        ],
        "boundary": {
            "deploy_requires": "run-next --deploy or run-batch --deploy",
            "validate_requires": "run-next --validate-only or run-batch --validate-only",
            "validated_train_deploy": "run-batch --deploy claims one exact validated train",
            "validated_gate_reuse": "disabled by default; requires deploy.reuse.enabled or --reuse-validated",
            "progress_observation": "events, inspect, and logs are read-only; events JSONL resumes by persisted event ID",
            "daemon_processes_only": "jobs enqueued with --auto",
            "hub_observation": "hub serves a read-only aggregate; every repo keeps its own queue, lock, and recovery state",
            "hub_daemon_processes_only": "jobs enqueued with --auto, across registered repos, through each repo's own runner and lock; concurrency caps simultaneous repos machine-wide",
            "destructive_cleanup_requires": "gc --apply; branch deletion also requires --delete-branches",
            "recovery_after_crash": "reconcile / recover / unlock resolve crash state against the remote; run-batch --deploy is refused while any job is needs_reconcile",
        },
        "human_vocabulary": {
            **words.to_dict(),
            "cli_flag": f"--{words.action}",
            "canonical_cli_flag": "--deploy",
            "machine_status": "deployed",
            "machine_fields": ["deploy_sha", "push_status", "verify_status"],
            "scope": "atomic Git ref push only; provider release is a separate post-push action",
        },
    }


def render_agent_contract(terminology: TerminologyConfig | None = None) -> str:
    words = terminology or TerminologyConfig()
    payload = agent_contract_payload(words)
    rules = "\n".join(f"{i}. {rule}" for i, rule in enumerate(payload["rules"], start=1))
    return f"""# mergetrain agent contract

Purpose: {payload['purpose']}

## Rules

{rules}

## Safety boundary

- Git {words.noun} requires `run-next --{words.action}` or `run-batch --{words.action}`; `--deploy` remains the canonical compatibility flag.
- Validation requires `run-next --validate-only` or `run-batch --validate-only`.
- A validated train is {words.completed} as one exact identity by `run-batch --{words.action}`.
- Validated-gate reuse is disabled unless config or `--reuse-validated` explicitly authorizes it.
- `events`, `inspect`, and `logs` are read-only observation commands; event JSONL resumes by ID.
- The daemon processes only jobs enqueued with `--auto`.
- The hub dashboard is a read-only aggregate; every repo keeps its own queue, lock, and recovery state.
- The hub daemon also processes only `--auto` jobs, across registered repos, through each repo's own runner and lock; `--concurrency` caps simultaneous repos machine-wide.
- Destructive cleanup requires `gc --apply`; branch deletion also requires `--delete-branches`.
- After a crash, `reconcile`/`recover` resolve `needs_reconcile` jobs against the remote; `run-batch --{words.action}` is refused while any job is `needs_reconcile`. `unlock --force` clears a wedged lock (remote-reachable first).

## Stable machine contract

- Human output says `{words.action}`, `{words.in_progress}`, and `{words.completed}`.
- JSON/SQLite continue to use `status=deployed`, `deploy_sha`, `push_status`, and `verify_status`.
- This operation is an atomic Git ref push. Configured `deploy.verify` hooks report an independent post-push outcome; a provider release is separate and requires its own authorization.
"""


def cmd_init(args: argparse.Namespace) -> int:
    repo = Path(args.repo or Path.cwd()).expanduser().resolve()
    project = args.project or repo.name or "example-app"
    config_text = render_default_config(project)
    if not args.write:
        print(config_text, end="")
        return 0
    files = {
        repo / ".mergetrain.yaml": config_text,
        repo / "AGENTS.mergetrain.md": render_agent_contract(),
        repo / "CLAUDE.mergetrain.md": render_agent_contract(),
    }
    written: list[str] = []
    for path, content in files.items():
        if path.exists() and not args.force:
            raise ConfigError(f"refusing to overwrite existing file without --force: {path}")
        path.write_text(content, encoding="utf-8")
        written.append(str(path))
    dump_json({"ok": True, "written": written})
    return 0


def cmd_agent_contract(args: argparse.Namespace) -> int:
    terminology = config_from_args(args).terminology
    if args.json:
        dump_json(agent_contract_payload(terminology))
    else:
        print(render_agent_contract(terminology), end="")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    runtime = runtime_provenance()
    if args.json:
        dump_json({"ok": True, "version": __version__, "runtime": runtime})
        return 0
    print(f"mergetrain {__version__}")
    print(f"distribution: {runtime['distribution_version'] or 'unknown'}")
    print(f"install mode: {runtime['install_mode']}")
    print(f"package: {runtime['package_path']}")
    if runtime["source_path"]:
        print(f"source: {runtime['source_path']}")
    print(f"commit: {runtime['source_commit'] or 'unknown'}")
    dirty = runtime["source_dirty"]
    print(f"dirty: {'unknown' if dirty is None else str(dirty).lower()}")
    return 0


def _capture_sha_or_error(path: Path, ref: str, *, label: str) -> str:
    try:
        return git_rev_parse(path, ref)
    except CommandFailed as exc:
        raise QueueError(f"could not capture {label} SHA for {ref}: {exc}") from exc


def cmd_enqueue(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    worktree = Path(args.worktree or Path.cwd()).expanduser().resolve()
    if not args.no_ready_check:
        if not worktree.exists():
            raise QueueError(f"worktree does not exist: {worktree}")
        if not git_repo_root(worktree):
            raise QueueError(f"not a git worktree: {worktree}")
        if not args.allow_dirty and not git_worktree_clean(worktree):
            raise QueueError("worktree is dirty; commit/stash changes or pass --allow-dirty")
        current = git_current_branch(worktree)
        if not args.allow_branch_mismatch and current != args.branch:
            raise QueueError(f"current branch {current!r} does not match --branch {args.branch!r}")
    base_sha = args.base_sha or ""
    head_sha = args.head_sha or ""
    if args.capture_sha:
        base_sha = base_sha or _capture_sha_or_error(config.repo, config.git.integration_ref, label="base")
        head_sha = head_sha or _capture_sha_or_error(worktree, args.branch, label="head")
    conn = connect(config.state.db)
    try:
        job = enqueue_job(
            conn,
            task=args.task,
            branch=args.branch,
            worktree_path=str(worktree),
            base_sha=base_sha,
            head_sha=head_sha,
            note=args.note or "",
            allow_duplicate=args.allow_duplicate,
            auto_deploy=args.auto,
        )
    finally:
        conn.close()
    payload = {"ok": True, "job": job.to_dict()}
    if args.json:
        dump_json(payload)
    else:
        print(f"queued job {job.id}: {job.task} ({job.branch})")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    conn = connect(config.state.db)
    try:
        lock = get_lock(conn)
        validated_trains = validated_train_summaries(conn)
        payload = {
            "ok": True,
            "db": str(config.state.db),
            "lock": lock.to_dict() if lock else None,
            "jobs": [job.to_dict() for job in list_jobs(conn, limit=args.limit)],
            "validated_trains": validated_trains,
        }
    finally:
        conn.close()
    if args.json:
        dump_json(payload)
    else:
        lock_text = payload["lock"]["owner"] if payload["lock"] else "none"
        print(f"db: {payload['db']}")
        print(f"lock: {lock_text}")
        for job in payload["jobs"]:
            print(f"{_job_result_line(job, config.terminology)} - {job['task']}")
    return 0


def _dump_jsonl(payload: dict[str, Any]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        flush=True,
    )


def _event_scope(config: MergetrainConfig, args: argparse.Namespace, after_id: int | None):
    conn = connect(config.state.db)
    try:
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
        lock = get_lock(conn)
        return jobs, events, latest, lock
    finally:
        conn.close()


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
    cursor = args.after
    last_heartbeat = ""
    scoped = args.job_id is not None or bool(args.train_id)
    try:
        while True:
            jobs, events, latest, lock = _event_scope(config, args, cursor)
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


def _read_job_and_lock(config: MergetrainConfig, job_id: int):
    conn = connect(config.state.db)
    try:
        return get_job(conn, job_id), get_lock(conn)
    finally:
        conn.close()


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
    try:
        while True:
            job, lock = _read_job_and_lock(config, args.job_id)
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
                job, lock = _read_job_and_lock(config, args.job_id)
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


def cmd_doctor(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    db_existed_before = config.state.db.exists()
    conn = connect(config.state.db)
    try:
        lock = get_lock(conn)
        count_data = counts(conn)
        validated_trains = validated_train_summaries(conn)
    finally:
        conn.close()
    remote_url = git_remote_url(config.repo, config.git.remote)
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
        },
        "git": {
            "repo_root": git_repo_root(config.repo),
            "current_branch": git_current_branch(config.repo),
            "worktree_clean": git_worktree_clean(config.repo) if git_repo_root(config.repo) else False,
            "remote_url": remote_url,
            "remote_exists": bool(remote_url) or git_remote_exists(config.repo, config.git.remote),
            "integration_ref": config.git.integration_ref,
            "integration_ref_exists": git_ref_exists(config.repo, config.git.integration_ref) if git_repo_root(config.repo) else False,
        },
        "lock": lock.to_dict() if lock else None,
        "counts": count_data,
        "validated_trains": validated_trains,
        "gc": {"worktree_candidates": find_worktree_gc_candidates(config)},
    }
    payload["ok"] = bool(payload["config_exists"] and payload["git"]["repo_root"])
    payload["next_action"] = _doctor_next_action(payload)
    if args.json:
        dump_json(payload)
    else:
        print(f"ok: {payload['ok']}")
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
    return 0


def _mode_from_args(args: argparse.Namespace) -> bool:
    if args.deploy == args.validate_only:
        raise QueueError("choose exactly one: --validate-only, --deploy, --integrate, or --push")
    return bool(args.deploy)


def _emit_deploy_reconcile_block(args: argparse.Namespace, pending: int) -> int:
    note = (
        f"deploy hard-blocked: {pending} job(s) pending reconcile — run "
        "'mergetrain reconcile --apply' first"
    )
    if args.json:
        dump_json(
            {
                "ok": False,
                "result": "blocked",
                "blocked_reason": "reconcile_pending_deploy",
                "needs_reconcile": pending,
                "next_action": "reconcile_pending_deploy",
                "note": note,
            }
        )
    else:
        print(note, file=sys.stderr)
    return 1


def _human_next_action(action: str, terminology: TerminologyConfig) -> str:
    return {
        "deploy_validated_train_when_approved": (
            f"{terminology.action}_validated_train_when_approved"
        ),
        "run_daemon_or_run_batch_deploy_when_approved": (
            f"run_daemon_or_run_batch_{terminology.action}_when_approved"
        ),
    }.get(action, action)


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
        "ok": result == "success",
        "result": result,
        "counts": dict(sorted(status_counts.items())),
        "push_counts": dict(sorted(push_counts.items())),
        "verify_counts": dict(sorted(verify_counts.items())),
        "reused_validation_shas": reused_validation_shas,
        "jobs": [job.to_dict() for job in results],
    }


def _human_category(category: str, terminology: TerminologyConfig) -> str:
    return terminology.completed if category == "deployed" else category


def _job_result_line(
    job: dict[str, Any],
    terminology: TerminologyConfig | None = None,
) -> str:
    words = terminology or TerminologyConfig()
    outcomes: list[str] = []
    if job.get("push_status", "not_run") != "not_run":
        outcomes.append(f"push={job['push_status']}")
    if job.get("verify_status", "not_run") != "not_run":
        outcomes.append(f"verify={job['verify_status']}")
    if job.get("reused_validation_sha"):
        outcomes.append(f"reused={job['reused_validation_sha']}")
    outcome_text = f" ({', '.join(outcomes)})" if outcomes else ""
    status = words.completed if job["status"] == "deployed" else job["status"]
    return f"#{job['id']} {status}{outcome_text}: {job['branch']}"


def _print_run_payload(
    payload: dict[str, Any],
    terminology: TerminologyConfig | None = None,
) -> None:
    if payload.get("jobs"):
        for job_data in payload["jobs"]:
            print(_job_result_line(job_data, terminology))
        if payload.get("result") != "success":
            print(f"result: {payload['result']}")
    else:
        print(payload.get("note", "done"))


def cmd_run_next(args: argparse.Namespace) -> int:
    deploy = _mode_from_args(args)
    config = config_from_args(args)
    owner = default_owner()
    lease_token = ""
    conn = connect(config.state.db)
    try:
        if deploy:
            pending = deploy_reconcile_pending(conn)
            if pending:
                return _emit_deploy_reconcile_block(args, pending)
        job = claim_next_job(conn, owner=owner, ttl_minutes=config.queue.lock_ttl_minutes)
        if job is None:
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
        _print_run_payload(payload, config.terminology)
    return 0 if payload["ok"] else 1


def cmd_run_batch(args: argparse.Namespace) -> int:
    deploy = _mode_from_args(args)
    if args.train_id and not deploy:
        raise QueueError("--train-id requires --deploy, --integrate, or --push")
    if args.reuse_validated and not deploy:
        raise QueueError("--reuse-validated requires --deploy, --integrate, or --push")
    if args.preview and not deploy:
        raise QueueError("--preview requires --deploy, --integrate, or --push")
    config = config_from_args(args)
    if args.preview:
        conn = connect(config.state.db)
        try:
            selected, jobs = select_validated_train(
                conn, train_id=args.train_id or ""
            )
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
            "terminology": config.terminology.to_dict(),
            "push_plan": {
                "atomic": True,
                "remote": config.git.remote,
                "refs": [
                    {"source": "HEAD", "target": ref, "spec": f"HEAD:{ref}"}
                    for ref in config.git.push_refs
                ],
            },
            "train_id": selected["train_id"],
            "reuse": decision.to_dict(),
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
                    f"preview: {config.terminology.action} validated commit "
                    f"{decision.reused_validation_sha} by atomic push to "
                    f"{config.git.remote}: {targets}"
                )
            else:
                print(
                    f"preview: {decision.action} full gates, then "
                    f"{config.terminology.action} by atomic push to "
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
                operation_label=config.terminology.action,
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
        _print_run_payload(payload, config.terminology)
    return 0 if payload["ok"] else 1


def cmd_daemon(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    runner = GitRunner(config)
    owner = default_owner()

    def process_batch(conn, jobs: list[Job]) -> object:  # type: ignore[no-untyped-def]
        return runner.process_batch(
            conn,
            jobs,
            deploy=True,
            keep_worktree=args.keep_worktree,
            owner=owner,
            ttl_minutes=config.queue.lock_ttl_minutes,
        )

    daemon_loop(
        db_path=str(config.state.db),
        process_batch=process_batch,
        owner=owner,
        interval_seconds=args.interval or config.queue.daemon_interval_seconds,
        lock_ttl_minutes=config.queue.lock_ttl_minutes,
        once=args.once,
        say=print,
    )
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    conn = connect(config.state.db)
    try:
        branch_candidates_raw = terminal_branch_candidates(conn)
    finally:
        conn.close()
    protected = set(config.git.push_refs) | {config.git.integration_branch, git_current_branch(config.repo)}
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
        "worktree_candidates": find_worktree_gc_candidates(config),
        "branch_candidates": branch_candidates,
        "result": None,
    }
    if args.apply:
        result = apply_gc(
            config,
            delete_branches=delete_branch_names if args.delete_branches else (),
        )
        conn = connect(config.state.db)
        try:
            result["swept_pending_refs"] = sweep_pending_refs(config, conn)
        finally:
            conn.close()
        payload["result"] = result
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
        dump_json(
            {
                "ok": False,
                "error": {
                    "code": error_code,
                    "message": message,
                    "retryable": exit_code in (3, 7),
                },
            }
        )
    else:
        print(f"mergetrain: {message}", file=sys.stderr)
    return exit_code


def _recovery_next_action(conn) -> str:
    lock = get_lock(conn)
    return _doctor_next_action(
        {
            "lock": lock.to_dict() if lock else None,
            "counts": counts(conn),
            "validated_trains": validated_train_summaries(conn),
            "gc": {"worktree_candidates": []},
        }
    )


def cmd_reconcile(args: argparse.Namespace) -> int:
    try:
        config = config_from_args(args)
    except ConfigError as exc:
        return _emit_recovery_error(args, str(exc), 2, error_code="config_error")
    conn = connect(config.state.db)
    try:
        outcome = reconcile(config, conn, apply=args.apply)
        next_action = _recovery_next_action(conn)
    except LockHeld as exc:
        return _emit_recovery_error(args, str(exc), 3, error_code="lock_held")
    except RemoteUnreachable as exc:
        return _emit_recovery_error(args, str(exc), 7, error_code="remote_unreachable")
    finally:
        conn.close()
    payload = {
        "ok": outcome.exit_code == 0,
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


def cmd_recover(args: argparse.Namespace) -> int:
    try:
        config = config_from_args(args)
    except ConfigError as exc:
        return _emit_recovery_error(args, str(exc), 2, error_code="config_error")
    conn = connect(config.state.db)
    try:
        outcome = recover(config, conn, gc=args.gc, apply=True)
        next_action = _recovery_next_action(conn)
    except LockHeld as exc:
        return _emit_recovery_error(args, str(exc), 3, error_code="lock_held")
    except RemoteUnreachable as exc:
        return _emit_recovery_error(args, str(exc), 7, error_code="remote_unreachable")
    finally:
        conn.close()
    reconciled = outcome.reconcile
    payload = {
        "ok": outcome.exit_code == 0,
        "reconcile": {
            "applied": reconciled.applied,
            "jobs": reconciled.jobs,
            "summary": reconciled.summary,
        },
        "gc": outcome.gc,
        "next_action": next_action,
    }
    if args.json:
        dump_json(payload)
    else:
        summary = reconciled.summary
        print(
            f"recover: {summary['reconciled_deployed']} deployed, "
            f"{summary['requeued']} requeued, {summary['canceled']} canceled, "
            f"{summary['conflicts']} conflict(s)"
        )
        if outcome.gc is not None:
            print(f"gc: removed {len(outcome.gc.get('removed_worktrees', []))} worktree(s)")
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
        next_action = _recovery_next_action(conn)
    except RemoteUnreachable as exc:
        return _emit_recovery_error(args, str(exc), 7, error_code="remote_unreachable")
    finally:
        conn.close()
    payload = {
        "ok": outcome.exit_code == 0,
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


def cmd_cancel(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    conn = connect(config.state.db)
    try:
        job = cancel_job(conn, args.job_id, note=args.note or "")
    finally:
        conn.close()
    if args.json:
        dump_json({"ok": True, "job": job.to_dict()})
    else:
        action = "cancellation requested for" if job.cancel_requested_at else "canceled"
        print(f"{action} job {job.id}: {job.branch}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import serve_dashboard

    host = str(args.host).strip()
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if host not in loopback_hosts and not args.allow_remote:
        raise QueueError(
            "dashboard binds to loopback by default; pass --allow-remote to expose it"
        )
    if not 0 <= args.port <= 65535:
        raise QueueError("dashboard port must be between 0 and 65535")
    config = config_from_args(args)

    def announce(url: str) -> None:
        print(f"mergetrain dashboard: {url}", flush=True)
        print("read-only · press Ctrl-C to stop", flush=True)

    serve_dashboard(config, host=host, port=args.port, preview=args.preview, ready=announce)
    return 0


def cmd_hub_serve(args: argparse.Namespace) -> int:
    from .dashboard import serve_hub
    from .registry import load_registry, registry_path

    host = str(args.host).strip()
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if host not in loopback_hosts and not args.allow_remote:
        raise QueueError(
            "hub binds to loopback by default; pass --allow-remote to expose it"
        )
    if not 0 <= args.port <= 65535:
        raise QueueError("hub port must be between 0 and 65535")
    registered = load_registry(args.registry)
    roster = args.registry or registry_path()

    def announce(url: str) -> None:
        print(f"mergetrain hub: {url}", flush=True)
        print(
            f"read-only · {len(registered)} repo(s) registered in {roster} · press Ctrl-C to stop",
            flush=True,
        )

    serve_hub(host=host, port=args.port, registry=args.registry, ready=announce)
    return 0


def cmd_hub_status(args: argparse.Namespace) -> int:
    from .hub import build_hub_snapshot
    from .registry import load_registry

    snapshot = build_hub_snapshot(load_registry(args.registry))
    if args.json:
        dump_json(snapshot)
        return 0
    if not snapshot["repos"]:
        print("no repos registered; run `mergetrain hub add <repo>`")
        return 0
    for entry in snapshot["repos"]:
        name = entry.get("name") or entry["path"]
        if not entry["ok"]:
            print(f"{name}: ERROR - {entry.get('error', 'unknown')}")
            continue
        if entry.get("empty"):
            print(f"{name}: no queue database yet")
            continue
        repo_snapshot = entry["snapshot"]
        counts = repo_snapshot.get("counts", {})
        active = " ".join(
            f"{key}={counts[key]}"
            for key in ("queued", "in_progress", "blocked", "failed", "needs_reconcile", "validated")
            if counts.get(key)
        )
        lock = repo_snapshot.get("lock")
        runner = "runner=active" if lock and lock.get("liveness") == "alive" else ""
        detail = " ".join(part for part in (active or "idle", runner) if part)
        print(f"{name}: {detail} | next: {repo_snapshot.get('next_action')}")
    return 0


def cmd_hub_daemon(args: argparse.Namespace) -> int:
    from .hub_daemon import hub_daemon_loop

    if args.concurrency < 1:
        raise QueueError("hub daemon --concurrency must be at least 1")
    say = (lambda message: None) if args.json and args.once else print
    outcomes = hub_daemon_loop(
        registry=args.registry,
        interval_seconds=args.interval,
        concurrency=args.concurrency,
        keep_worktree=args.keep_worktree,
        once=args.once,
        say=say,
    )
    if args.json and args.once:
        dump_json({"ok": True, "outcomes": outcomes})
    return 0


def cmd_hub_add(args: argparse.Namespace) -> int:
    from .registry import add_repo, registry_path

    entry = add_repo(args.path, args.registry, daemon=args.daemon)
    if args.json:
        dump_json({"ok": True, "registry": str(args.registry or registry_path()), "entry": entry})
    else:
        suffix = "" if entry.get("daemon", True) else " (hub daemon: excluded)"
        print(f"registered: {entry['path']}{suffix}")
    return 0


def cmd_hub_remove(args: argparse.Namespace) -> int:
    from .registry import registry_path, remove_repo

    removed = remove_repo(args.path, args.registry)
    if args.json:
        dump_json(
            {
                "ok": removed,
                "registry": str(args.registry or registry_path()),
                "removed": removed,
            }
        )
    else:
        print("deregistered" if removed else "not registered; nothing removed")
    return 0 if removed else 1


def cmd_hub_list(args: argparse.Namespace) -> int:
    from .registry import load_registry, registry_path

    entries = load_registry(args.registry)
    if args.json:
        dump_json(
            {
                "ok": True,
                "registry": str(args.registry or registry_path()),
                "repos": entries,
            }
        )
        return 0
    if not entries:
        print("no repos registered; run `mergetrain hub add <repo>`")
        return 0
    for entry in entries:
        suffix = "" if entry.get("daemon", True) else "  [no-daemon]"
        print(f"{entry['path']}{suffix}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mergetrain")
    parser.add_argument("--version", action="version", version=f"mergetrain {__version__}")
    parser.add_argument("--config", help="Path to .mergetrain.yaml")
    parser.add_argument("--repo", default=str(Path.cwd()), help="Repository root or worktree path")
    parser.add_argument("--db", help="Override SQLite DB path")
    subparsers = parser.add_subparsers(dest="command")

    p_init = subparsers.add_parser("init", help="Print or write starter config and agent docs")
    p_init.add_argument("--project", help="Project name for config and worktree prefixes")
    p_init.add_argument("--write", action="store_true", help="Write .mergetrain.yaml and agent docs")
    p_init.add_argument("--force", action="store_true", help="Overwrite generated files")
    p_init.set_defaults(func=cmd_init)

    p_contract = subparsers.add_parser("agent-contract", help="Print agent operating contract")
    p_contract.add_argument("--json", action="store_true")
    p_contract.set_defaults(func=cmd_agent_contract)

    p_version = subparsers.add_parser("version", help="Show version and installed package provenance")
    p_version.add_argument("--json", action="store_true")
    p_version.set_defaults(func=cmd_version)

    p_enqueue = subparsers.add_parser("enqueue", help="Add a task branch to the integration queue")
    p_enqueue.add_argument("--task", required=True)
    p_enqueue.add_argument("--branch", required=True)
    p_enqueue.add_argument("--worktree")
    p_enqueue.add_argument("--base-sha", default="")
    p_enqueue.add_argument("--head-sha", default="")
    p_enqueue.add_argument("--note", default="")
    p_enqueue.add_argument("--allow-duplicate", action="store_true")
    p_enqueue.add_argument("--auto", action="store_true")
    p_enqueue.add_argument("--capture-sha", action="store_true")
    p_enqueue.add_argument("--allow-dirty", action="store_true")
    p_enqueue.add_argument("--allow-branch-mismatch", action="store_true")
    p_enqueue.add_argument("--no-ready-check", action="store_true")
    p_enqueue.add_argument("--json", action="store_true")
    p_enqueue.set_defaults(func=cmd_enqueue)

    p_status = subparsers.add_parser("status", help="Show queue and lock status")
    p_status.add_argument("--json", action="store_true")
    p_status.add_argument("--limit", type=int, default=50)
    p_status.set_defaults(func=cmd_status)

    p_events = subparsers.add_parser(
        "events", help="Read or follow structured runner events"
    )
    event_scope = p_events.add_mutually_exclusive_group()
    event_scope.add_argument("--job", dest="job_id", type=int, help="Scope to one job run history")
    event_scope.add_argument("--train-id", help="Scope to one validated train")
    p_events.add_argument("--after", type=int, help="Resume after this event ID (exclusive)")
    p_events.add_argument("--limit", type=int, default=200)
    p_events.add_argument("--follow", action="store_true")
    p_events.add_argument(
        "--jsonl",
        action="store_true",
        help="Emit one compact JSON object per line",
    )
    p_events.add_argument("--poll-interval", type=float, default=0.5)
    p_events.set_defaults(func=cmd_events)

    p_inspect = subparsers.add_parser(
        "inspect", help="Inspect one job, its latest run, and train outcome"
    )
    p_inspect.add_argument("job_id", type=int)
    p_inspect.add_argument("--event-limit", type=int, default=100)
    p_inspect.add_argument("--json", action="store_true")
    p_inspect.set_defaults(func=cmd_inspect)

    p_logs = subparsers.add_parser("logs", help="Read or follow one job's runner log")
    p_logs.add_argument("job_id", type=int)
    p_logs.add_argument("--follow", action="store_true")
    p_logs.add_argument("--tail", type=int, default=200)
    p_logs.add_argument("--poll-interval", type=float, default=0.5)
    p_logs.set_defaults(func=cmd_logs)

    p_doctor = subparsers.add_parser("doctor", help="Diagnose config, queue, git, and next action")
    p_doctor.add_argument("--json", action="store_true")
    p_doctor.set_defaults(func=cmd_doctor)

    for name, func, help_text in [
        ("run-next", cmd_run_next, "Process one queued job"),
        ("run-batch", cmd_run_batch, "Validate queued jobs or push an exact validated train"),
    ]:
        p_run = subparsers.add_parser(name, help=help_text)
        mode = p_run.add_mutually_exclusive_group(required=True)
        mode.add_argument("--validate-only", action="store_true")
        mode.add_argument("--deploy", action="store_true")
        mode.add_argument("--integrate", dest="deploy", action="store_true")
        mode.add_argument("--push", dest="deploy", action="store_true")
        p_run.add_argument("--keep-worktree", action="store_true")
        p_run.add_argument("--json", action="store_true")
        if name == "run-batch":
            p_run.add_argument("--train-id", help="Push one exact validated train")
            p_run.add_argument(
                "--reuse-validated",
                action="store_true",
                help="Explicitly authorize the configured validated-gate reuse policy",
            )
            p_run.add_argument(
                "--preview",
                action="store_true",
                help="Evaluate a validated train and reuse decision without claiming or pushing",
            )
        p_run.set_defaults(func=func)

    p_daemon = subparsers.add_parser("daemon", help="Run foreground auto-only daemon")
    p_daemon.add_argument("--interval", type=int)
    p_daemon.add_argument("--once", action="store_true")
    p_daemon.add_argument("--keep-worktree", action="store_true")
    p_daemon.set_defaults(func=cmd_daemon)

    p_gc = subparsers.add_parser("gc", help="Clean temporary worktrees and optionally terminal branches")
    p_gc.add_argument("--json", action="store_true")
    p_gc.add_argument("--apply", action="store_true")
    p_gc.add_argument("--delete-branches", action="store_true")
    p_gc.set_defaults(func=cmd_gc)

    p_reconcile = subparsers.add_parser(
        "reconcile",
        help="Resolve crashed pending-deploy jobs against the remote (default: dry-run)",
    )
    p_reconcile.add_argument(
        "--apply", action="store_true", help="Write the reconciled outcome"
    )
    p_reconcile.add_argument("--json", action="store_true")
    p_reconcile.set_defaults(func=cmd_reconcile)

    p_recover = subparsers.add_parser(
        "recover", help="Restart heal: split orphans, then reconcile --apply"
    )
    p_recover.add_argument(
        "--gc", action="store_true", help="Also remove crashed worktrees"
    )
    p_recover.add_argument("--json", action="store_true")
    p_recover.set_defaults(func=cmd_recover)

    p_unlock = subparsers.add_parser(
        "unlock", help="Clear a wedged runner lock"
    )
    p_unlock.add_argument(
        "--force",
        action="store_true",
        help="Steal an alive/unknown owner's lock after confirming the remote is reachable",
    )
    p_unlock.add_argument("--json", action="store_true")
    p_unlock.set_defaults(func=cmd_unlock)

    p_cancel = subparsers.add_parser("cancel", help="Cancel a non-terminal queue item")
    p_cancel.add_argument("job_id", type=int)
    p_cancel.add_argument("--note", default="")
    p_cancel.add_argument("--json", action="store_true")
    p_cancel.set_defaults(func=cmd_cancel)

    p_dashboard = subparsers.add_parser(
        "dashboard", help="Serve the local read-only live dashboard"
    )
    p_dashboard.add_argument("--host", default="127.0.0.1")
    p_dashboard.add_argument("--port", type=int, default=8765)
    p_dashboard.add_argument(
        "--allow-remote",
        action="store_true",
        help="Explicitly allow binding outside the loopback interface",
    )
    p_dashboard.add_argument(
        "--preview",
        action="store_true",
        help="Label the connected database as preview data",
    )
    p_dashboard.set_defaults(func=cmd_dashboard)

    p_hub = subparsers.add_parser(
        "hub",
        help="Serve one read-only dashboard over every registered repo",
    )
    p_hub.add_argument("--host", default="127.0.0.1")
    p_hub.add_argument("--port", type=int, default=8765)
    p_hub.add_argument(
        "--allow-remote",
        action="store_true",
        help="Explicitly allow binding outside the loopback interface",
    )
    p_hub.add_argument("--registry", help="Override the hub registry file path")
    p_hub.set_defaults(func=cmd_hub_serve)
    hub_sub = p_hub.add_subparsers(dest="hub_command")
    p_hub_add = hub_sub.add_parser("add", help="Register a repo with the hub")
    p_hub_add.add_argument("path", nargs="?", default=".")
    p_hub_add.add_argument(
        "--daemon",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Whether `hub daemon` may sweep this repo (--no-daemon: policy opt-out; "
        "re-run add to flip an existing entry; default for new entries: eligible)",
    )
    p_hub_add.add_argument("--registry", help="Override the hub registry file path")
    p_hub_add.add_argument("--json", action="store_true")
    p_hub_add.set_defaults(func=cmd_hub_add)
    p_hub_remove = hub_sub.add_parser("remove", help="Deregister a repo from the hub")
    p_hub_remove.add_argument("path")
    p_hub_remove.add_argument("--registry", help="Override the hub registry file path")
    p_hub_remove.add_argument("--json", action="store_true")
    p_hub_remove.set_defaults(func=cmd_hub_remove)
    p_hub_list = hub_sub.add_parser("list", help="List repos registered with the hub")
    p_hub_list.add_argument("--registry", help="Override the hub registry file path")
    p_hub_list.add_argument("--json", action="store_true")
    p_hub_list.set_defaults(func=cmd_hub_list)
    p_hub_status = hub_sub.add_parser(
        "status",
        help="One machine-wide read of every registered repo's queue",
    )
    p_hub_status.add_argument("--registry", help="Override the hub registry file path")
    p_hub_status.add_argument("--json", action="store_true")
    p_hub_status.set_defaults(func=cmd_hub_status)
    p_hub_daemon = hub_sub.add_parser(
        "daemon",
        help="Run the auto-only daemon across every registered repo",
    )
    p_hub_daemon.add_argument("--interval", type=int, default=15)
    p_hub_daemon.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Max repos running gates at the same time (default 1: machine-wide serial)",
    )
    p_hub_daemon.add_argument("--once", action="store_true", help="Run one sweep and exit")
    p_hub_daemon.add_argument("--keep-worktree", action="store_true")
    p_hub_daemon.add_argument("--registry", help="Override the hub registry file path")
    p_hub_daemon.add_argument("--json", action="store_true", help="With --once, print sweep outcomes as JSON")
    p_hub_daemon.set_defaults(func=cmd_hub_daemon)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(normalize_global_options(raw))
    if not hasattr(args, "func"):
        parser.print_help(sys.stderr)
        return 2
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        if getattr(args, "json", False):
            dump_json({"ok": False, "error": {"code": "interrupted", "message": "interrupted"}})
        else:
            print("mergetrain: interrupted", file=sys.stderr)
        return 130
    except (MergetrainError, CommandFailed, ConfigError, QueueError) as exc:
        if getattr(args, "json", False):
            code = "".join(
                [f"_{char.lower()}" if char.isupper() else char for char in type(exc).__name__]
            ).lstrip("_")
            dump_json(
                {
                    "ok": False,
                    "error": {
                        "code": code,
                        "message": str(exc),
                        "retryable": type(exc).__name__ in {"LockHeld", "LostLease"},
                    },
                }
            )
        else:
            print(f"mergetrain: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
