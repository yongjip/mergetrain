from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from scripts.check_release import check_release

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "integrations/claude/plugin"


class ClaudePluginTests(unittest.TestCase):
    def test_manifest_and_mcp_runtime_match_the_release(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
        version = project["version"]
        manifest = json.loads(
            (PLUGIN_ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
        )
        mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["version"], version)
        self.assertEqual(
            mcp["mcpServers"]["mergetrain"],
            {
                "title": "mergetrain",
                "description": (
                    "Queue parallel-agent worktree branches, test together, "
                    "serialize and recover Git pushes."
                ),
                "command": "uvx",
                "args": [
                    "--from",
                    f"mergetrain[mcp]=={version}",
                    "mergetrain",
                    "mcp",
                ],
            },
        )

    def test_plugin_is_self_documenting_for_directory_review(self) -> None:
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("uvx mergetrain demo", readme)
        self.assertIn("## Example prompts", readme)
        self.assertGreaterEqual(readme.count("\n> "), 3)
        self.assertIn("## Privacy and security", readme)
        self.assertIn("GitHub Issues", readme)
        self.assertIn("SECURITY.md", readme)

    def test_release_check_includes_claude_plugin_metadata(self) -> None:
        self.assertEqual(check_release(), [])


if __name__ == "__main__":
    unittest.main()

