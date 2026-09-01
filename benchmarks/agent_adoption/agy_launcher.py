"""Launch Antigravity CLI inside the agent-adoption tracing boundary.

This is a benchmark adapter, not part of the installed mergetrain product.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
from collections.abc import Sequence
from pathlib import Path

DEFAULT_AGY = shutil.which("agy") or "agy"
DEFAULT_MODEL = "gemini-3.1-pro-high"
DEFAULT_EFFORT = "high"
DEFAULT_PRINT_TIMEOUT = "10m"
TRACE_ENVIRONMENT_NAMES = (
    "GIT_TERMINAL_PROMPT",
    "MERGETRAIN_BENCHMARK_REAL_GIT",
    "MERGETRAIN_BENCHMARK_REAL_MERGETRAIN",
    "MERGETRAIN_BENCHMARK_TRACE",
    "PATH",
)


def _configure_zsh_login_path(environment: dict[str, str]) -> None:
    """Keep the harness wrappers first if AGY starts a login zsh."""

    trace_path = Path(environment["MERGETRAIN_BENCHMARK_TRACE"]).resolve()
    run_root = trace_path.parent.parent
    wrapper_bin = Path(environment["PATH"].split(os.pathsep, 1)[0]).resolve()
    profile_dir = run_root / "agy-zdotdir"
    profile_dir.mkdir(exist_ok=True)
    profile = profile_dir / ".zprofile"
    profile.write_text(
        f"export PATH={shlex.quote(str(wrapper_bin))}:\"$PATH\"\n",
        encoding="utf-8",
    )
    environment["ZDOTDIR"] = str(profile_dir)


def _build_command(
    *,
    executable: str,
    prompt: str,
    task_repo: str,
    control_repo: str,
    model: str,
    effort: str,
    print_timeout: str,
) -> list[str]:
    return [
        executable,
        "--print",
        prompt,
        "--output-format",
        "stream-json",
        "--model",
        model,
        "--effort",
        effort,
        "--mode",
        "accept-edits",
        "--new-project",
        "--sandbox",
        "--disable-slash-commands",
        "--print-timeout",
        print_timeout,
        "--add-dir",
        task_repo,
        "--add-dir",
        control_repo,
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", type=Path)
    parser.add_argument("control_repo", type=Path)
    parser.add_argument("--agy", default=DEFAULT_AGY)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", choices=("low", "medium", "high"), default=DEFAULT_EFFORT)
    parser.add_argument("--print-timeout", default=DEFAULT_PRINT_TIMEOUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    prompt = args.prompt.read_text(encoding="utf-8")
    task_repo = Path.cwd().resolve()
    control_repo = args.control_repo.resolve()
    if not (task_repo / ".git").exists():
        raise SystemExit(f"missing task repository: {task_repo}")
    if not (control_repo / ".git").is_dir():
        raise SystemExit(f"missing control repository: {control_repo}")
    environment = os.environ.copy()
    missing = [name for name in TRACE_ENVIRONMENT_NAMES if not environment.get(name)]
    if missing:
        raise SystemExit(f"missing harness environment: {', '.join(missing)}")
    _configure_zsh_login_path(environment)

    command = _build_command(
        executable=args.agy,
        prompt=prompt,
        task_repo=str(task_repo),
        control_repo=str(control_repo),
        model=args.model,
        effort=args.effort,
        print_timeout=args.print_timeout,
    )
    os.execvpe(args.agy, command, environment)
    return 0  # pragma: no cover - exec only returns on failure


if __name__ == "__main__":
    raise SystemExit(main())
