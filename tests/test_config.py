from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mergetrain.config import load_config, load_yaml, render_default_config


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


if __name__ == "__main__":
    unittest.main()
