from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/mergetrain"


class CodexPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.canonical = yaml.safe_load(
            (ROOT / "discovery/metadata.yaml").read_text(encoding="utf-8")
        )
        self.manifest = json.loads(
            (PLUGIN / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )

    def test_marketplace_points_to_the_native_plugin(self) -> None:
        marketplace = json.loads(
            (ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8")
        )

        self.assertEqual(marketplace["name"], "mergetrain")
        self.assertEqual(
            marketplace["plugins"],
            [
                {
                    "name": "mergetrain",
                    "source": {
                        "source": "local",
                        "path": "./plugins/mergetrain",
                    },
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                    "category": "DeveloperTools",
                }
            ],
        )

    def test_manifest_uses_canonical_problem_first_copy(self) -> None:
        interface = self.manifest["interface"]
        self.assertEqual(self.manifest["description"], self.canonical["short_description"])
        self.assertEqual(self.manifest["keywords"], self.canonical["catalog_tags"])
        self.assertEqual(interface["shortDescription"], self.canonical["headline"])
        self.assertEqual(
            interface["longDescription"], self.canonical["qualified_description"]
        )
        self.assertEqual(
            interface["defaultPrompt"], self.canonical["codex_default_prompts"]
        )

    def test_mcp_server_is_release_pinned_and_unchanged(self) -> None:
        config = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        version = project["project"]["version"]
        server = config["mcpServers"]["mergetrain"]

        self.assertEqual(
            server["args"],
            [
                "--from",
                f"mergetrain[mcp]=={version}",
                "mergetrain",
                "mcp",
            ],
        )
        self.assertEqual(server["description"], self.canonical["mcp_description"])
        self.assertEqual(set(config["mcpServers"]), {"mergetrain"})
        self.assertNotIn("env", server)


if __name__ == "__main__":
    unittest.main()
