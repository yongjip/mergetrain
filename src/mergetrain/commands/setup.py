"""Configuration, contract, version, demo, and MCP commands."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .. import __version__
from ..cli_support import dump_json
from ..config import render_default_config
from ..errors import ConfigError


def agent_contract_payload() -> dict[str, Any]:
    return {
        "name": "mergetrain agent contract",
        "purpose": "Serialize committed local task branches through one merge/test/push/verify runner.",
        "rules": [
            "Work on a task-specific branch and worktree.",
            "Commit all changes before enqueueing.",
            "Do not push configured Git refs directly. Task agents hand off by enqueueing the exact committed HEAD, then stop unless separately authorized as the runner.",
            "Read doctor --json first. Use status --json --limit 10 only when job or train details are needed, and read attention_jobs before recent history.",
            "Use --auto only after explicit unattended-deployment approval from the user/operator. A bounded instruction to QA, deploy, verify, and finish end-to-end is unattended-deployment approval for that task scope; continue without repeated train-ID prompts unless the scope, destination, execution policy, or recovery authority changes.",
            "An auto job is bound to the approved Git destination and execution policy. If the remote, push refs, gates, validation-reuse policy, command timeout, or verify hooks change, mergetrain blocks before claim, gates, or push; review the change before enqueueing with --auto again.",
            "Reuse validated gates only after explicit deploy.reuse configuration or --reuse-validated authorization.",
            "Let one separately authorized runner or daemon own merge, test, push, and verify; ordinary task, merge, integration, or enqueue intent is not deploy approval unless the user explicitly authorizes bounded end-to-end deployment.",
            "Fix blocked or failed work in the owning branch and commit a clean result, then run mergetrain retry <id> to dismiss the old outcome and enqueue a fresh SHA-pinned job.",
            "Replace a validated train only with mergetrain supersede; the replacement is a new SHA-pinned train that requires fresh validation. One-shot train approval does not carry over; bounded unattended-deployment approval carries only while task scope, destination, and execution policy remain unchanged.",
            "After a crash, run reconcile/recover to resolve needs_reconcile jobs against the remote before deploying; run reconcile before any manual force-push.",
            "Do not delete or rewrite refs/mergetrain/deploys/*; they are permanent remote recovery evidence.",
        ],
        "boundary": {
            "deploy_requires": "either explicit approval after a human-readable exact-train summary or prior explicit bounded unattended-deployment approval; an opaque train ID is binding evidence, not a user-facing explanation",
            "validate_requires": "run-next --validate-only, run-batch --validate-only, or a separately authorized daemon --validate-only",
            "validated_train_deploy": "run-batch --deploy claims one exact validated train; describe its changes, destination refs, gates, blocked or failed work, and reassembly risk, and never ask the user to copy an opaque train ID",
            "validated_gate_reuse": "disabled by default; requires deploy.reuse.enabled or --reuse-validated",
            "validated_train_supersede": "supersede atomically retires one validated train and enqueues exact replacement SHAs without inheriting validation, reuse identity, or one-shot train approval; bounded unattended authorization continues only for unchanged task scope, destination, and execution policy",
            "progress_observation": "events, inspect, and logs are read-only; events JSONL resumes by persisted event ID",
            "daemon_processes_only": "default mode deploys only jobs enqueued with --auto whose approved destination and execution policy still match; --validate-only processes only manual queued jobs and pauses while any validated train exists",
            "hub_observation": "hub serves a read-only aggregate; every repo keeps its own queue, lock, and recovery state",
            "hub_daemon_processes_only": "jobs enqueued with --auto, across registered repos, through each repo's own runner and lock; concurrency caps simultaneous repos machine-wide",
            "destructive_cleanup_requires": "gc --apply; branch deletion also requires --delete-branches",
            "recovery_after_crash": "reconcile / recover / unlock resolve crash state against the remote; run-batch --deploy is refused while any job is needs_reconcile",
            "machine_contract": "every --json payload and the stream_start JSONL frame carry contract_version; branch outcome on result, health on health, failures on error.code; ignore unknown keys and dispatch JSONL on type (see docs/contract.md)",
        },
    }


def render_agent_contract() -> str:
    payload = agent_contract_payload()
    rules = "\n".join(f"{i}. {rule}" for i, rule in enumerate(payload["rules"], start=1))
    return f"""# mergetrain agent contract

