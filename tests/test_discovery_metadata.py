from __future__ import annotations

import unittest

from scripts.check_discovery_metadata import (
    check_discovery_metadata,
    validate_canonical,
)


class DiscoveryMetadataTests(unittest.TestCase):
    def test_current_discovery_surfaces_match_canonical_metadata(self) -> None:
        self.assertEqual(check_discovery_metadata(), [])

    def test_duplicate_catalog_values_are_rejected(self) -> None:
        invalid = {
            "schema_version": 1,
            "headline": "Headline",
            "short_description": (
                "Parallel coding worktree committed branches combined push interrupted"
            ),
            "qualified_description": "Qualified",
            "skill_description": (
                "single-agent GitHub Merge Queue GitLab Merge Trains"
            ),
            "deploy_skill_description": "Deploy",
            "keywords": ["git", "git"],
            "catalog_tags": ["git"],
            "github_about": {"description": "About", "topics": ["git"]},
        }

        errors = validate_canonical(invalid)

        self.assertTrue(any("keywords must not contain duplicates" in e for e in errors))

    def test_gemini_is_not_a_canonical_client_signal(self) -> None:
        from pathlib import Path

        import yaml

        root = Path(__file__).resolve().parents[1]
        metadata = yaml.safe_load(
            (root / "discovery/metadata.yaml").read_text(encoding="utf-8")
        )
        rendered = " ".join(
            str(value) for key, value in metadata.items() if key != "github_about"
        ).lower()
        self.assertNotIn("gemini", rendered)
        self.assertIn("antigravity-cli", metadata["github_about"]["topics"])


if __name__ == "__main__":
    unittest.main()
