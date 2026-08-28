"""Argument parsing and dispatch for the mergetrain CLI."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .cli_support import (
    _dump_jsonl,
    _error_payload,
    _job_result_line,
    config_from_args,
    dump_json,
    normalize_global_options,
)
from .commands.daemon import cmd_daemon
from .commands.deploy import (
    _results_payload,
    _run_exit_code,
    cmd_run_batch,
    cmd_run_next,
)
from .commands.hub import (
    cmd_dashboard,
    cmd_hub_add,
    cmd_hub_daemon,
    cmd_hub_list,
    cmd_hub_remove,
    cmd_hub_serve,
    cmd_hub_status,
)
from .commands.inspection import (
    cmd_doctor,
    cmd_events,
    cmd_history,
    cmd_inspect,
    cmd_logs,
    cmd_stats,
    cmd_status,
)
from .commands.queue import (
    cmd_cancel,
    cmd_dismiss,
    cmd_enqueue,
    cmd_retry,
    cmd_supersede,
)
from .commands.recovery import cmd_gc, cmd_reconcile, cmd_recover, cmd_unlock, cmd_verify
from .commands.setup import (
    agent_contract_payload,
    cmd_agent_contract,
    cmd_demo,
    cmd_init,
    cmd_mcp,
    cmd_version,
    render_agent_contract,
)
from .errors import CommandFailed, ConfigError, MergetrainError, QueueError

__all__ = [
    "_job_result_line",
    "_results_payload",
    "_run_exit_code",
    "agent_contract_payload",
    "config_from_args",
    "main",
    "normalize_global_options",
    "render_agent_contract",
]

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

    p_demo = subparsers.add_parser(
        "demo", help="Run a local-only semantic-conflict walkthrough"
    )
    p_demo.add_argument(
        "--dir",
        dest="directory",
        help="Empty or absent sandbox directory (default: a temporary directory)",
    )
    p_demo.add_argument("--keep", action="store_true", help="Keep the sandbox after success")
    p_demo.add_argument("--pause", action="store_true", help="Wait for Enter between steps")
    p_demo.add_argument(
        "--brief",
        action="store_true",
        help="Show only milestone results (useful for recordings and presentations)",
    )
    p_demo.set_defaults(func=cmd_demo)

    p_mcp = subparsers.add_parser(
        "mcp", help="Serve the queue to coding agents over MCP (stdio)"
    )
    p_mcp.set_defaults(func=cmd_mcp)

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

    p_retry = subparsers.add_parser(
        "retry",
        help="Dismiss a fixed blocked/failed job and enqueue a fresh SHA-pinned job",
    )
    p_retry.add_argument("job_id", type=int)
    p_retry.add_argument(
        "--rebase",
        action="store_true",
        help="Fetch and rebase the owning branch before replacing the queue job",
    )
    p_retry.add_argument("--json", action="store_true")
    p_retry.set_defaults(func=cmd_retry)

    p_supersede = subparsers.add_parser(
        "supersede",
        help=(
            "Atomically retire a validated train and enqueue SHA-pinned "
            "replacement work"
        ),
    )
    p_supersede.add_argument("--train-id", required=True)
    p_supersede.add_argument(
        "--replacement",
        action="append",
        nargs=3,
        required=True,
        metavar=("TASK", "BRANCH", "WORKTREE"),
        help=(
            "Replacement task, branch, and clean owning worktree; repeat for "
            "a replacement set"
        ),
    )
    p_supersede.add_argument("--note", default="")
    p_supersede.add_argument("--json", action="store_true")
    p_supersede.set_defaults(func=cmd_supersede)

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

    p_history = subparsers.add_parser(
        "history", help="Show retained train/job history and gate outcomes"
    )
    p_history.add_argument("--since", default="", help="ISO-8601 lower time bound")
    p_history.add_argument("--limit", type=int, default=50)
    p_history.add_argument("--json", action="store_true")
    p_history.set_defaults(func=cmd_history)

    p_stats = subparsers.add_parser(
        "stats",
        help="Aggregate outcomes, validation, batching, latency, and gate timing",
    )
    p_stats.add_argument("--since", default="", help="ISO-8601 lower time bound")
    p_stats.add_argument("--json", action="store_true")
    p_stats.set_defaults(func=cmd_stats)

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
    p_daemon.add_argument(
        "--notify",
        action="store_true",
        help="Notify configured transitions via the optional webhook",
    )
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

    p_dismiss = subparsers.add_parser(
        "dismiss",
        help="Clear a superseded blocked/failed job (non-destructive; not for queued/in-progress)",
    )
    p_dismiss.add_argument("job_id", type=int, nargs="?", help="Job to dismiss (or use --all)")
    p_dismiss.add_argument("--all", action="store_true", help="Dismiss every blocked/failed job")
    p_dismiss.add_argument("--note", default="")
    p_dismiss.add_argument("--json", action="store_true")
    p_dismiss.set_defaults(func=cmd_dismiss)

    p_verify = subparsers.add_parser(
        "verify",
        help="Discharge deployed jobs left verify_status='unknown' by a crash",
    )
    p_verify.add_argument("--job", type=int, help="Resolve one job (default: all unresolved)")
    p_verify.add_argument(
        "--ack",
        choices=["succeeded", "failed"],
        help="Mark the result without re-running hooks (for non-repeatable verifies)",
    )
    p_verify.add_argument("--json", action="store_true")
    p_verify.set_defaults(func=cmd_verify)

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
    p_hub_daemon.add_argument(
        "--notify",
        action="store_true",
        help="Notify each repo's configured transitions via its optional webhook",
    )
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
        error_payload = _error_payload(
            "interrupted", "interrupted", retryable=False
        )
        if getattr(args, "json", False):
            # Route through the single builder so Ctrl-C emits the same
            # {code,message,retryable} shape as every other failure — a consumer
            # doing resp["error"]["retryable"] must not KeyError on interrupt.
            dump_json(error_payload)
        elif getattr(args, "jsonl", False):
            _dump_jsonl(
                {
                    "type": "stream_end",
                    "reason": "interrupted",
                    "exit_code": 130,
                    "ok": False,
                    "error": error_payload["error"],
                }
            )
        else:
            print("mergetrain: interrupted", file=sys.stderr)
        return 130
    except (MergetrainError, CommandFailed, ConfigError, QueueError) as exc:
        code = "".join(
            [f"_{char.lower()}" if char.isupper() else char for char in type(exc).__name__]
        ).lstrip("_")
        error_payload = _error_payload(
            code,
            str(exc),
            retryable=type(exc).__name__ in {"LockHeld", "LostLease", "QueueBusy"},
        )
        if getattr(args, "json", False):
            dump_json(error_payload)
        elif getattr(args, "jsonl", False):
            _dump_jsonl(
                {
                    "type": "stream_end",
                    "reason": "error",
                    "exit_code": 1,
                    "ok": False,
                    "error": error_payload["error"],
                }
            )
        else:
            print(f"mergetrain: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
