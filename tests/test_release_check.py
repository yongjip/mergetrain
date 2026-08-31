from __future__ import annotations

import unittest

from scripts.check_release import SECURITY_SUPPORT_POLICY, _security_policy_errors


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


if __name__ == "__main__":
    unittest.main()
