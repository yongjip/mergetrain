"""Command-line interface for trainyard."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import TrainyardConfig, load_config, render_default_config
from .daemon import daemon_loop
from .errors import CommandFailed, ConfigError, QueueError, TrainyardError
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
from .store import (
    cancel_job,
    claim_all_queued,
    claim_next_job,
    connect,
    counts,
    default_owner,
    enqueue_job,
    get_lock,
    list_jobs,
    release_runner_lock,
    terminal_branch_candidates,
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


def config_from_args(args: argparse.Namespace) -> TrainyardConfig:
    return load_config(config_path=args.config, repo=args.repo, db_override=args.db)


def agent_contract_payload() -> dict[str, Any]:
    return {
        "name": "trainyard agent contract",
        "purpose": "Serialize committed local task branches through one merge/test/push/verify runner.",
        "rules": [
            "Work on a task-specific branch and worktree.",
            "Commit all changes before enqueueing.",
            "Do not push deploy refs directly; enqueue the branch instead.",
            "Read doctor --json or status --json before deciding the next action.",
            "Use --auto only after explicit unattended-deploy approval from the user/operator.",
            "Let one runner or daemon own merge, test, push, and verify.",
            "Fix blocked or failed work in the owning branch, commit a clean result, then enqueue a new job.",
        ],
        "boundary": {
            "deploy_requires": "run-next --deploy or run-batch --deploy",
            "validate_requires": "run-next --validate-only or run-batch --validate-only",
            "daemon_processes_only": "jobs enqueued with --auto",
            "destructive_cleanup_requires": "gc --apply; branch deletion also requires --delete-branches",
        },
    }


def render_agent_contract() -> str:
    payload = agent_contract_payload()
    rules = "\n".join(f"{i}. {rule}" for i, rule in enumerate(payload["rules"], start=1))
    return f"""# trainyard agent contract

Purpose: {payload['purpose']}

## Rules

{rules}

## Safety boundary

