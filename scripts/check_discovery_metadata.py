#!/usr/bin/env python3
"""Fail when problem-first discovery metadata drifts across catalogs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PATH = Path("discovery/metadata.yaml")


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML object")
    return data


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def _frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing YAML frontmatter")
    try:
        raw = text.split("---\n", 2)[1]
    except IndexError as exc:
        raise ValueError(f"{path}: unterminated YAML frontmatter") from exc
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a frontmatter object")
    return data


def _first_paragraph(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    try:
        body = text.split("\n\n", 1)[1]
    except IndexError as exc:
        raise ValueError(f"{path}: missing introductory paragraph") from exc
    return body.split("\n\n", 1)[0].replace("\n", " ").strip()


def validate_canonical(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("discovery metadata schema_version must be 1")

    required_strings = (
        "headline",
        "short_description",
        "mcp_description",
        "qualified_description",
        "skill_description",
        "deploy_skill_description",
    )
    for key in required_strings:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"discovery metadata {key} must be a non-empty string")

    mcp_description = data.get("mcp_description")
    if isinstance(mcp_description, str) and len(mcp_description) > 100:
        errors.append("discovery metadata mcp_description must contain at most 100 characters")

    for key in ("keywords", "catalog_tags"):
        values = data.get(key)
        if not isinstance(values, list) or not values or not all(
            isinstance(value, str) and value for value in values
        ):
            errors.append(f"discovery metadata {key} must be a non-empty string list")
        elif len(values) != len(set(values)):
            errors.append(f"discovery metadata {key} must not contain duplicates")

    codex_prompts = data.get("codex_default_prompts")
    if not isinstance(codex_prompts, list) or not codex_prompts or not all(
        isinstance(value, str) and value for value in codex_prompts
    ):
        errors.append("discovery metadata codex_default_prompts must be a string list")
    elif len(codex_prompts) > 3:
        errors.append("discovery metadata codex_default_prompts must contain at most 3 prompts")
    elif any(len(value) > 128 for value in codex_prompts):
        errors.append("discovery metadata codex_default_prompts entries must be at most 128 characters")

    github_about = data.get("github_about")
    if not isinstance(github_about, dict):
        errors.append("discovery metadata github_about must be an object")
    else:
        description = github_about.get("description")
        topics = github_about.get("topics")
        if not isinstance(description, str) or not description:
            errors.append("github_about.description must be a non-empty string")
        if not isinstance(topics, list) or not topics or not all(
            isinstance(topic, str) and topic for topic in topics
        ):
            errors.append("github_about.topics must be a non-empty string list")
        elif len(topics) != len(set(topics)):
            errors.append("github_about.topics must not contain duplicates")
        elif len(topics) > 20:
            errors.append("github_about.topics must contain at most 20 topics")

    combined = " ".join(
        str(data.get(key, ""))
        for key in ("short_description", "qualified_description", "skill_description")
    ).lower()
    for phrase in (
        "parallel coding",
        "worktree",
        "committed branches",
        "combined",
        "push",
        "interrupted",
    ):
        if phrase not in combined:
            errors.append(f"discovery metadata is missing problem trigger {phrase!r}")
    for phrase in ("single-agent", "github merge queue", "gitlab merge trains"):
        if phrase not in str(data.get("skill_description", "")).lower():
            errors.append(f"skill_description is missing negative trigger {phrase!r}")
    if "gemini" in combined:
        errors.append("canonical discovery copy must use the current agy path, not Gemini")
    return errors


def _expect(errors: list[str], path: str, field: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        errors.append(f"{path}: {field} must match {CANONICAL_PATH}")


def check_discovery_metadata(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    try:
        canonical = _load_yaml(root / CANONICAL_PATH)
        errors.extend(validate_canonical(canonical))

        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
        _expect(
            errors,
            "pyproject.toml",
            "project.description",
            project.get("description"),
            canonical["short_description"],
        )
        _expect(
            errors,
            "pyproject.toml",
            "project.keywords",
            project.get("keywords"),
            canonical["keywords"],
        )

        server = _load_json(root / "server.json")
        _expect(
            errors,
            "server.json",
            "description",
            server.get("description"),
            canonical["mcp_description"],
        )

        marketplace = _load_json(root / ".claude-plugin/marketplace.json")
        _expect(
            errors,
            ".claude-plugin/marketplace.json",
            "metadata.description",
            marketplace.get("metadata", {}).get("description"),
            canonical["qualified_description"],
        )
        plugins = marketplace.get("plugins", [])
        if len(plugins) != 1 or not isinstance(plugins[0], dict):
            errors.append(".claude-plugin/marketplace.json: expected one plugin object")
        else:
            _expect(
                errors,
                ".claude-plugin/marketplace.json",
                "plugins[0].description",
                plugins[0].get("description"),
                canonical["short_description"],
            )
            _expect(
                errors,
                ".claude-plugin/marketplace.json",
                "plugins[0].tags",
                plugins[0].get("tags"),
                canonical["catalog_tags"],
            )

        manifest = _load_json(
            root / "integrations/claude/plugin/.claude-plugin/plugin.json"
        )
        _expect(
            errors,
            "integrations/claude/plugin/.claude-plugin/plugin.json",
            "description",
            manifest.get("description"),
            canonical["short_description"],
        )
        _expect(
            errors,
            "integrations/claude/plugin/.claude-plugin/plugin.json",
            "keywords",
            manifest.get("keywords"),
            canonical["catalog_tags"],
        )

        agy_manifest = _load_json(root / "plugin.json")
        _expect(
            errors,
            "plugin.json",
            "description",
            agy_manifest.get("description"),
            canonical["short_description"],
        )

        codex_marketplace = _load_json(root / ".agents/plugins/marketplace.json")
        codex_plugins = codex_marketplace.get("plugins", [])
        if codex_marketplace.get("name") != "mergetrain":
            errors.append(".agents/plugins/marketplace.json: name must be mergetrain")
        if len(codex_plugins) != 1 or not isinstance(codex_plugins[0], dict):
            errors.append(".agents/plugins/marketplace.json: expected one plugin object")
        else:
            if codex_plugins[0].get("name") != "mergetrain":
                errors.append(".agents/plugins/marketplace.json: plugin name must be mergetrain")

        codex_root = root / "plugins/mergetrain"
        codex_manifest = _load_json(codex_root / ".codex-plugin/plugin.json")
        _expect(
            errors,
            "plugins/mergetrain/.codex-plugin/plugin.json",
            "description",
            codex_manifest.get("description"),
            canonical["short_description"],
        )
        _expect(
            errors,
            "plugins/mergetrain/.codex-plugin/plugin.json",
            "keywords",
            codex_manifest.get("keywords"),
            canonical["catalog_tags"],
        )
        codex_interface = codex_manifest.get("interface", {})
        _expect(
            errors,
            "plugins/mergetrain/.codex-plugin/plugin.json",
            "interface.shortDescription",
            codex_interface.get("shortDescription"),
            canonical["headline"],
        )
        _expect(
            errors,
            "plugins/mergetrain/.codex-plugin/plugin.json",
            "interface.longDescription",
            codex_interface.get("longDescription"),
            canonical["qualified_description"],
        )
        _expect(
            errors,
            "plugins/mergetrain/.codex-plugin/plugin.json",
            "interface.defaultPrompt",
            codex_interface.get("defaultPrompt"),
            canonical["codex_default_prompts"],
        )
        codex_mcp = _load_json(codex_root / ".mcp.json")
        _expect(
            errors,
            "plugins/mergetrain/.mcp.json",
            "mcpServers.mergetrain.description",
            codex_mcp.get("mcpServers", {}).get("mergetrain", {}).get("description"),
            canonical["mcp_description"],
        )

        skill = _frontmatter(
            root / "integrations/claude/plugin/skills/mergetrain/SKILL.md"
        )
        _expect(
            errors,
            "integrations/claude/plugin/skills/mergetrain/SKILL.md",
            "description",
            skill.get("description"),
            canonical["skill_description"],
        )

        agy_skill = _frontmatter(root / "skills/mergetrain/SKILL.md")
        _expect(
            errors,
            "skills/mergetrain/SKILL.md",
            "description",
            agy_skill.get("description"),
            canonical["skill_description"],
        )

        codex_skill = _frontmatter(
            root / "plugins/mergetrain/skills/mergetrain/SKILL.md"
        )
        _expect(
            errors,
            "plugins/mergetrain/skills/mergetrain/SKILL.md",
            "description",
            codex_skill.get("description"),
            canonical["skill_description"],
        )

        deploy_skill = _frontmatter(
            root / "integrations/claude/plugin/skills/deploy/SKILL.md"
        )
        _expect(
            errors,
            "integrations/claude/plugin/skills/deploy/SKILL.md",
            "description",
            deploy_skill.get("description"),
            canonical["deploy_skill_description"],
        )
        if deploy_skill.get("disable-model-invocation") is not True:
            errors.append(
                "integrations/claude/plugin/skills/deploy/SKILL.md: "
                "disable-model-invocation must remain true"
            )

        readme = (root / "README.md").read_text(encoding="utf-8")
        if f"**{canonical['headline']}**" not in readme:
            errors.append("README.md: subtitle must match discovery/metadata.yaml")
        _expect(
            errors,
            "llms.txt",
            "introductory paragraph",
            _first_paragraph(root / "llms.txt"),
            canonical["short_description"],
        )
        _expect(
            errors,
            "llms-full.txt",
            "introductory paragraph",
            _first_paragraph(root / "llms-full.txt"),
            canonical["qualified_description"],
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(str(exc))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--github-json",
        action="store_true",
        help="print the canonical GitHub About description and topics",
    )
    args = parser.parse_args(argv)

    errors = check_discovery_metadata()
    if errors:
        for error in errors:
            print(f"discovery metadata check failed: {error}", file=sys.stderr)
        return 1

    if args.github_json:
        canonical = _load_yaml(ROOT / CANONICAL_PATH)
        print(json.dumps(canonical["github_about"], indent=2, sort_keys=True))
    else:
        print("discovery metadata surfaces are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
