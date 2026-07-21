from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mergetrain.errors import QueueError
from mergetrain.registry import (
    add_repo,
    load_registry,
    registry_path,
    remove_repo,
    save_registry,
)


def make_repo(root: Path, name: str = "svc") -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    (repo / ".mergetrain.yaml").write_text(f"project:\n  name: {name}\n", encoding="utf-8")
    return repo


class RegistryTests(unittest.TestCase):
    def test_registry_path_honors_env_override_then_xdg(self) -> None:
        with mock.patch.dict(
            "os.environ", {"MERGETRAIN_HUB_REGISTRY": "/custom/repos.json"}, clear=False
        ):
            self.assertEqual(registry_path(), Path("/custom/repos.json"))
        with mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": "/xdg"}, clear=False):
            with mock.patch.dict("os.environ") as env:
                env.pop("MERGETRAIN_HUB_REGISTRY", None)
                self.assertEqual(registry_path(), Path("/xdg/mergetrain/repos.json"))

    def test_missing_registry_is_empty_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_registry(Path(td) / "absent.json"), [])

    def test_add_requires_config_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            with self.assertRaises(QueueError):
                add_repo(root / "nope", registry)
            bare = root / "bare"
            bare.mkdir()
            with self.assertRaises(QueueError):
                add_repo(bare, registry)
            repo = make_repo(root)
            first = add_repo(repo, registry)
            second = add_repo(repo, registry)
            self.assertEqual(first["path"], str(repo.resolve()))
            self.assertEqual(first, second)
            self.assertEqual(len(load_registry(registry)), 1)

    def test_daemon_flag_defaults_upserts_and_survives_reload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            repo = make_repo(root)
            self.assertTrue(add_repo(repo, registry)["daemon"])
            self.assertTrue(add_repo(repo, registry, daemon=None)["daemon"])
            self.assertFalse(add_repo(repo, registry, daemon=False)["daemon"])
            self.assertFalse(load_registry(registry)[0]["daemon"])
            self.assertFalse(add_repo(repo, registry, daemon=None)["daemon"])
            self.assertTrue(add_repo(repo, registry, daemon=True)["daemon"])
            other = make_repo(root, "other")
            self.assertFalse(add_repo(other, registry, daemon=False)["daemon"])

    def test_remove_reports_membership_and_keeps_repo_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            repo = make_repo(root)
            add_repo(repo, registry)
            self.assertTrue(remove_repo(repo, registry))
            self.assertFalse(remove_repo(repo, registry))
            self.assertEqual(load_registry(registry), [])
            self.assertTrue((repo / ".mergetrain.yaml").is_file())

    def test_alias_of_registered_repo_is_the_same_entry(self) -> None:
        # An existing roster entry may name the repo through a different but
        # samefile-identical string (case aliases on macOS, hand edits). The
        # policy flag must follow the physical repo, not the spelling.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            repo = make_repo(root)
            alias = f"{repo.resolve()}{'/.'}"
            save_registry(
                [{"path": alias, "added_at": "t", "daemon": False}], registry
            )
            entry = add_repo(repo, registry)
            self.assertEqual(len(load_registry(registry)), 1)
            self.assertEqual(entry["path"], alias)
            self.assertFalse(load_registry(registry)[0]["daemon"])
            # Re-adding through the canonical spelling must not resurrect
            # eligibility, and removal must match the alias too.
            add_repo(repo, registry, daemon=True)
            self.assertTrue(load_registry(registry)[0]["daemon"])
            add_repo(repo, registry, daemon=False)
            self.assertTrue(remove_repo(repo, registry))
            self.assertEqual(load_registry(registry), [])

    def test_hand_edited_non_boolean_daemon_flag_fails_safe_to_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "repos.json"
            save_registry(
                [
                    {"path": "/a", "added_at": "t", "daemon": "false"},
                    {"path": "/b", "added_at": "t", "daemon": "true"},
                    {"path": "/c", "added_at": "t", "daemon": True},
                ],
                registry,
            )
            loaded = {entry["path"]: entry["daemon"] for entry in load_registry(registry)}
            # Non-boolean values are never truthy-coerced into eligibility.
            self.assertEqual(loaded, {"/a": False, "/b": False, "/c": True})

    def test_save_is_atomic_json_and_bad_shapes_fail_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "repos.json"
            save_registry([{"path": "/a", "added_at": "t"}], registry)
            data = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(data["version"], 1)
            self.assertEqual(data["repos"][0]["path"], "/a")
            self.assertFalse(list(registry.parent.glob(".repos-*.tmp")))
            registry.write_text("[]", encoding="utf-8")
            with self.assertRaises(QueueError):
                load_registry(registry)
            registry.write_text("not json", encoding="utf-8")
            with self.assertRaises(QueueError):
                load_registry(registry)


if __name__ == "__main__":
    unittest.main()
