#!/usr/bin/env python3
"""Generic compatibility wrapper for service repositories.

Copy this file into a service repository when you want a stable local command
shape while delegating all queue behavior to the installed `trainyard` CLI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode == 0:
        return Path(completed.stdout.strip())
    return Path.cwd()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = repo_root()
    config = os.environ.get("TRAINYARD_CONFIG", str(root / ".trainyard.yaml"))
    db = os.environ.get("TRAINYARD_DB")
    command = ["trainyard", "--repo", str(root), "--config", config]
    if db:
        command.extend(["--db", db])
    command.extend(args)
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
