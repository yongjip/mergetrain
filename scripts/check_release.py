#!/usr/bin/env python3
"""Fail closed when release metadata does not describe one exact version."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
SECURITY_SUPPORT_POLICY = (
    "Only the latest release published to PyPI receives security fixes."
)


def _security_policy_errors(text: str) -> list[str]:
    errors: list[str] = []
    if re.search(r"\bpre-1\.0\b", text, flags=re.IGNORECASE):
        errors.append("SECURITY.md still describes mergetrain as pre-1.0")
    if SECURITY_SUPPORT_POLICY not in text:
        errors.append(
            "SECURITY.md needs the release-independent supported-versions policy: "
            f"{SECURITY_SUPPORT_POLICY}"
        )
    return errors


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

    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    errors.extend(_security_policy_errors(security))

    if tag and tag != f"v{project_version}":
        errors.append(
            f"release tag mismatch: expected v{project_version}, received {tag}"
        )

    server_path = ROOT / "server.json"
    try:
        server = json.loads(server_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"server.json is missing or invalid JSON: {exc}")
        return errors

    server_name = str(server.get("name", ""))
    if server.get("version") != project_version:
        errors.append(
            f"server.json version mismatch: expected {project_version}, "
            f"received {server.get('version')}"
        )
    if f"mcp-name: {server_name}" not in (
        ROOT / "README.md"
    ).read_text(encoding="utf-8"):
        errors.append(
            "README.md MCP Registry ownership marker does not match "
            f"server.json name {server_name!r}"
        )

    packages = server.get("packages")
    expected_package = {
        "registryType": "pypi",
        "identifier": "mergetrain",
        "version": project_version,
        "packageArguments": [{"type": "positional", "value": "mcp"}],
        "transport": {"type": "stdio"},
    }
    if not isinstance(packages, list) or expected_package not in packages:
        errors.append(
            "server.json needs the exact PyPI mergetrain package, release "
            "version, stdio transport, and fixed 'mcp' package argument"
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
