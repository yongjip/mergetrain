from __future__ import annotations

import unittest
from pathlib import Path

from scripts.check_mcp_registry_launch import (
    isolated_uv_environment,
    registry_command,
    source_version,
)


class MCPRegistryLaunchTests(unittest.TestCase):
    def test_manifest_constructs_the_optional_extra_uvx_command(self) -> None:
        root = Path(__file__).resolve().parents[1]

        self.assertEqual(
            registry_command(root / "server.json"),
            [
                "uvx",
                "--from",
                "mergetrain[mcp]==3.0.5",
                "mergetrain",
                "mcp",
            ],
        )

    def test_each_retry_can_use_an_isolated_uv_cache(self) -> None:
        cache = Path("/tmp/one-attempt")

        env = isolated_uv_environment(cache)

        self.assertEqual(env["UV_CACHE_DIR"], str(cache))

    def test_source_version_matches_release_metadata(self) -> None:
        self.assertEqual(source_version(), "3.0.5")


if __name__ == "__main__":
    unittest.main()
