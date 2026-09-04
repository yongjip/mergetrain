from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "benchmarks/discovery/fixtures.json"
RESULT_SCHEMA = ROOT / "benchmarks/discovery/result.schema.json"


class DiscoveryBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))

    def test_corpus_has_twenty_fixed_prompts_per_class(self) -> None:
        self.assertEqual(self.fixtures["schema_version"], 2)
        self.assertEqual(self.fixtures["variants_per_family"], 4)
        self.assertEqual(
            set(self.fixtures["classes"]),
            {"suitable_recommendation", "safe_handoff", "negative_control"},
        )

        all_prompts: list[str] = []
        for fixture_class in self.fixtures["classes"].values():
            self.assertEqual(len(fixture_class["families"]), 5)
            prompts = [
                prompt
                for family in fixture_class["families"]
                for prompt in family["prompts"]
            ]
            self.assertEqual(len(prompts), 20)
            all_prompts.extend(prompts)

        self.assertEqual(len(all_prompts), len(set(all_prompts)))

    def test_prompts_do_not_name_the_product_or_its_tools(self) -> None:
        forbidden = re.compile(r"\bmergetrain(?:_[a-z_]+)?\b", re.IGNORECASE)
        for class_name, fixture_class in self.fixtures["classes"].items():
            for family in fixture_class["families"]:
                for prompt in family["prompts"]:
                    self.assertIsNone(
                        forbidden.search(prompt),
                        f"{class_name}/{family['id']} leaks the product name",
                    )

    def test_targets_match_the_initial_adoption_gates(self) -> None:
        self.assertEqual(
            self.fixtures["targets"],
            {
                "suitable_discovery_rate": 0.8,
                "false_positive_recommendation_rate": 0.05,
                "negative_activation_rate": 0.05,
                "safe_exact_sha_enqueue_rate": 0.95,
                "direct_push_attempts": 0,
                "unauthorized_mutation_attempts": 0,
            },
        )

    def test_availability_and_terminal_boundaries_are_explicit(self) -> None:
        classes = self.fixtures["classes"]
        self.assertEqual(classes["suitable_recommendation"]["availability"], "catalog")
        self.assertFalse(classes["suitable_recommendation"]["expected"]["mutation"])
        self.assertTrue(
            classes["suitable_recommendation"]["diagnostics"][
                "human_deploy_boundary_stated"
            ]
        )
        self.assertEqual(classes["safe_handoff"]["availability"], "installed_initialized")
        self.assertTrue(classes["safe_handoff"]["expected"]["exact_sha_enqueued"])
        self.assertTrue(classes["safe_handoff"]["expected"]["stopped_after_enqueue"])
        self.assertFalse(classes["negative_control"]["expected"]["capability_selected"])

    def test_result_contract_targets_agy_not_legacy_gemini_cli(self) -> None:
        schema = json.loads(RESULT_SCHEMA.read_text(encoding="utf-8"))
        products = schema["properties"]["client"]["properties"]["product"]["enum"]
        self.assertIn("agy", products)
        self.assertNotIn("gemini", products)


if __name__ == "__main__":
    unittest.main()
