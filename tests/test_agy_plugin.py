from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from scripts.check_release import check_release

ROOT = Path(__file__).resolve().parents[1]


class AgyPluginTests(unittest.TestCase):
    def test_manifest_uses_the_canonical_problem_first_description(self) -> None:
        canonical = yaml.safe_load(
            (ROOT / "discovery/metadata.yaml").read_text(encoding="utf-8")
        )
        manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))

        self.assertEqual(
            manifest,
            {
                "$schema": "https://antigravity.google/schemas/v1/plugin.json",
                "name": "mergetrain",
                "description": canonical["short_description"],
            },
        )

    def test_mcp_config_is_release_pinned_and_provider_neutral(self) -> None:
        config = json.loads((ROOT / "mcp_config.json").read_text(encoding="utf-8"))
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        version = project["project"]["version"]

        self.assertEqual(
            config,
            {
                "mcpServers": {
                    "mergetrain": {
                        "command": "uvx",
                        "args": [
                            "--from",
                            f"mergetrain[mcp]=={version}",
                            "mergetrain",
                            "mcp",
                        ],
                    }
                }
            },
        )
        self.assertNotIn("env", config["mcpServers"]["mergetrain"])

    def test_release_check_includes_agy_plugin_metadata(self) -> None:
        self.assertEqual(check_release(), [])


if __name__ == "__main__":
    unittest.main()
