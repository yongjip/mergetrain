"""Run Codex through the adoption harness for one safe-handoff trial.

This adapter keeps the discovery runner's immutable prompt envelope while the
adoption harness owns Git, queue, and remote tracing. It is benchmark-only.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmarks.agent_adoption.harness import run_agent  # noqa: E402

CODEX_LAUNCHER = ROOT / "benchmarks" / "agent_adoption" / "codex_launcher.py"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", type=Path)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("fixture_root", type=Path)
    parser.add_argument("--codex", default="/opt/homebrew/bin/codex")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning", default="high")
    parser.add_argument("--agent-version", default="0.150.1")
    parser.add_argument("--timeout", type=float, default=900.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    fixture_root = args.fixture_root.resolve()
    control = fixture_root / "control"
    command = [
        sys.executable,
        str(CODEX_LAUNCHER),
        str(args.prompt.resolve()),
        str(control),
        "--codex",
        args.codex,
        "--model",
        args.model,
        "--reasoning",
        args.reasoning,
        "--working-directory",
        str(args.workspace.resolve()),
        "--writable-directory",
        str((fixture_root / "artifacts").resolve()),
    ]
    exit_code = run_agent(
        fixture_root,
        command,
        timeout_seconds=args.timeout,
        agent_product="codex",
        agent_version=args.agent_version,
        model=args.model,
        reasoning_setting=args.reasoning,
        permission_profile=(
            "approve-for-me(workspace-write); add-dir=control+artifacts; "
            "shell-env=core+explicit-trace+controlled-zprofile; "
            "shell-network=disabled; ignore-user-config; ephemeral"
        ),
    )

    artifacts = fixture_root / "artifacts"
    stdout = artifacts / "agent.stdout"
    stderr = artifacts / "agent.stderr"
    if stdout.exists():
        sys.stdout.write(stdout.read_text(encoding="utf-8"))
    if stderr.exists():
        sys.stderr.write(stderr.read_text(encoding="utf-8"))

    discovery_trace = os.environ.get("DISCOVERY_BENCHMARK_TRACE")
    adoption_trace = artifacts / "trace.jsonl"
    if discovery_trace and adoption_trace.exists():
        Path(discovery_trace).write_text(
            adoption_trace.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
