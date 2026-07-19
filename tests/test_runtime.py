from __future__ import annotations

import json
import tempfile
import unittest
from importlib import metadata
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from mergetrain.runtime import runtime_provenance


class FakeDistribution:
    def __init__(self, root: Path, *, direct_url: dict[str, object] | None = None) -> None:
        self.root = root
        self.version = "0.1.0"
        self.files = [PurePosixPath("mergetrain/__init__.py")]
        self._direct_url = direct_url

    def read_text(self, filename: str) -> str | None:
        if filename != "direct_url.json" or self._direct_url is None:
            return None
        return json.dumps(self._direct_url)

    def locate_file(self, path: object) -> Path:
        return self.root / Path(str(path))


class RuntimeProvenanceTests(unittest.TestCase):
    def test_matching_installed_distribution_reports_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site_packages = Path(td)
            package_path = site_packages / "mergetrain"
            package_path.mkdir()
            distribution = FakeDistribution(site_packages)
            with patch("mergetrain.runtime.metadata.distribution", return_value=distribution):
                payload = runtime_provenance(package_path=package_path)
        self.assertEqual(payload["distribution_version"], "0.1.0")
        self.assertEqual(payload["install_mode"], "wheel")
        self.assertIsNone(payload["source_path"])
        self.assertIsNone(payload["source_dirty"])

    def test_editable_distribution_reports_source_git_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "checkout"
            package_path = source / "src" / "mergetrain"
            package_path.mkdir(parents=True)
            distribution = FakeDistribution(
                Path(td) / "site-packages",
                direct_url={"url": source.as_uri(), "dir_info": {"editable": True}},
            )
            with (
                patch("mergetrain.runtime.metadata.distribution", return_value=distribution),
                patch("mergetrain.runtime._git_provenance", return_value=("a" * 40, True)),
            ):
                payload = runtime_provenance(package_path=package_path)
        self.assertEqual(payload["install_mode"], "editable")
        self.assertEqual(payload["source_path"], str(source.resolve()))
        self.assertEqual(payload["source_commit"], "a" * 40)
        self.assertTrue(payload["source_dirty"])

    def test_vcs_wheel_uses_pep_610_commit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            site_packages = Path(td)
            package_path = site_packages / "mergetrain"
            package_path.mkdir()
            distribution = FakeDistribution(
                site_packages,
                direct_url={
                    "url": "https://github.com/yongjip/mergetrain.git",
                    "vcs_info": {"vcs": "git", "commit_id": "b" * 40},
                },
            )
            with patch("mergetrain.runtime.metadata.distribution", return_value=distribution):
                payload = runtime_provenance(package_path=package_path)
        self.assertEqual(payload["install_mode"], "wheel")
        self.assertEqual(payload["source_commit"], "b" * 40)

    def test_missing_or_unrelated_metadata_degrades_to_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            package_path = Path(td) / "source" / "mergetrain"
            package_path.mkdir(parents=True)
            distribution = FakeDistribution(Path(td) / "site-packages")
            with patch("mergetrain.runtime.metadata.distribution", return_value=distribution):
                payload = runtime_provenance(package_path=package_path)
        self.assertEqual(payload["install_mode"], "unknown")
        self.assertIsNone(payload["source_commit"])
        self.assertIsNone(payload["source_dirty"])

    def test_uninstalled_source_checkout_reports_git_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "checkout"
            package_path = source / "src" / "mergetrain"
            package_path.mkdir(parents=True)
            with (
                patch(
                    "mergetrain.runtime.metadata.distribution",
                    side_effect=metadata.PackageNotFoundError("mergetrain"),
                ),
                patch("mergetrain.runtime._source_checkout_path", return_value=source),
                patch("mergetrain.runtime._git_provenance", return_value=("c" * 40, False)),
            ):
                payload = runtime_provenance(package_path=package_path)
        self.assertEqual(payload["install_mode"], "unknown")
        self.assertEqual(payload["source_path"], str(source))
        self.assertEqual(payload["source_commit"], "c" * 40)
        self.assertFalse(payload["source_dirty"])


if __name__ == "__main__":
    unittest.main()
