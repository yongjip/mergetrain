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
_FULL_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


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


def _readme_status_errors(text: str) -> list[str]:
    """Reject release numbers that inevitably drift in the README status copy."""

    status_section = re.search(
        r"^## Status\s*$([\s\S]*?)(?=^## |\Z)",
        text,
        flags=re.MULTILINE,
    )
    if status_section and re.search(
        r"\bv?\d+\.\d+\.\d+\b", status_section.group(1)
    ):
        return [
            "README.md must not hard-code the latest package version; use the PyPI badge"
        ]
    return []


def _workflow_pin_errors(workflows_dir: Path | None = None) -> list[str]:
    """Require immutable commit pins for every external workflow action."""

    directory = workflows_dir or ROOT / ".github" / "workflows"
    errors: list[str] = []
    for path in sorted((*directory.glob("*.yml"), *directory.glob("*.yaml"))):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = re.match(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", line)
            if match is None:
                continue
            target = match.group(1)
            if target.startswith("./"):
                continue
            _separator, marker, revision = target.rpartition("@")
            if not marker or not _FULL_COMMIT_SHA.fullmatch(revision):
                errors.append(
                    f"{path.relative_to(directory.parent.parent)}:{lineno} "
                    f"must pin external action {target!r} to a full commit SHA"
                )
    return errors


def _release_workflow_errors(text: str | None = None) -> list[str]:
    """Keep package publication rooted in protected main, not release tags."""

    workflow = text
    if workflow is None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
    errors: list[str] = []
    if "types: [published]" in workflow or re.search(
        r"(?m)^\s*release:\s*$", workflow
    ):
        errors.append("release.yml must not publish from a release event at a tag ref")
    required = {
        "a manual main-rooted trigger": "workflow_dispatch:",
        "an exact main-ref guard": "github.ref == 'refs/heads/main'",
        "an explicit wrong-ref rejection": "github.ref != 'refs/heads/main'",
        "deny-by-default workflow permissions": "permissions: {}",
        "the protected-main tag verifier": "scripts/verify_release_source.py verify",
        "the protected-main signer policy": ".github/release-allowed-signers",
        "a published immutable GitHub Release check": (
            ".draft == false and .immutable == true"
        ),
        "the verified source checkout": "ref: ${{ needs.verify.outputs.commit_sha }}",
        "the production publishing environment": "environment: pypi",
        "API-based release artifact download": (
            'gh run download "${GITHUB_RUN_ID}" --repo "${GITHUB_REPOSITORY}"'
        ),
    }
    for description, marker in required.items():
        if marker not in workflow:
            errors.append(f"release.yml needs {description}: {marker}")
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
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if f"mcp-name: {server_name}" not in readme:
        errors.append(
            "README.md MCP Registry ownership marker does not match "
            f"server.json name {server_name!r}"
        )

    errors.extend(_readme_status_errors(readme))
    errors.extend(_workflow_pin_errors())
    errors.extend(_release_workflow_errors())

    packages = server.get("packages")
    pypi_packages = [
        package
        for package in packages or []
        if isinstance(package, dict)
        and package.get("registryType") == "pypi"
        and package.get("identifier") == "mergetrain"
    ]
    if len(pypi_packages) != 1:
        errors.append(
            "server.json needs exactly one PyPI mergetrain package"
        )
    else:
        package = pypi_packages[0]
        expected_from = f"mergetrain[mcp]=={project_version}"
        if package.get("version") != project_version:
            errors.append(
                "server.json PyPI package version must match the release version"
            )
        if package.get("runtimeHint") != "uvx":
            errors.append("server.json PyPI package runtimeHint must be 'uvx'")
        expected_runtime_arguments = [{
            "type": "named",
            "name": "--from",
            "value": expected_from,
        }]
        if package.get("runtimeArguments") != expected_runtime_arguments:
            errors.append(
                "server.json must install the MCP extra with "
                f"uvx --from {expected_from}"
            )
        if package.get("packageArguments") != [
            {"type": "positional", "value": "mcp"}
        ]:
            errors.append("server.json must pass the fixed 'mcp' package argument")
        if package.get("transport") != {"type": "stdio"}:
            errors.append("server.json PyPI package transport must be stdio")

    agy_manifest_path = ROOT / "plugin.json"
    agy_mcp_path = ROOT / "mcp_config.json"
    try:
        agy_manifest = json.loads(agy_manifest_path.read_text(encoding="utf-8"))
        agy_mcp = json.loads(agy_mcp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"agy plugin metadata is missing or invalid JSON: {exc}")
        return errors

    if set(agy_manifest) != {"$schema", "name", "description"}:
        errors.append("plugin.json must contain only schema, name, and description")
    if agy_manifest.get("$schema") != "https://antigravity.google/schemas/v1/plugin.json":
        errors.append("plugin.json must use the official Antigravity v1 schema")
    if agy_manifest.get("name") != "mergetrain":
        errors.append("plugin.json name must be 'mergetrain'")

    agy_servers = agy_mcp.get("mcpServers") if isinstance(agy_mcp, dict) else None
    if not isinstance(agy_servers, dict) or set(agy_servers) != {"mergetrain"}:
        errors.append("mcp_config.json must contain exactly one mergetrain server")
    else:
        expected_agy_server = {
            "command": "uvx",
            "args": [
                "--from",
                f"mergetrain[mcp]=={project_version}",
                "mergetrain",
                "mcp",
            ],
        }
        if agy_servers["mergetrain"] != expected_agy_server:
            errors.append(
                "mcp_config.json must launch the exact release with "
                f"uvx --from mergetrain[mcp]=={project_version} mergetrain mcp"
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
