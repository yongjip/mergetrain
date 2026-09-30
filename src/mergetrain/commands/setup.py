"""Configuration, generated instructions, demo, and MCP commands."""

from __future__ import annotations

import argparse
import os
import uuid
from pathlib import Path

from ..cli_support import dump_json
from ..config import config_file_path, render_default_config, shared_state_root
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


def _write_generated(path: Path, content: str, *, replace: bool) -> None:
    """Write ``path`` itself, never a file a symbolic link there points to.

    A cloned repository can commit a link where a generated file belongs; a
    write through it would overwrite, or with a dangling link create, a file
    outside the repository (#231).
    """

    if path.is_symlink():
        raise ConfigError(f"refusing to write through a symbolic link: {path}")
    exclusive = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    target = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp") if replace else path
    with os.fdopen(os.open(target, exclusive, 0o666), "w", encoding="utf-8") as handle:
        handle.write(content)
    if replace:
        try:
            # rename replaces the directory entry, even a link that appeared
            # since the check above, and never follows it.
            os.replace(target, path)
        except BaseException:
            target.unlink(missing_ok=True)
            raise


def cmd_init(args: argparse.Namespace) -> int:
    # Write where load_config reads: in a linked task worktree, the control
    # checkout that owns the shared queue.
    repo = shared_state_root(Path(args.repo or Path.cwd()))
    project = args.project or repo.name or "example-app"
    config_text = render_default_config(project)
    if args.refresh_instructions:
        files = {
            repo / "AGENTS.mergetrain.md": render_agent_contract(),
            repo / "CLAUDE.mergetrain.md": render_agent_contract(),
        }
        linked = [str(path) for path in files if path.is_symlink()]
        if linked:
            raise ConfigError(
                "refusing to write through symbolic links: " + ", ".join(linked)
            )
        refreshed: list[str] = []
        for path, content in files.items():
            _write_generated(path, content, replace=True)
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
        # The file every later command reads, including one --config names.
        config_file_path(args.config, args.repo or Path.cwd()): config_text,
        repo / "AGENTS.mergetrain.md": render_agent_contract(),
        repo / "CLAUDE.mergetrain.md": render_agent_contract(),
    }
    # is_symlink catches a dangling link, which exists() reports as absent.
    conflicts = [path for path in files if path.exists() or path.is_symlink()]
    if conflicts:
        rendered = ", ".join(str(path) for path in conflicts)
        raise ConfigError(
            "refusing to overwrite existing files: " + rendered + "; use "
            "init --refresh-instructions to refresh only generated agent docs"
        )
    written: list[str] = []
    for path, content in files.items():
        _write_generated(path, content, replace=False)
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

    # Every tool runs a CLI child, so the server hands its global options to
    # each one; a --config or --db given here must select the same queue.
    return run_server(Path(args.repo), config=args.config, db=args.db)