Purpose: {payload['purpose']}

## Rules

{rules}

## Safety boundary

- Git deployment requires either explicit approval after a human-readable exact-train summary or prior explicit bounded unattended-deployment approval. An opaque train ID binds the operation internally; never make the user repeat it.
- A task agent stops after enqueueing. Only a separately authorized runner validates with `run-next --validate-only`, `run-batch --validate-only`, or `daemon --validate-only`.
- A validated train is deployed as one exact identity by `run-batch --deploy`; summarize changes, destination refs, gates, blocked or failed work, and reassembly risk in human terms.
- Validated-gate reuse is disabled unless config or `--reuse-validated` explicitly authorizes it.
- `supersede` atomically retires a validated train and enqueues exact replacement SHAs; validation, reuse identity, and one-shot train approval never carry over. Bounded unattended authorization continues only while task scope, destination, and execution policy remain unchanged.
- `events`, `inspect`, and `logs` are read-only observation commands; event JSONL resumes by ID.
- The default daemon deploys only jobs enqueued with `--auto`. `daemon --validate-only` processes only manual queued jobs, never pushes, and pauses while any validated train exists.
- Auto approval is bound to the Git remote, integration ref, push refs, gates, command timeout, validation-reuse policy, and verify hooks recorded at enqueue. A destination or execution-policy change blocks before claim, gates, or push and requires renewed approval.
- The hub dashboard is a read-only aggregate; every repo keeps its own queue, lock, and recovery state.
- The hub daemon also processes only `--auto` jobs, across registered repos, through each repo's own runner and lock; `--concurrency` caps simultaneous repos machine-wide.
- Destructive cleanup requires `gc --apply`; branch deletion also requires `--delete-branches`.
- After a crash, `reconcile`/`recover` resolve `needs_reconcile` jobs against the remote; `run-batch --deploy` is refused while any job is `needs_reconcile`. `unlock --force` clears a wedged lock (remote-reachable first).

## Stable machine contract

- Human output and JSON/SQLite use `deploy`, `deploying`, `status=deployed`, `deploy_sha`, `push_status`, and `verify_status`.
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
    if not args.force:
        conflicts = [path for path in files if path.exists()]
        if conflicts:
            rendered = ", ".join(str(path) for path in conflicts)
            raise ConfigError(
                "refusing to overwrite existing file without --force: " + rendered
            )
    written: list[str] = []
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")
        written.append(str(path))
    # The scaffold is meant to be committed; the .mergetrain/ runtime dir
    # self-ignores. Say so, or the first enqueue trips the clean-worktree check.
    next_step = (
        "link the generated sidecars from the repository's standard AGENTS.md "
        "and/or CLAUDE.md, then commit these files (git add . && git commit); "
        "mergetrain's own .mergetrain/ state directory is git-ignored automatically"
    )
    dump_json({"ok": True, "written": written, "next_step": next_step})
    return 0


def cmd_agent_contract(args: argparse.Namespace) -> int:
    if args.json:
        dump_json({"ok": True, **agent_contract_payload()})
    else:
        print(render_agent_contract(), end="")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    from ..runtime import runtime_provenance

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


def cmd_demo(args: argparse.Namespace) -> int:
    # Keep the sizeable walkthrough implementation off ordinary CLI import
    # paths; `mergetrain --help` and agent commands avoid demo-only imports.
    from ..demo import run_demo

    return run_demo(
        directory=args.directory,
        keep=args.keep,
        pause=args.pause,
        brief=args.brief,
    )


def cmd_mcp(args: argparse.Namespace) -> int:
    # Imported here so the zero-dependency core keeps importing without the MCP
    # SDK; run_server prints the install hint when the extra is missing.
    from ..mcp_server import run_server

    return run_server(Path(args.repo))
