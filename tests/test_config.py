from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mergetrain.config import load_config, load_yaml, render_default_config
from mergetrain.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def test_simple_yaml_shape_loads_without_required_dependency(self) -> None:
        data = load_yaml(render_default_config("demo"))
        self.assertEqual(data["project"]["name"], "demo")
        self.assertEqual(data["git"]["push_refs"], ["main"])
        self.assertEqual(data["gates"][0]["name"], "diff-check")

    def test_relative_paths_resolve_from_repo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Resolve symlinks (e.g. macOS /var -> /private/var) so the expected
            # paths match what load_config() produces after its own .resolve().
            repo = Path(td).resolve()
            (repo / ".mergetrain.yaml").write_text(render_default_config("demo"), encoding="utf-8")
            config = load_config(repo=repo)
            self.assertEqual(config.project.name, "demo")
            self.assertEqual(config.state.db, repo / ".mergetrain" / "queue.sqlite")
            self.assertEqual(config.git.integration_ref, "origin/main")


    def test_malformed_yaml_raises_config_error(self) -> None:
        # Whichever parser is active (PyYAML or the built-in subset parser), a
        # malformed document must surface as ConfigError so the CLI exits cleanly
        # with "mergetrain: error: ..." rather than dumping a raw traceback.
        with self.assertRaises(ConfigError):
            load_yaml("project:\n  name: x\n bad-indent: y\n")

    def test_explicit_empty_push_refs_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                "git:\n  integration_branch: main\n  push_refs: []\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "at least one ref"):
                load_config(repo=repo)

    def test_omitted_push_refs_defaults_to_integration_branch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                "git:\n  remote: origin\n  integration_branch: release\n",
                encoding="utf-8",
            )
            self.assertEqual(load_config(repo=repo).git.push_refs, ("release",))

    def test_invalid_queue_timing_and_duplicate_gate_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                "queue:\n  lock_ttl_minutes: 1\n  heartbeat_interval_seconds: 60\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "must be shorter"):
                load_config(repo=repo)
            (repo / ".mergetrain.yaml").write_text(
                "gates:\n  - name: tests\n    run: echo true\n"
                "deploy:\n  verify:\n    - name: tests\n      run: echo true\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "must be unique"):
                load_config(repo=repo)

    def test_validated_reuse_policy_parses_explicit_safety_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                """gates:
  - name: tests
    run: make test
    always_rerun_on_deploy: true
deploy:
  reuse:
    enabled: true
    max_age_minutes: 15
    on_mismatch: fail
    fingerprints:
      - name: toolchain
        run: scripts/toolchain-id
""",
                encoding="utf-8",
            )
            config = load_config(repo=repo)
            self.assertTrue(config.deploy.reuse.enabled)
            self.assertEqual(config.deploy.reuse.max_age_minutes, 15)
            self.assertEqual(config.deploy.reuse.on_mismatch, "fail")
            self.assertEqual(config.deploy.reuse.fingerprints[0].name, "toolchain")
            self.assertTrue(config.gates[0].always_rerun_on_deploy)

    def test_invalid_validated_reuse_policy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            invalid_values = [
                ("enabled: 1", "true or false"),
                ("max_age_minutes: 0", "positive integer"),
                ("on_mismatch: skip", "rerun.*fail"),
            ]
            for value, message in invalid_values:
                with self.subTest(value=value):
                    (repo / ".mergetrain.yaml").write_text(
                        f"deploy:\n  reuse:\n    {value}\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ConfigError, message):
                        load_config(repo=repo)


if __name__ == "__main__":
    unittest.main()
