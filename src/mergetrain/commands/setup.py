"""Configuration, contract, version, demo, and MCP commands."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .. import __version__
from ..cli_support import config_from_args, dump_json
from ..config import TerminologyConfig, render_default_config
from ..errors import ConfigError


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
            "Fix blocked or failed work in the owning branch and commit a clean result, then run mergetrain retry <id> to dismiss the old outcome and enqueue a fresh SHA-pinned job.",
            "Replace a validated train only with mergetrain supersede; the replacement is a new SHA-pinned train that requires fresh validation and deploy approval.",
            "After a crash, run reconcile/recover to resolve needs_reconcile jobs against the remote before deploying; run reconcile before any manual force-push.",
            "Do not delete or rewrite refs/mergetrain/deploys/*; they are permanent remote recovery evidence.",
        ],
        "boundary": {
            "deploy_requires": "run-batch --deploy for a validated train; run-next --deploy only when none is pending",
            "validate_requires": "run-next --validate-only or run-batch --validate-only",
            "validated_train_deploy": "run-batch --deploy claims one exact validated train",
            "validated_gate_reuse": "disabled by default; requires deploy.reuse.enabled or --reuse-validated",
            "validated_train_supersede": "supersede atomically retires one validated train and enqueues exact replacement SHAs without inheriting validation, reuse identity, or deploy approval",
            "progress_observation": "events, inspect, and logs are read-only; events JSONL resumes by persisted event ID",
            "daemon_processes_only": "jobs enqueued with --auto",
            "hub_observation": "hub serves a read-only aggregate; every repo keeps its own queue, lock, and recovery state",
            "hub_daemon_processes_only": "jobs enqueued with --auto, across registered repos, through each repo's own runner and lock; concurrency caps simultaneous repos machine-wide",
            "destructive_cleanup_requires": "gc --apply; branch deletion also requires --delete-branches",
            "recovery_after_crash": "reconcile / recover / unlock resolve crash state against the remote; run-batch --deploy is refused while any job is needs_reconcile",
            "machine_contract": "every --json payload and the stream_start JSONL frame carry contract_version; branch outcome on result, health on health, failures on error.code; ignore unknown keys and dispatch JSONL on type (see docs/contract.md)",
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
- `supersede` atomically retires a validated train and enqueues exact replacement SHAs; validation, reuse identity, and deploy approval never carry over.
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
        "commit these files (git add . && git commit); mergetrain's own "
        ".mergetrain/ state directory is git-ignored automatically"
    )
    dump_json({"ok": True, "written": written, "next_step": next_step})
    return 0


def cmd_agent_contract(args: argparse.Namespace) -> int:
    terminology = config_from_args(args).terminology
    if args.json:
        dump_json({"ok": True, **agent_contract_payload(terminology)})
    else:
        print(render_agent_contract(terminology), end="")
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
