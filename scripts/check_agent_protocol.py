#!/usr/bin/env python3
"""Fail when committed agent protocol surfaces drift from the CLI source."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mergetrain.cli import build_parser, render_agent_contract  # noqa: E402
from mergetrain.snapshot import NEXT_ACTION_VALUES  # noqa: E402

START = "<!-- BEGIN GENERATED: mergetrain-agent-protocol -->"
END = "<!-- END GENERATED: mergetrain-agent-protocol -->"
SURFACES = (
    ROOT / "CLAUDE.md",
    ROOT / "llms.txt",
    ROOT / "llms-full.txt",
    ROOT / "integrations/claude/plugin/skills/mergetrain/SKILL.md",
    ROOT / "skills/mergetrain/SKILL.md",
    ROOT / "plugins/mergetrain/skills/mergetrain/SKILL.md",
)
CORE_COMMANDS = ("init", "status", "enqueue", "validate", "deploy", "inspect")
GRAMMAR_SURFACES = (
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "README.md",
    ROOT / "llms.txt",
    ROOT / "llms-full.txt",
    ROOT / "docs/agent-contract.md",
    ROOT / "docs/quickstart.md",
    ROOT / "integrations/claude/plugin/skills/deploy/SKILL.md",
    ROOT / "integrations/claude/plugin/skills/mergetrain/SKILL.md",
    ROOT / "skills/mergetrain/SKILL.md",
    ROOT / "plugins/mergetrain/skills/mergetrain/SKILL.md",
    ROOT / "scripts/mt-status.sh",
    ROOT / "scripts/mt-validate.sh",
    ROOT / "scripts/mt-deploy.sh",
)
REMOVED_GRAMMAR = (
    re.compile(r"\bmergetrain\s+(?:doctor|run-batch|run-next|recover|agent-contract)\b"),
    re.compile(
        r"\bmergetrain_(?:doctor|history|stats|agent_contract|gc_preview|events|logs)\b"
    ),
)


def canonical_protocol() -> str:
    """Render the shared body with headings demoted for embedding."""

    _title, separator, body = render_agent_contract().partition("\n\n")
    if not separator:
        raise RuntimeError("render_agent_contract did not return a title and body")
    return body.rstrip().replace("\n## ", "\n### ")


def generated_block() -> str:
    return f"{START}\n{canonical_protocol()}\n{END}"


def replace_generated_block(text: str, *, path: Path) -> str:
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    if len(pattern.findall(text)) != 1:
        raise RuntimeError(f"{path}: expected exactly one generated protocol block")
    return pattern.sub(generated_block(), text)


def table_values(text: str, heading: str) -> set[str]:
    try:
        section = text.split(heading, 1)[1].split("\n\n## ", 1)[0]
    except IndexError as exc:
        raise RuntimeError(f"missing table section {heading}") from exc
    lines = section.splitlines()
    divider = next(
        (index for index, line in enumerate(lines) if line.startswith("| ---")),
        None,
    )
    if divider is None:
        raise RuntimeError(f"missing table divider in {heading}")
    rows = "\n".join(lines[divider + 1 :])
    return set(re.findall(r"^\| `([a-z_]+)` \|", rows, flags=re.MULTILINE))


def implemented_mcp_errors() -> set[str]:
    source = (ROOT / "src/mergetrain/mcp_server.py").read_text(encoding="utf-8")
    return set(re.findall(r'_error\(\s*"([a-z_]+)"', source))


def check_reference_tables() -> list[str]:
    path = ROOT / "integrations/claude/plugin/skills/mergetrain/reference.md"
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    documented_actions = table_values(text, "## `next_action` guide")
    if documented_actions != set(NEXT_ACTION_VALUES):
        errors.append(
            f"{path}: next_action table mismatch "
            f"(missing={sorted(set(NEXT_ACTION_VALUES) - documented_actions)}, "
            f"extra={sorted(documented_actions - set(NEXT_ACTION_VALUES))})"
        )
    documented_mcp_errors = table_values(text, "## MCP refusal and adapter errors")
    expected_mcp_errors = implemented_mcp_errors()
    if documented_mcp_errors != expected_mcp_errors:
        errors.append(
            f"{path}: MCP error table mismatch "
            f"(missing={sorted(expected_mcp_errors - documented_mcp_errors)}, "
            f"extra={sorted(documented_mcp_errors - expected_mcp_errors)})"
        )
    return errors


def check_product_grammar() -> list[str]:
    errors: list[str] = []
    help_text = build_parser().format_help()
    section = help_text.split("core commands:\n", 1)[-1]
    visible = re.findall(r"^    ([a-z][a-z-]+)\s{2,}", section, flags=re.MULTILINE)
    if tuple(visible) != CORE_COMMANDS:
        errors.append(
            "default CLI help must expose exactly the six core commands in "
            f"lifecycle order; received {visible}"
        )

    for path in GRAMMAR_SURFACES:
        text = path.read_text(encoding="utf-8")
        for pattern in REMOVED_GRAMMAR:
            match = pattern.search(text)
            if match:
                errors.append(f"{path}: removed grammar remains: {match.group(0)}")

    deploy_skill = (
        ROOT / "integrations/claude/plugin/skills/deploy/SKILL.md"
    ).read_text(encoding="utf-8")
    for forbidden in ("$ARGUMENTS", "train_id"):
        if forbidden in deploy_skill:
            errors.append(
                "the Claude deploy skill must not accept a model/user train "
                f"selector: found {forbidden}"
            )
    for required in ("mergetrain_status", "mergetrain_deploy"):
        if required not in deploy_skill:
            errors.append(f"the Claude deploy skill is missing {required}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite generated protocol blocks from the CLI source",
    )
    args = parser.parse_args(argv)

    errors: list[str] = []
    expected = generated_block()
    for path in SURFACES:
        current = path.read_text(encoding="utf-8")
        updated = replace_generated_block(current, path=path)
        if args.write and current != updated:
            path.write_text(updated, encoding="utf-8")
            current = updated
        if expected not in current:
            errors.append(f"{path}: generated agent protocol is stale")

    errors.extend(check_reference_tables())
    errors.extend(check_product_grammar())
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(
            "Run `python scripts/check_agent_protocol.py --write`, "
            "then update the reference tables if required.",
            file=sys.stderr,
        )
        return 1
    print("agent protocol surfaces are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
