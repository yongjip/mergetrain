from __future__ import annotations

import math
import unittest
from datetime import datetime, timezone
from pathlib import Path

from mergetrain.config import (
    AgentConfig,
    DeployConfig,
    GitConfig,
    MergetrainConfig,
    ProjectConfig,
    QueueConfig,
    ReuseConfig,
    StateConfig,
    TerminologyConfig,
)
from mergetrain.models import Job
from mergetrain.reuse import (
    ReuseDecision,
    _sha256_json,
    environment_sha,
    gate_policy_sha,
    train_identity_sha,
    validation_age_minutes,
)

# A fixed "now" so validation_age_minutes assertions are deterministic.
NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def _config(project_name: str = "demo") -> MergetrainConfig:
    return MergetrainConfig(
        project=ProjectConfig(name=project_name),
        state=StateConfig(db=Path("/x/db"), logs=Path("/x/logs"), worktree_root=Path("/x/wt")),
        git=GitConfig(remote="origin", integration_branch="main", push_refs=("main",)),
        queue=QueueConfig(),
        agent=AgentConfig(),
        terminology=TerminologyConfig(),
        gates=(),
        deploy=DeployConfig(verify=(), reuse=ReuseConfig()),
        repo=Path("/x"),
        config_path=Path("/x/.mergetrain.yaml"),
        config_exists=False,
    )


class Sha256JsonTests(unittest.TestCase):
    def test_canonical_hashes_are_stable_and_key_order_independent(self) -> None:
        # sort_keys + no whitespace: {a,b} and {b,a} hash identically.
        self.assertEqual(
            _sha256_json({"a": 1, "b": 2}),
            "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777",
        )
        self.assertEqual(_sha256_json({"b": 2, "a": 1}), _sha256_json({"a": 1, "b": 2}))
        self.assertEqual(
            _sha256_json([1, 2, 3]),
            "a615eeaee21de5179de080de8c3052c8da901138406ba71c38c032845f7d54f4",
        )
        self.assertEqual(
            _sha256_json("hello"),
            "5aa762ae383fbb727af3c7a36d4940a5b8c40a989452d2304fc958ff3f354e7a",
        )
        # ensure_ascii=False -> non-ASCII is hashed as UTF-8 bytes, not \uXXXX.
        self.assertEqual(
            _sha256_json({"k": "café"}),
            "2303df0176226e83b89fa2a9311d76a8a4c29b0e8ae83ffaa0e431fa4f8b5359",
        )


class EnvironmentShaTests(unittest.TestCase):
    def test_order_sensitive_and_stable(self) -> None:
        a = environment_sha([("os", "linux"), ("py", "3.11")])
        self.assertEqual(
            a, "0729f74de7d4a449ea4ee569a9b88e5b0ccf696423fe4efde4f3f63655e4516d"
        )
        # it is a list, not a sorted set: reversing the pairs changes the hash.
        b = environment_sha([("py", "3.11"), ("os", "linux")])
        self.assertEqual(
            b, "91837123729852bc26a33bb22946fb1472b2978c74a797ce02746e75ce7ed22e"
        )
        self.assertNotEqual(a, b)
        self.assertEqual(
            environment_sha([]),
            "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
        )


class TrainIdentityShaTests(unittest.TestCase):
    def _jobs(self) -> list[Job]:
        return [
            Job(id=1, task="t1", branch="agent/a", train_id="train-1", train_size=2,
                validated_head_sha="aaa"),
            Job(id=2, task="t2", branch="agent/b", train_id="train-1", train_size=2,
                validated_head_sha="bbb"),
        ]

    def test_derived_explicit_and_validated_heads_agree(self) -> None:
        golden = "af68d7fc9af32a871aff78595541508f4511c955f478ded13ca413f74e5b503e"
        jobs = self._jobs()
        # train_id/train_size derived from jobs[0]; validated_head_sha from each job.
        self.assertEqual(train_identity_sha(jobs), golden)
        # passing the same values explicitly resolves identically.
        self.assertEqual(train_identity_sha(jobs, train_id="train-1", train_size=2), golden)
        self.assertEqual(
            train_identity_sha(jobs, validated_heads={1: "aaa", 2: "bbb"}), golden
        )

    def test_changes_when_a_member_field_changes(self) -> None:
        jobs = self._jobs()
        baseline = train_identity_sha(jobs)
        jobs[1].validated_head_sha = "ccc"
        self.assertEqual(
            train_identity_sha(jobs),
            "946b4497e0b412cfb9ebc6cde7260088966c5341156d759ca1361503b807a9bd",
        )
        self.assertNotEqual(train_identity_sha(jobs), baseline)

    def test_empty_train_is_stable(self) -> None:
        self.assertEqual(
            train_identity_sha([]),
            "9a54d17665f812dd223da1b841ea7c18f8a5aa398b026bb1dd37d5101ae91231",
        )


class ValidationAgeMinutesTests(unittest.TestCase):
    def test_edge_cases(self) -> None:
        # empty and unparseable -> infinite age (never reusable).
        self.assertTrue(math.isinf(validation_age_minutes("", now=NOW)))
        self.assertTrue(math.isinf(validation_age_minutes("not-a-date", now=NOW)))
        # Z is normalized to +00:00.
        self.assertEqual(validation_age_minutes("2026-07-22T11:50:00Z", now=NOW), 10.0)
        # a naive timestamp is treated as UTC.
        self.assertEqual(validation_age_minutes("2026-07-22T11:30:00", now=NOW), 30.0)
        # explicit offsets are honored.
        self.assertEqual(validation_age_minutes("2026-07-22T13:00:00+02:00", now=NOW), 60.0)
        self.assertEqual(validation_age_minutes("2026-07-22T12:00:00Z", now=NOW), 0.0)
        # a future timestamp (negative age) clamps to infinity, never negative.
        self.assertTrue(math.isinf(validation_age_minutes("2026-07-22T13:00:00Z", now=NOW)))


class GatePolicyShaTests(unittest.TestCase):
    def test_deterministic_and_sensitive_to_policy(self) -> None:
        sha = gate_policy_sha(_config())
        self.assertEqual(len(sha), 64)
        self.assertEqual(sha, gate_policy_sha(_config()))  # deterministic
        # the reuse fingerprint must change when the policy inputs change.
        self.assertNotEqual(sha, gate_policy_sha(_config(project_name="other")))


class ReuseDecisionTests(unittest.TestCase):
    def test_to_dict_converts_reasons_tuple_to_list(self) -> None:
        decision = ReuseDecision(True, True, "reuse", "x", reasons=("a", "b"))
        self.assertEqual(
            decision.to_dict(),
            {
                "authorized": True,
                "eligible": True,
                "action": "reuse",
                "validation_sha": "x",
                "reused_validation_sha": "",
                "reasons": ["a", "b"],
            },
        )


if __name__ == "__main__":
    unittest.main()