- Deploy requires `run-next --deploy` or `run-batch --deploy`.
- Validation requires `run-next --validate-only` or `run-batch --validate-only`.
- The daemon processes only jobs enqueued with `--auto`.
- Destructive cleanup requires `gc --apply`; branch deletion also requires `--delete-branches`.
"""


def cmd_init(args: argparse.Namespace) -> int:
    repo = Path(args.repo or Path.cwd()).expanduser().resolve()
    project = args.project or repo.name or "example-app"
    config_text = render_default_config(project)
    if not args.write:
        print(config_text, end="")
        return 0
    files = {
        repo / ".trainyard.yaml": config_text,
        repo / "AGENTS.trainyard.md": render_agent_contract(),
        repo / "CLAUDE.trainyard.md": render_agent_contract(),
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
    if args.json:
        dump_json(agent_contract_payload())
    else:
        print(render_agent_contract(), end="")
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
        payload = {
            "ok": True,
            "db": str(config.state.db),
            "lock": lock.to_dict() if lock else None,
            "jobs": [job.to_dict() for job in list_jobs(conn, limit=args.limit)],
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
            print(f"#{job['id']} {job['status']} {job['branch']} - {job['task']}")
    return 0


def _doctor_next_action(payload: dict[str, Any]) -> str:
    lock = payload.get("lock")
    count_data = payload.get("counts") or {}
    if lock and lock.get("liveness") == "alive":
        return "wait_for_runner"
    if count_data.get("blocked", 0) or count_data.get("failed", 0):
        return "fix_blocked_job"
    if count_data.get("auto_queued", 0):
        return "run_daemon_or_run_batch_deploy_when_approved"
    if count_data.get("queued", 0):
        return "run_batch_validate"
    if payload.get("gc", {}).get("worktree_candidates"):
        return "gc_available"
    return "enqueue_clean_branch"


def cmd_doctor(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    db_existed_before = config.state.db.exists()
    conn = connect(config.state.db)
    try:
        lock = get_lock(conn)
        count_data = counts(conn)
    finally:
        conn.close()
    remote_url = git_remote_url(config.repo, config.git.remote)
    payload: dict[str, Any] = {
        "ok": True,
        "version": __version__,
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
        print(f"next action: {payload['next_action']}")
    return 0


def _mode_from_args(args: argparse.Namespace) -> bool:
    if args.deploy == args.validate_only:
        raise QueueError("choose exactly one: --validate-only or --deploy")
    return bool(args.deploy)


def _results_payload(results: list[Job]) -> dict[str, Any]:
    return {"ok": True, "jobs": [job.to_dict() for job in results]}


def cmd_run_next(args: argparse.Namespace) -> int:
    deploy = _mode_from_args(args)
    config = config_from_args(args)
    owner = default_owner()
    conn = connect(config.state.db)
    try:
        job = claim_next_job(conn, owner=owner, ttl_minutes=config.queue.lock_ttl_minutes)
        if job is None:
            payload = {"ok": True, "jobs": [], "note": "no queued jobs"}
        else:
            result = GitRunner(config).process_one(conn, job, deploy=deploy, keep_worktree=args.keep_worktree)
            payload = _results_payload([result])
    finally:
        release_runner_lock(conn, owner=owner)
        conn.close()
    if args.json:
        dump_json(payload)
    else:
        if payload.get("jobs"):
            for job_data in payload["jobs"]:
                print(f"#{job_data['id']} {job_data['status']}: {job_data['branch']}")
        else:
            print(payload.get("note", "done"))
    return 0


def cmd_run_batch(args: argparse.Namespace) -> int:
    deploy = _mode_from_args(args)
    config = config_from_args(args)
    owner = default_owner()
    conn = connect(config.state.db)
    try:
        jobs = claim_all_queued(conn, owner=owner, ttl_minutes=config.queue.lock_ttl_minutes)
        if not jobs:
            payload = {"ok": True, "jobs": [], "note": "no queued jobs"}
        else:
            results = GitRunner(config).process_batch(conn, jobs, deploy=deploy, keep_worktree=args.keep_worktree)
            payload = _results_payload(results)
    finally:
        release_runner_lock(conn, owner=owner)
        conn.close()
    if args.json:
        dump_json(payload)
    else:
        if payload.get("jobs"):
            for job_data in payload["jobs"]:
                print(f"#{job_data['id']} {job_data['status']}: {job_data['branch']}")
        else:
            print(payload.get("note", "done"))
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    runner = GitRunner(config)

    def process_batch(conn, jobs: list[Job]) -> object:  # type: ignore[no-untyped-def]
        return runner.process_batch(conn, jobs, deploy=True, keep_worktree=args.keep_worktree)

    daemon_loop(
        db_path=str(config.state.db),
        process_batch=process_batch,
        owner=default_owner(),
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
        payload["result"] = apply_gc(
            config,
            delete_branches=delete_branch_names if args.delete_branches else (),
        )
    if args.json:
        dump_json(payload)
    else:
        print(f"worktree candidates: {len(payload['worktree_candidates'])}")
        print(f"branch candidates: {len(payload['branch_candidates'])}")
        if args.apply:
            print("applied")
        else:
            print("dry-run; pass --apply to remove candidates")
    return 0


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
        print(f"canceled job {job.id}: {job.branch}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trainyard")
    parser.add_argument("--version", action="version", version=f"trainyard {__version__}")
    parser.add_argument("--config", help="Path to .trainyard.yaml")
    parser.add_argument("--repo", default=str(Path.cwd()), help="Repository root or worktree path")
    parser.add_argument("--db", help="Override SQLite DB path")
    subparsers = parser.add_subparsers(dest="command")

    p_init = subparsers.add_parser("init", help="Print or write starter config and agent docs")
    p_init.add_argument("--project", help="Project name for config and worktree prefixes")
    p_init.add_argument("--write", action="store_true", help="Write .trainyard.yaml and agent docs")
    p_init.add_argument("--force", action="store_true", help="Overwrite generated files")
    p_init.set_defaults(func=cmd_init)

    p_contract = subparsers.add_parser("agent-contract", help="Print agent operating contract")
    p_contract.add_argument("--json", action="store_true")
    p_contract.set_defaults(func=cmd_agent_contract)

    p_enqueue = subparsers.add_parser("enqueue", help="Add a task branch to the deploy queue")
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

    p_doctor = subparsers.add_parser("doctor", help="Diagnose config, queue, git, and next action")
    p_doctor.add_argument("--json", action="store_true")
    p_doctor.set_defaults(func=cmd_doctor)

    for name, func, help_text in [
        ("run-next", cmd_run_next, "Process one queued job"),
        ("run-batch", cmd_run_batch, "Process all queued jobs as one merge train"),
    ]:
        p_run = subparsers.add_parser(name, help=help_text)
        mode = p_run.add_mutually_exclusive_group(required=True)
        mode.add_argument("--validate-only", action="store_true")
        mode.add_argument("--deploy", action="store_true")
        p_run.add_argument("--keep-worktree", action="store_true")
        p_run.add_argument("--json", action="store_true")
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

    p_cancel = subparsers.add_parser("cancel", help="Cancel a non-terminal queue item")
    p_cancel.add_argument("job_id", type=int)
    p_cancel.add_argument("--note", default="")
    p_cancel.add_argument("--json", action="store_true")
    p_cancel.set_defaults(func=cmd_cancel)
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
        print("trainyard: interrupted", file=sys.stderr)
        return 130
    except (TrainyardError, CommandFailed, ConfigError, QueueError) as exc:
        print(f"trainyard: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
