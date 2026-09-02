from __future__ import annotations

import unittest
from pathlib import Path

from scripts.check_mcp_registry_launch import registry_command


class MCPRegistryLaunchTests(unittest.TestCase):
    def test_manifest_constructs_the_optional_extra_uvx_command(self) -> None:
        root = Path(__file__).resolve().parents[1]

        self.assertEqual(
            registry_command(root / "server.json"),
            [
                "uvx",
                "--from",
                "mergetrain[mcp]==2.3.1",
                "mergetrain",
                "mcp",
            ],
        )


if __name__ == "__main__":
    unittest.main()
