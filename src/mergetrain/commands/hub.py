"""Dashboard and multi-repository hub commands."""

from __future__ import annotations

import argparse
import sys

from ..cli_support import config_from_args, dump_json
from ..config import load_config
from ..errors import QueueError


def cmd_dashboard(args: argparse.Namespace) -> int:
    from ..dashboard import serve_dashboard

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
    from ..dashboard import serve_hub
    from ..registry import load_registry, registry_path

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
    from ..hub import build_hub_snapshot
    from ..registry import load_registry

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
    from ..hub_daemon import hub_daemon_loop
    from ..notify import configured_notifier, notification_transition

    if args.concurrency < 1:
        raise QueueError("hub daemon --concurrency must be at least 1")
    say = (lambda message: None) if args.json and args.once else print
    warned_without_webhook: set[str] = set()

    def resolve_notifier(path: str, key: str):
        try:
            config = load_config(repo=path)
        except Exception:
            # A broken config cannot provide a trusted webhook. The open Hub
            # dashboard surfaces the repository error through browser alerts.
            return None
        if notification_transition(key) not in config.notify.transitions:
            return None
        if not config.notify.webhook_url:
            warning_key = str(config.repo)
            if warning_key not in warned_without_webhook:
                print(
                    f"mergetrain warning: --notify has no configured webhook for "
                    f"{config.project.name}; no headless notification was sent",
                    file=sys.stderr,
                )
                warned_without_webhook.add(warning_key)
            return None
        return configured_notifier(config.notify)

    outcomes = hub_daemon_loop(
        registry=args.registry,
        interval_seconds=args.interval,
        concurrency=args.concurrency,
        keep_worktree=args.keep_worktree,
        once=args.once,
        say=say,
        notifier_resolver=resolve_notifier if args.notify else None,
    )
    if args.json and args.once:
        dump_json({"ok": True, "outcomes": outcomes})
    return 0


def cmd_hub_add(args: argparse.Namespace) -> int:
    from ..registry import add_repo, registry_path

    entry = add_repo(args.path, args.registry, daemon=args.daemon)
    if args.json:
        dump_json({"ok": True, "registry": str(args.registry or registry_path()), "entry": entry})
    else:
        suffix = "" if entry.get("daemon", True) else " (hub daemon: excluded)"
        print(f"registered: {entry['path']}{suffix}")
    return 0


def cmd_hub_remove(args: argparse.Namespace) -> int:
    from ..registry import registry_path, remove_repo

    removed = remove_repo(args.path, args.registry)
    if args.json:
        # ok = the removal attempt ran; `removed` carries found-or-not.
        dump_json(
            {
                "ok": True,
                "registry": str(args.registry or registry_path()),
                "removed": removed,
            }
        )
    else:
        print("deregistered" if removed else "not registered; nothing removed")
    return 0 if removed else 1


def cmd_hub_list(args: argparse.Namespace) -> int:
    from ..registry import load_registry, registry_path

    print(
        "mergetrain warning: `hub list` is deprecated; use `hub status` "
        "(planned removal in 2.0)",
        file=sys.stderr,
    )
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
