from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mergetrain.config import (
    CONFIG_VERSION,
    _parse_simple_yaml,
    load_config,
    load_yaml,
    render_default_config,
)
from mergetrain.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def test_simple_yaml_shape_loads_without_required_dependency(self) -> None:
        data = load_yaml(render_default_config("demo"))
        self.assertEqual(data["project"]["name"], "demo")
        self.assertEqual(data["git"]["push_refs"], ["main"])
        self.assertEqual(data["terminology"]["git_operation"], "deploy")
        self.assertEqual(data["gates"][0]["name"], "diff-check")

    def test_fallback_parser_strips_inline_comments_like_pyyaml(self) -> None:
        # The zero-dependency parser is the DEFAULT path (no runtime deps), so it
        # must match PyYAML on inline comments — otherwise the same config parses
        # differently with vs without PyYAML: `lock_ttl_minutes: 30  # x` would
        # become the string "30  # x" and `- name: tests  # x` a corrupted gate.
        doc = (
            "lock_ttl_minutes: 30  # thirty\n"
            "gates:\n"
            "  - name: tests  # unit gate\n"
            'quoted: "a # b"\n'
            "url: http://example.com/p#frag\n"
            "# a whole-line comment\n"
            "plain: value\n"
        )
        parsed = _parse_simple_yaml(doc)
        self.assertEqual(parsed["lock_ttl_minutes"], 30)  # int, not "30  # thirty"
        self.assertEqual(parsed["gates"][0]["name"], "tests")  # not "tests  # unit gate"
        self.assertEqual(parsed["quoted"], "a # b")  # '#' inside quotes is literal
        self.assertEqual(parsed["url"], "http://example.com/p#frag")  # no space -> not a comment
        self.assertEqual(parsed["plain"], "value")
        self.assertNotIn("thirty", str(parsed))
        # Parity: where PyYAML is installed, the built-in parser agrees with it.
        try:
            import yaml
        except Exception:  # pragma: no cover - only when PyYAML is absent
            return
        self.assertEqual(parsed, yaml.safe_load(doc))

    def test_fallback_parser_rejects_unsupported_flow_collections(self) -> None:
        for value in ("[main, release]", "{name: tests}"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigError, "flow-style YAML"):
                    _parse_simple_yaml(f"value: {value}\n")

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
            self.assertEqual(config.terminology.completed, "deployed")

    def test_integration_terminology_has_derived_human_words(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                "terminology:\n  git_operation: integrate\n",
                encoding="utf-8",
            )
            config = load_config(repo=repo)
            self.assertEqual(
                config.terminology.to_dict(),
                {
                    "git_operation": "integrate",
                    "action": "integrate",
                    "in_progress": "integrating",
                    "completed": "integrated",
                    "noun": "integration",
                },
            )
            self.assertEqual(config.to_dict()["terminology"]["completed"], "integrated")

    def test_invalid_git_operation_terminology_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                "terminology:\n  git_operation: release\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "deploy.*integrate.*push"):
                load_config(repo=repo)


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

    def test_config_version_defaults_absent_records_and_tolerates_newer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            cfg = repo / ".mergetrain.yaml"
            # A pre-versioning file (no `version:`) rides forward as version 1.
            cfg.write_text("project:\n  name: legacy\n", encoding="utf-8")
            self.assertEqual(load_config(repo=repo).config_version, 1)

            # The default writers stamp the current version.
            cfg.write_text(render_default_config("demo"), encoding="utf-8")
            config = load_config(repo=repo)
            self.assertEqual(config.config_version, CONFIG_VERSION)
            # config_version reaches JSON consumers via to_dict().
            self.assertEqual(config.to_dict()["config_version"], CONFIG_VERSION)

            # A too-new version is RECORDED, not rejected here (enforcement is
            # command-scoped) — load_config must never lock recovery out.
            cfg.write_text("version: 999\nproject:\n  name: future\n", encoding="utf-8")
            self.assertEqual(load_config(repo=repo).config_version, 999)

    def test_config_version_must_be_a_positive_integer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            cfg = repo / ".mergetrain.yaml"
            for bad in ("version: nope", "version: 0", "version: true"):
                with self.subTest(bad=bad):
                    cfg.write_text(f"{bad}\nproject:\n  name: x\n", encoding="utf-8")
                    with self.assertRaisesRegex(ConfigError, "version"):
                        load_config(repo=repo)

    def test_config_strings_and_unicode_integers_fail_with_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            cfg = repo / ".mergetrain.yaml"
            invalid = [
                ("project:\n  name:\n    nested: value\n", "project.name"),
                ("state:\n  db:\n    nested: value\n", "state.db"),
                ("queue:\n  lock_ttl_minutes: ²\n", "positive integer"),
            ]
            for text, message in invalid:
                with self.subTest(text=text):
                    cfg.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(ConfigError, message):
                        load_config(repo=repo)

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
