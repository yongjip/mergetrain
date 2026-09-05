"""Configuration, generated instructions, demo, and MCP commands."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..cli_support import dump_json
from ..config import render_default_config
from ..errors import ConfigError

_AGENT_RULES = (
    "Work on a task-specific branch and worktree.",
    "Commit a clean HEAD before handing work off.",
    "Read mergetrain status --json and follow its next action before changing queue state.",
    "Enqueue every named finished branch in the requested order using only its task and branch; mergetrain resolves the worktree and captures the exact commits. Stop after the last successful enqueue unless the user explicitly authorized validation or the complete validation-and-deployment workflow.",
    "Never push configured integration refs directly. One authorized runner owns validation and deployment; recovery and destructive actions require their stated approval.",
)


def render_agent_contract() -> str:
    rules = "\n".join(f"{i}. {rule}" for i, rule in enumerate(_AGENT_RULES, start=1))
    return f"""# mergetrain agent contract

Purpose: Serialize committed local task branches through one merge/test/push/verify runner.

## Existing queues and explanations

- Existing mergetrain repositories keep status → enqueue → stop even for one branch. Queue counts alone do not establish health, runner ownership, or recovery needs; read `health`, `state`, and `next_action` together.
- For explanation-only requests, read the skill documentation when permitted and explain the procedure without Git or product commands. Distinguish hypothetical steps from observed state.

## Current command reference

- The v3 core commands are `init`, `status`, `enqueue`, `validate`, `deploy`, and `inspect`.
- Start with `mergetrain status --json`. Use `mergetrain status --diagnose --json` only for configuration, Git, runtime, or lock detail, and `mergetrain inspect JOB_ID --json` for job evidence. `doctor` is removed, not an alias for `status --diagnose`.
- Confirm uncertain syntax with the installed `mergetrain --version` and command-specific `--help` only when command execution is permitted. Otherwise use this reference and identify missing details; do not invent commands or copy older syntax from unversioned web results. Inspection and `next_action` do not authorize recovery or deployment.

## Rules

{rules}

## Safety boundary

- A task agent enqueues every named finished branch, then stops. "Queue for validation" authorizes enqueue only; only an explicit request to run validation or the complete end-to-end workflow authorizes `validate`.
- Only a separately authorized runner uses `deploy` or a daemon.
- Deployment requires either confirmation of the human-readable exact plan or prior bounded unattended approval. Agents never select train IDs or supply plan hashes; structured evidence may include identifiers for inspection.
- Unattended approval is bound to the exact destination and execution policy. Any change blocks before push.
- Recovery and destructive cleanup require their stated approval. Follow `status.next_action`; never rewrite permanent deploy audit refs.

## Stable machine contract

- Every JSON payload carries `contract_version`; ignore unknown keys and fail closed on unknown safety actions.
- `deploy` means the atomic Git ref update plus configured verification. A downstream provider release is separate.
"""


def cmd_init(args: argparse.Namespace) -> int:
    repo = Path(args.repo or Path.cwd()).expanduser().resolve()
    project = args.project or repo.name or "example-app"
    config_text = render_default_config(project)
    if args.refresh_instructions:
        files = {
            repo / "AGENTS.mergetrain.md": render_agent_contract(),
            repo / "CLAUDE.mergetrain.md": render_agent_contract(),
        }
        refreshed: list[str] = []
        for path, content in files.items():
            path.write_text(content, encoding="utf-8")
            refreshed.append(str(path))
        dump_json(
            {
                "ok": True,
                "written": refreshed,
                "next_step": "review and commit the refreshed agent instructions",
            }
        )
        return 0
    if not args.write:
        print(config_text, end="")
        return 0
    files = {
        repo / ".mergetrain.yaml": config_text,
        repo / "AGENTS.mergetrain.md": render_agent_contract(),
        repo / "CLAUDE.mergetrain.md": render_agent_contract(),
    }
    conflicts = [path for path in files if path.exists()]
    if conflicts:
        rendered = ", ".join(str(path) for path in conflicts)
        raise ConfigError(
            "refusing to overwrite existing files: " + rendered + "; use "
            "init --refresh-instructions to refresh only generated agent docs"
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
