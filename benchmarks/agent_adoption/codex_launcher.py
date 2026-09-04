"""Launch Codex inside the agent-adoption harness tracing boundary.

This is a benchmark adapter, not part of the installed mergetrain product.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
from collections.abc import Sequence
from pathlib import Path

DEFAULT_CODEX = "/opt/homebrew/bin/codex"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING = "max"
TRACE_ENVIRONMENT_NAMES = (
    "GIT_TERMINAL_PROMPT",
    "MERGETRAIN_BENCHMARK_REAL_GIT",
    "MERGETRAIN_BENCHMARK_REAL_MERGETRAIN",
    "MERGETRAIN_BENCHMARK_TRACE",
    "PATH",
)


def _configure_zsh_login_path(environment: dict[str, str]) -> None:
    """Keep the harness wrappers first after macOS `/etc/zprofile` runs."""

    trace_path = Path(environment["MERGETRAIN_BENCHMARK_TRACE"]).resolve()
    run_root = trace_path.parent.parent
    wrapper_bin = Path(environment["PATH"].split(os.pathsep, 1)[0]).resolve()
    profile_dir = run_root / "codex-zdotdir"
    profile_dir.mkdir(exist_ok=True)
    profile = profile_dir / ".zprofile"
    profile.write_text(
        f"export PATH={shlex.quote(str(wrapper_bin))}:\"$PATH\"\n",
        encoding="utf-8",
    )
    environment["ZDOTDIR"] = str(profile_dir)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", type=Path)
    parser.add_argument("control_repo", type=Path)
    parser.add_argument("--codex", default=DEFAULT_CODEX)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning", default=DEFAULT_REASONING)
    parser.add_argument(
        "--working-directory",
        type=Path,
        help="Start Codex from this directory instead of the harness task worktree",
    )
    parser.add_argument(
        "--writable-directory",
        action="append",
        default=[],
        type=Path,
        help="Grant a benchmark-owned output directory to the Codex sandbox",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    prompt = args.prompt.read_text(encoding="utf-8")
    control_repo = str(args.control_repo.resolve())
    environment = os.environ.copy()
    missing = [name for name in TRACE_ENVIRONMENT_NAMES if not environment.get(name)]
    if missing:
        raise SystemExit(f"missing harness environment: {', '.join(missing)}")
    _configure_zsh_login_path(environment)
    trace_environment = {
        name: environment[name] for name in (*TRACE_ENVIRONMENT_NAMES, "ZDOTDIR")
    }
    shell_environment = ", ".join(
        f"{name} = {json.dumps(value)}" for name, value in trace_environment.items()
    )
    command = [
        args.codex,
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "--model",
        args.model,
        "--config",
        f'model_reasoning_effort={json.dumps(args.reasoning)}',
        "--config",
        'shell_environment_policy.inherit="core"',
        "--config",
        f"shell_environment_policy.set={{ {shell_environment} }}",
        "--config",
        "sandbox_workspace_write.network_access=false",
        "--approve-for-me",
    ]
    if args.working_directory is not None:
        command += ["--cd", str(args.working_directory.resolve())]
    command += ["--add-dir", control_repo]
    for directory in args.writable_directory:
        command += ["--add-dir", str(directory.resolve())]
    command.append(prompt)
    os.execvpe(args.codex, command, environment)
    return 0  # pragma: no cover - exec only returns on failure


if __name__ == "__main__":
    raise SystemExit(main())
