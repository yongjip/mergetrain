from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.check_release import (
    SECURITY_SUPPORT_POLICY,
    _readme_status_errors,
    _release_workflow_errors,
    _security_policy_errors,
    _workflow_pin_errors,
    check_release,
)


class SecuritySupportPolicyTests(unittest.TestCase):
    def test_release_independent_policy_is_accepted(self) -> None:
        self.assertEqual(_security_policy_errors(SECURITY_SUPPORT_POLICY), [])

    def test_pre_1_0_lifecycle_drift_is_rejected(self) -> None:
        errors = _security_policy_errors(
            "mergetrain is pre-1.0. " + SECURITY_SUPPORT_POLICY
        )
        self.assertTrue(any("pre-1.0" in error for error in errors))

    def test_missing_support_policy_is_rejected(self) -> None:
        errors = _security_policy_errors("Security fixes are sometimes available.")
        self.assertTrue(any("supported-versions policy" in error for error in errors))


class ReleaseManifestTests(unittest.TestCase):
    def test_workflow_actions_require_full_commit_pins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workflows = Path(td)
            (workflows / "ci.yml").write_text(
                "steps:\n"
                "  - uses: actions/checkout@v7\n"
                "  - uses: ./.github/actions/local\n"
                "  - uses: owner/action@0123456789abcdef0123456789abcdef01234567\n",
                encoding="utf-8",
            )

            errors = _workflow_pin_errors(workflows)

        self.assertEqual(len(errors), 1)
        self.assertIn("actions/checkout@v7", errors[0])

    def test_readme_status_rejects_a_hard_coded_release_number(self) -> None:
        self.assertTrue(
            _readme_status_errors(
                "# Project\n\n## Status\n\nThe package version is `v2.1.0`.\n"
            )
        )

    def test_readme_status_accepts_badge_owned_release_copy(self) -> None:
        self.assertEqual(
            _readme_status_errors(
                "# Project\n\n## Status\n\nThe PyPI badge shows the latest release.\n"
            ),
            [],
        )

    def test_current_release_metadata_is_self_consistent(self) -> None:
        self.assertEqual(check_release(), [])

    def test_release_workflow_is_main_rooted_before_building(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        checkout = workflow.index("actions/checkout@")
        verify = workflow.index("scripts/verify_release_source.py verify")
        immutable = workflow.index(".immutable == true")
        build = workflow.index("python -m build")

        self.assertLess(checkout, verify)
        self.assertLess(verify, immutable)
        self.assertLess(verify, build)
        self.assertNotIn("types: [published]", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("github.ref != 'refs/heads/main'", workflow)
        self.assertIn("permissions: {}", workflow)
        self.assertIn("ref: ${{ needs.verify.outputs.commit_sha }}", workflow)
        self.assertIn(".github/release-allowed-signers", workflow)
        allowed_signers = (root / ".github/release-allowed-signers").read_text(
            encoding="utf-8"
        )
        self.assertIn("ssh-ed25519 ", allowed_signers)

    def test_release_verifier_and_builder_have_no_oidc_authority(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        verify = workflow.split("\n  verify:\n", 1)[1].split("\n  build:\n", 1)[0]
        build = workflow.split("\n  build:\n", 1)[1].split("\n  attest:\n", 1)[0]
        attest = workflow.split("\n  attest:\n", 1)[1].split("\n  publish:\n", 1)[0]

        for unprivileged in (verify, build):
            self.assertNotIn("id-token: write", unprivileged)
            self.assertNotIn("attestations: write", unprivileged)
            self.assertNotIn("environment:", unprivileged)
            self.assertNotIn("secrets.", unprivileged)
        self.assertNotIn("actions/checkout@", attest)
        self.assertIn("actions: read", attest)
        self.assertIn(
            'gh run download "${GITHUB_RUN_ID}" --repo "${GITHUB_REPOSITORY}"',
            attest,
        )

        publish = workflow.split("\n  publish:\n", 1)[1].split(
            "\n  publish-mcp-registry:\n", 1
        )[0]
        self.assertIn("actions: read", publish)
        self.assertIn(
            'gh run download "${GITHUB_RUN_ID}" --repo "${GITHUB_REPOSITORY}"',
            publish,
        )

    def test_testpypi_builds_only_the_verified_commit_from_main(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/test-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("github.ref != 'refs/heads/main'", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("scripts/verify_release_source.py verify", workflow)
        self.assertIn("ref: ${{ needs.verify.outputs.commit_sha }}", workflow)
        self.assertIn("environment: testpypi", workflow)

    def test_release_workflow_policy_check_accepts_current_files(self) -> None:
        self.assertEqual(_release_workflow_errors(), [])

    def test_release_workflow_policy_rejects_tag_trigger(self) -> None:
        self.assertTrue(
            any(
                "release event" in error
                for error in _release_workflow_errors(
                    "on:\n  release:\n    types: [published]\n"
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
