#!/usr/bin/env python3
"""Fail when committed agent protocol surfaces drift from the CLI source."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mergetrain.cli import render_agent_contract  # noqa: E402
from mergetrain.snapshot import NEXT_ACTION_VALUES  # noqa: E402

START = "<!-- BEGIN GENERATED: mergetrain-agent-protocol -->"
END = "<!-- END GENERATED: mergetrain-agent-protocol -->"
SURFACES = (
    ROOT / "CLAUDE.md",
    ROOT / "integrations/claude/plugin/skills/mergetrain/SKILL.md",
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
