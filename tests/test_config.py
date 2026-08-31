from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from mergetrain.config import (
    CONFIG_VERSION,
    effective_gates,
    load_config,
    load_yaml,
    render_default_config,
)
from mergetrain.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def test_notify_webhook_is_validated_and_redacted_from_public_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                """project:
  name: demo
notify:
  webhook_url: https://notify.example.invalid/hook/super-secret
  transitions:
    - landed
    - needs_reconcile
  timeout_seconds: 4
""",
                encoding="utf-8",
            )
            config = load_config(repo=repo)

            self.assertEqual(
                config.notify.webhook_url,
                "https://notify.example.invalid/hook/super-secret",
            )
            self.assertEqual(config.notify.transitions, ("landed", "needs_reconcile"))
            public = config.to_dict()["notify"]
            self.assertTrue(public["webhook_configured"])
            self.assertNotIn("webhook_url", public)
            self.assertNotIn("super-secret", str(public))

    def test_notify_rejects_non_http_url_and_unknown_transition(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            config_path = repo / ".mergetrain.yaml"
            config_path.write_text(
                "notify:\n  webhook_url: file:///tmp/hook\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ConfigError, "http or https"):
                load_config(repo=repo)
            config_path.write_text(
                "notify:\n  transitions:\n    - landed\n    - surprise\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, r"notify.transitions\[1\]"):
                load_config(repo=repo)

    def test_generated_yaml_loads_with_required_parser(self) -> None:
        data = load_yaml(render_default_config("demo"))
        self.assertEqual(data["project"]["name"], "demo")
        self.assertEqual(data["git"]["push_refs"], ["main"])
        self.assertNotIn("agent", data)
        self.assertNotIn("terminology", data)
        self.assertEqual(data["gates"], [])

    def test_exact_builtin_diff_check_duplicate_is_not_effective(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                """git:\n  integration_branch: main\ngates:\n  - name: diff-check\n    run: git diff --check ${integration_ref}..HEAD\n  - name: tests\n    run: python -m unittest\n""",
                encoding="utf-8",
            )
            config = load_config(repo=repo)

        self.assertEqual([gate.name for gate in config.gates], ["diff-check", "tests"])
        self.assertEqual([gate.name for gate in effective_gates(config)], ["tests"])

    def test_custom_diff_check_named_gate_remains_effective(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                "gates:\n  - name: diff-check\n    run: ./scripts/custom-diff-check\n",
                encoding="utf-8",
            )
            config = load_config(repo=repo)

        self.assertEqual([gate.name for gate in effective_gates(config)], ["diff-check"])

    def test_version_one_removed_settings_are_migrated_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                """agent:
  require_clean_worktree_before_enqueue: false
  require_explicit_auto_approval: false
  prefer_json_status: false
terminology:
  git_operation: integrate
""",
                encoding="utf-8",
            )

            config = load_config(repo=repo)

            self.assertEqual(config.config_version, 1)
            self.assertNotIn("agent", config.to_dict())
            self.assertNotIn("terminology", config.to_dict())

    def test_version_two_rejects_removed_top_level_settings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            config_path = repo / ".mergetrain.yaml"
            for removed in ("agent", "terminology"):
                with self.subTest(removed=removed):
                    config_path.write_text(
                        f"version: 2\n{removed}: {{}}\n", encoding="utf-8"
                    )
                    with self.assertRaisesRegex(ConfigError, "removed top-level key"):
                        load_config(repo=repo)

    def test_yaml_loader_handles_comments_and_full_yaml_syntax(self) -> None:
        doc = (
            "lock_ttl_minutes: 30  # thirty\n"
            "gates:\n"
            "  - name: tests  # unit gate\n"
            "push_refs: [main, release]\n"
            "gate: {name: lint, run: ruff check .}\n"
            'quoted: "a # b"\n'
            "url: http://example.com/p#frag\n"
            "# a whole-line comment\n"
            "plain: value\n"
        )
        parsed = load_yaml(doc)
        self.assertEqual(parsed["lock_ttl_minutes"], 30)
        self.assertEqual(parsed["gates"][0]["name"], "tests")
        self.assertEqual(parsed["push_refs"], ["main", "release"])
        self.assertEqual(parsed["gate"], {"name": "lint", "run": "ruff check ."})
        self.assertEqual(parsed["quoted"], "a # b")
        self.assertEqual(parsed["url"], "http://example.com/p#frag")
        self.assertEqual(parsed["plain"], "value")
        self.assertNotIn("thirty", str(parsed))

    def test_yaml_policy_rejects_ambiguous_or_tab_indented_scalars(self) -> None:
        cases = {
            "git:\n\tremote: upstream\n": "tab indentation",
            "agent:\n  require_explicit_auto_approval: no\n": "true/false",
            "queue:\n  lock_ttl_minutes: 010\n": "quote it",
            "queue:\n  lock_ttl_minutes: 0x10\n": "quote it",
            "values: [yes]\n": "true/false",
            "values: [010]\n": "quote it",
        }
        for document, message in cases.items():
            with self.subTest(document=document):
                with self.assertRaisesRegex(ConfigError, message):
                    load_yaml(document)
        self.assertEqual(
            load_yaml("values: ['yes', '010']\n"),
            {"values": ["yes", "010"]},
        )

    def test_yaml_loader_handles_plain_config_shapes(self) -> None:
        document = (
            "project:\n"
            "    name: bob's app  # comment\n"
            "git:\n"
            "    push_refs:\n"
            "        - HEAD:main\n"
            "empty:\n"
        )
        parsed = load_yaml(document)
        self.assertEqual(parsed["project"]["name"], "bob's app")
        self.assertEqual(parsed["git"]["push_refs"], ["HEAD:main"])
        self.assertIsNone(parsed["empty"])

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

    def test_linked_worktrees_share_relative_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            control = root / "control"
            task = root / "task"
            subprocess.run(
                ["git", "init", "--initial-branch=main", str(control)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Config Test"],
                cwd=control,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "config@example.invalid"],
                cwd=control,
                check=True,
            )
            (control / ".mergetrain.yaml").write_text(
                render_default_config("demo"), encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", ".mergetrain.yaml"], cwd=control, check=True
            )
            subprocess.run(
                ["git", "commit", "-m", "initialize"],
                cwd=control,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "worktree", "add", "-b", "agent/task", str(task)],
                cwd=control,
                check=True,
                capture_output=True,
            )

            control_config = load_config(repo=control)
            task_config = load_config(repo=task)

            self.assertEqual(task_config.state, control_config.state)
            self.assertEqual(task_config.repo, task)
            self.assertEqual(task_config.config_path, task / ".mergetrain.yaml")
            self.assertEqual(
                task_config.state.db, control / ".mergetrain" / "queue.sqlite"
            )
            overridden = load_config(repo=task, db_override="override.sqlite")
            self.assertEqual(overridden.state.db, task / "override.sqlite")

    def test_malformed_linked_worktree_metadata_keeps_repo_relative_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td).resolve()
            (repo / ".git").write_text("gitdir: missing\n", encoding="utf-8")
            (repo / ".mergetrain.yaml").write_text(
                render_default_config("demo"), encoding="utf-8"
            )

            config = load_config(repo=repo)

            self.assertEqual(config.state.db, repo / ".mergetrain" / "queue.sqlite")

    def test_malformed_yaml_raises_config_error(self) -> None:
        # Parser failures must surface as ConfigError so the CLI exits cleanly
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

    def test_path_scoped_gate_parses_and_is_visible_in_public_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                """gates:
  - name: tests
    run: make test
    paths:
      - src/**
      - tests/**/*.py
      - pyproject.toml
""",
                encoding="utf-8",
            )
            config = load_config(repo=repo)

            self.assertEqual(
                config.gates[0].paths,
                ("src/**", "tests/**/*.py", "pyproject.toml"),
            )
            self.assertEqual(
                config.to_dict()["gates"][0]["paths"],
                ["src/**", "tests/**/*.py", "pyproject.toml"],
            )

    def test_parallel_gate_group_parses_resource_and_dependency_policy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                """gate_parallelism:
  max_workers: 4
  timeout_seconds: 900
gates:
  - name: lint
    run: ruff check .
    parallel_group: quality
    needs:
      - diff-check
    workers: 1
    timeout_seconds: 120
  - name: types
    run: mypy src
    parallel_group: quality
    workers: 2
""",
                encoding="utf-8",
            )

            config = load_config(repo=repo)

            self.assertEqual(config.gate_parallelism.max_workers, 4)
            self.assertEqual(config.gate_parallelism.timeout_seconds, 900)
            self.assertEqual(config.gates[0].parallel_group, "quality")
            self.assertEqual(config.gates[0].needs, ("diff-check",))
            self.assertEqual(config.gates[0].timeout_seconds, 120)
            self.assertEqual(config.gates[1].workers, 2)
            public = config.to_dict()
            self.assertEqual(public["gate_parallelism"]["max_workers"], 4)
            self.assertEqual(public["gates"][0]["needs"], ["diff-check"])

    def test_parallel_gate_defaults_remain_sequential_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                """gates:
  - name: lint
    run: ruff check .
  - name: tests
    run: pytest
""",
                encoding="utf-8",
            )

            config = load_config(repo=repo)

            self.assertEqual(config.gate_parallelism.max_workers, 1)
            self.assertEqual(config.gate_parallelism.timeout_seconds, None)
            self.assertEqual(config.gates[0].parallel_group, "")
            self.assertEqual(config.gates[0].workers, 1)
            self.assertEqual(config.gates[0].timeout_seconds, None)

    def test_invalid_parallel_gate_graph_and_resource_limits_are_rejected(self) -> None:
        invalid = (
            (
                """gates:
  - name: lint
    run: lint
    needs:
      - tests
  - name: tests
    run: tests
""",
                "earlier configured gate",
            ),
            (
                """gate_parallelism:
  max_workers: 2
gates:
  - name: tests
    run: tests
    workers: 3
""",
                "exceeds",
            ),
            (
                """gates:
  - name: lint
    run: lint
    parallel_group: quality
  - name: tests
    run: tests
  - name: types
    run: types
    parallel_group: quality
""",
                "contiguous",
            ),
            (
                """deploy:
  verify:
    - name: live
      run: live
      parallel_group: network
""",
                "unsupported",
            ),
        )
        for config_text, message in invalid:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                (repo / ".mergetrain.yaml").write_text(
                    config_text, encoding="utf-8"
                )
                with self.assertRaisesRegex(ConfigError, message):
                    load_config(repo=repo)

    def test_persistent_validation_workspace_requires_explicit_safe_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                """project:
  name: demo
state:
  worktree_root: .state/worktrees
  validation_workspace:
    mode: persistent
    cache_key: unity-2026-07
    cache_paths:
      - unity/Teratorn/Library
""",
                encoding="utf-8",
            )

            config = load_config(repo=repo)

            self.assertEqual(config.state.validation_workspace.mode, "persistent")
            self.assertEqual(
                config.state.validation_workspace.cache_paths,
                ("unity/Teratorn/Library",),
            )
            self.assertEqual(
                config.validation_worktree_path,
                (repo / ".state/worktrees/demo-validation-workspace").resolve(),
            )
            self.assertEqual(
                config.to_dict()["state"]["validation_workspace"]["path"],
                str(config.validation_worktree_path),
            )

    def test_invalid_persistent_validation_workspace_is_rejected(self) -> None:
        invalid = [
            (
                "mode: shared\n    cache_key: v1\n    cache_paths:\n      - .cache\n",
                "mode must be",
            ),
            (
                "mode: persistent\n    cache_paths:\n      - .cache\n",
                "cache_key is required",
            ),
            (
                "mode: persistent\n    cache_key: v1\n    cache_paths: []\n",
                "must not be empty",
            ),
            (
                "mode: persistent\n    cache_key: v1\n    cache_paths:\n      - ../cache\n",
                "repository-relative",
            ),
            (
                "mode: persistent\n    cache_key: v1\n    cache_paths:\n      - cache/**\n",
                "does not support glob",
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            config_path = repo / ".mergetrain.yaml"
            for workspace, message in invalid:
                with self.subTest(workspace=workspace):
                    config_path.write_text(
                        "state:\n  validation_workspace:\n    " + workspace,
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ConfigError, message):
                        load_config(repo=repo)

    def test_invalid_or_unsupported_gate_paths_are_rejected(self) -> None:
        invalid = [
            (
                "gates:\n  - name: tests\n    run: echo true\n    paths: []\n",
                "non-empty list",
            ),
            (
                "gates:\n  - name: tests\n    run: echo true\n    paths:\n      - /src/**\n",
                "repository-relative",
            ),
            (
                "gates:\n  - name: tests\n    run: echo true\n    paths:\n      - ../src/**\n",
                "path segments",
            ),
            (
                "gates:\n  - name: tests\n    run: echo true\n    paths:\n      - src/foo**bar\n",
                "complete path segment",
            ),
            (
                "gates:\n  - name: tests\n    run: echo true\n    paths:\n      - src/**\n      - src/**\n",
                "duplicates",
            ),
            (
                "deploy:\n  verify:\n    - name: live\n      run: echo true\n      paths:\n        - src/**\n",
                "unsupported",
            ),
            (
                "deploy:\n  reuse:\n    fingerprints:\n      - name: toolchain\n        run: echo true\n        paths:\n          - src/**\n",
                "unsupported",
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            path = repo / ".mergetrain.yaml"
            for document, message in invalid:
                with self.subTest(document=document):
                    path.write_text(document, encoding="utf-8")
                    with self.assertRaisesRegex(ConfigError, message):
                        load_config(repo=repo)

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
