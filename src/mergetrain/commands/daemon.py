"""Single-repository daemon command."""

from __future__ import annotations

import argparse
import sys

from ..cli_support import _preflight_config, config_from_args
from ..config import MergetrainConfig
from ..daemon import daemon_loop
from ..deploy_plan import deploy_destination_sha, deploy_execution_policy_sha
from ..errors import QueueError
from ..git_runner import GitRunner
from ..models import Job
from ..store import default_owner


def cmd_daemon(args: argparse.Namespace) -> int:
    from ..notify import configured_notifier, repo_notify_state_path

    config = config_from_args(args)
    # An unattended daemon is the most dangerous place to ship against a config
    # this binary cannot honor (too new) or against guessed defaults (absent).
    # Reject both before the loop can claim and deploy a single auto job (#84,
    # defect 6).
    _preflight_config(config)
    if args.validate_only and args.notify:
        raise QueueError("daemon --validate-only cannot be combined with --notify")
    if args.notify and not config.notify.webhook_url:
        print(
            "mergetrain warning: --notify requested but notify.webhook_url is not "
            "configured; no headless notification backend is active",
            file=sys.stderr,
        )
    owner = default_owner()
    approval_snapshot: list[MergetrainConfig] = []

    def current_config() -> MergetrainConfig:
        latest = config_from_args(args)
        _preflight_config(latest)
        return latest

    def current_destination_sha() -> str:
        latest = current_config()
        approval_snapshot[:] = [latest]
        return deploy_destination_sha(latest)

    def current_execution_policy_sha() -> str:
        latest = approval_snapshot.pop() if approval_snapshot else current_config()
        return deploy_execution_policy_sha(latest)

    def process_batch(conn, jobs: list[Job]) -> object:  # type: ignore[no-untyped-def]
        latest = current_config()
        return GitRunner(latest).process_batch(
            conn,
            jobs,
            deploy=not args.validate_only,
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
        notifier=configured_notifier(config.notify) if args.notify else None,
        notification_name=config.project.name,
        notification_path=str(config.repo),
        notification_transitions=config.notify.transitions,
        notification_state_path=repo_notify_state_path(config.state.db),
        validate_only=args.validate_only,
        approval_destination_sha=(
            "" if args.validate_only else current_destination_sha
        ),
        approval_execution_policy_sha=(
            ""
            if args.validate_only
            else current_execution_policy_sha
        ),
    )
    return 0
