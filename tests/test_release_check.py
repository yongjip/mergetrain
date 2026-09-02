from __future__ import annotations

import unittest

from scripts.check_release import (
    SECURITY_SUPPORT_POLICY,
    _readme_status_errors,
    _security_policy_errors,
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


if __name__ == "__main__":
    unittest.main()
