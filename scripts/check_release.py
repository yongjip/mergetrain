#!/usr/bin/env python3
"""Fail closed when release metadata does not describe one exact version."""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _module_version() -> str:
    tree = ast.parse(
        (ROOT / "src/mergetrain/__init__.py").read_text(encoding="utf-8")
    )
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        defines_version = any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
        if defines_version:
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("src/mergetrain/__init__.py does not define a string __version__")


def check_release(*, tag: str = "") -> list[str]:
    errors: list[str] = []
    project_version = _project_version()
    module_version = _module_version()

    if module_version != project_version:
        errors.append(
            f"version mismatch: pyproject.toml={project_version}, "
            f"mergetrain.__version__={module_version}"
        )

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    release_heading = re.compile(
        rf"^## {re.escape(project_version)} - \d{{4}}-\d{{2}}-\d{{2}}$",
        re.MULTILINE,
    )
    if not release_heading.search(changelog):
        errors.append(
            f"CHANGELOG.md needs a dated '## {project_version} - YYYY-MM-DD' heading"
        )

    if tag and tag != f"v{project_version}":
        errors.append(
            f"release tag mismatch: expected v{project_version}, received {tag}"
        )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="", help="Release tag, for example v0.1.0")
    args = parser.parse_args()

    errors = check_release(tag=args.tag)
    if errors:
        for error in errors:
            print(f"release check failed: {error}", file=sys.stderr)
        return 1

    version = _project_version()
    suffix = f" for tag {args.tag}" if args.tag else ""
    print(f"release metadata OK: mergetrain {version}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
