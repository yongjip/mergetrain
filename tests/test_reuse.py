from __future__ import annotations

import io
import math
import subprocess
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from mergetrain.config import (
    DeployConfig,
    GateConfig,
    GateParallelismConfig,
    GitConfig,
    MergetrainConfig,
    ProjectConfig,
    QueueConfig,
    ReuseConfig,
    StateConfig,
)
from mergetrain.deploy_plan import deploy_execution_policy_sha
from mergetrain.errors import CommandFailed
from mergetrain.models import Job
from mergetrain.reuse import (
    ReuseCheck,
    ReuseDecision,
    _sha256_json,
    environment_sha,
    gate_policy_sha,
    reuse_explanation,
    train_identity_sha,
    validation_age_minutes,
)
from mergetrain.validation_reuse import ValidationReuse, unauthorized_reuse_decision

# A fixed "now" so validation_age_minutes assertions are deterministic.
NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def _config(
    project_name: str = "demo",
    *,
    gate_paths: tuple[str, ...] = (),
) -> MergetrainConfig:
    return MergetrainConfig(
        project=ProjectConfig(name=project_name),
        state=StateConfig(db=Path("/x/db"), logs=Path("/x/logs"), worktree_root=Path("/x/wt")),
        git=GitConfig(remote="origin", integration_branch="main", push_refs=("main",)),
        queue=QueueConfig(),
        gates=(
            (
                GateConfig(
                    name="tests",
                    run="make test",
                    paths=gate_paths,
                ),
            )
            if gate_paths
            else ()
        ),
        gate_parallelism=GateParallelismConfig(),
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
        self.assertNotEqual(
            gate_policy_sha(_config(gate_paths=("src/**",))),
            gate_policy_sha(_config(gate_paths=("docs/**",))),
        )
        self.assertNotEqual(
            sha,
            gate_policy_sha(_config(gate_paths=("src/**",))),
        )
        self.assertNotEqual(
            sha,
            gate_policy_sha(
                replace(
                    _config(),
                    queue=replace(
                        _config().queue,
                        command_timeout_seconds=120,
                    ),
                )
            ),
        )


class DeployExecutionPolicyShaTests(unittest.TestCase):
    def test_stable_and_sensitive_to_every_authorized_execution_surface(self) -> None:
        config = _config(gate_paths=("src/**",))
        baseline = deploy_execution_policy_sha(config)

        self.assertEqual(baseline, deploy_execution_policy_sha(config))
        self.assertNotEqual(
            baseline,
            deploy_execution_policy_sha(
                replace(
                    config,
                    queue=replace(config.queue, command_timeout_seconds=120),
                )
            ),
        )
        self.assertNotEqual(
            baseline,
            deploy_execution_policy_sha(
                replace(
                    config,
                    deploy=replace(
                        config.deploy,
                        verify=(GateConfig(name="live", run="./smoke"),),
                    ),
                )
            ),
        )
        self.assertNotEqual(
            baseline,
            deploy_execution_policy_sha(
                replace(
                    config,
                    deploy=replace(
                        config.deploy,
                        reuse=replace(config.deploy.reuse, enabled=True),
                    ),
                )
            ),
        )

    def test_operational_poll_interval_is_not_an_execution_policy_input(self) -> None:
        config = _config()
        changed = replace(
            config,
            queue=replace(config.queue, daemon_interval_seconds=99),
        )

        self.assertEqual(
            deploy_execution_policy_sha(config),
            deploy_execution_policy_sha(changed),
        )


class ReuseDecisionTests(unittest.TestCase):
    def test_to_dict_exposes_structured_identity_checks(self) -> None:
        decision = ReuseDecision(
            True,
            True,
            "reuse",
            "x",
            reasons=("a", "b"),
            checks=(
                ReuseCheck(
                    code="gate_policy",
                    status="match",
                    expected="old",
                    actual="old",
                ),
            ),
        )
        self.assertEqual(
            decision.to_dict(),
            {
                "evaluation": "exact",
                "authorized": True,
                "eligible": True,
                "action": "reuse",
                "validation_sha": "x",
                "reused_validation_sha": "",
                "reasons": ["a", "b"],
                "identity_checks": [
                    {
                        "code": "gate_policy",
                        "status": "match",
                        "expected": "old",
                        "actual": "old",
                        "detail": "",
                    }
                ],
            },
        )

    def test_explanation_separates_exact_savings_from_authorization(self) -> None:
        config = replace(
            _config(),
            gates=(
                GateConfig(name="tests", run="pytest"),
                GateConfig(
                    name="smoke",
                    run="scripts/smoke",
                    always_rerun_on_deploy=True,
                ),
            ),
        )
        decision = ReuseDecision(
            authorized=True,
            eligible=True,
            action="reuse",
            validation_sha="a" * 40,
            reused_validation_sha="a" * 40,
            changed_paths=(),
        )
        runs = [
            {
                "name": name,
                "state": "success",
                "duration_seconds": duration,
            }
            for name, duration in (
                ("diff-check", 1.0),
                ("diff-check", 2.0),
                ("diff-check", 3.0),
                ("tests", 8.0),
                ("tests", 9.0),
                ("tests", 10.0),
            )
        ]

        payload = reuse_explanation(
            config,
            [Job(id=1, task="a", branch="feature/a", validation_sha="a" * 40)],
            decision=decision,
            gate_runs=runs,
        )

        self.assertEqual(
            [(gate["name"], gate["action"]) for gate in payload["gates"]],
            [
                ("diff-check", "reuse"),
                ("tests", "reuse"),
                ("smoke", "rerun"),
            ],
        )
        savings = payload["estimated_savings"]
        self.assertEqual(savings["seconds"], 11.0)
        self.assertEqual(savings["coverage"], 1.0)
        self.assertEqual(savings["confidence"], "medium")
        self.assertFalse(savings["authorizes_reuse"])

    def test_explanation_represents_scoped_gates_without_guessing(self) -> None:
        config = _config(gate_paths=("src/**",))

        potential = reuse_explanation(
            config,
            [Job(id=1, task="a", branch="feature/a", validation_sha="a" * 40)],
            decision=None,
        )
        self.assertIsNone(potential["eligible"])
        self.assertEqual(
            potential["gates"][1]["action"], "conditional_reuse"
        )
        self.assertEqual(
            potential["estimated_savings"]["mode"], "potential"
        )
        self.assertFalse(
            potential["estimated_savings"]["authorizes_reuse"]
        )

        exact = reuse_explanation(
            config,
            [Job(id=1, task="a", branch="feature/a", validation_sha="a" * 40)],
            decision=ReuseDecision(
                authorized=True,
                eligible=True,
                action="reuse",
                validation_sha="a" * 40,
                changed_paths=("docs/readme.md",),
            ),
        )
        self.assertEqual(exact["gates"][1]["action"], "skip")
        self.assertEqual(exact["gates"][1]["reason_code"], "no_matching_paths")

        unknown = reuse_explanation(
            config,
            [Job(id=1, task="a", branch="feature/a", validation_sha="a" * 40)],
            decision=ReuseDecision(
                authorized=True,
                eligible=True,
                action="reuse",
                validation_sha="a" * 40,
                changed_paths=None,
            ),
        )
        self.assertEqual(unknown["gates"][1]["action"], "rerun")
        self.assertEqual(unknown["gates"][1]["reason_code"], "path_discovery_unavailable")

    def test_an_authorized_mismatch_reruns_every_gate_and_estimates_nothing(self) -> None:
        payload = reuse_explanation(
            _config(gate_paths=("src/**",)),
            [Job(id=1, task="a", branch="feature/a", validation_sha="a" * 40)],
            decision=ReuseDecision(
                authorized=True,
                eligible=False,
                action="rerun",
                validation_sha="a" * 40,
                reasons=("gate policy changed since validation",),
            ),
            gate_runs=[{"name": "tests", "state": "success", "duration_seconds": 4.0}],
        )

        self.assertEqual(
            [(gate["action"], gate["reason_code"]) for gate in payload["gates"]],
            [("rerun", "identity_mismatch"), ("conditional_run", "identity_mismatch")],
        )
        savings = payload["estimated_savings"]
        self.assertEqual(savings["mode"], "unavailable")
        self.assertEqual(savings["seconds"], 0.0)
        self.assertEqual(savings["confidence"], "none")

    def test_an_unauthorized_decision_is_a_potential_saving(self) -> None:
        # The decision a runner returns when reuse is not enabled in config.
        config = replace(
            _config(),
            gates=(
                GateConfig(name="tests", run="pytest", paths=("src/**",)),
                GateConfig(name="smoke", run="scripts/smoke", always_rerun_on_deploy=True),
            ),
        )
        jobs = [Job(id=1, task="a", branch="feature/a", validation_sha="a" * 40)]

        payload = reuse_explanation(config, jobs, decision=unauthorized_reuse_decision(jobs))

        self.assertEqual(
            [(gate["action"], gate["reason_code"]) for gate in payload["gates"]],
            [
                ("potential_reuse", "authorization_required"),
                ("conditional_reuse", "authorization_and_preview_required"),
                ("rerun", "always_rerun_on_deploy"),
            ],
        )
        self.assertFalse(payload["authorized"])
        self.assertEqual(payload["estimated_savings"]["mode"], "potential")

    def test_the_estimate_counts_only_named_timed_successful_runs(self) -> None:
        config = _config()
        jobs = [Job(id=1, task="a", branch="feature/a", validation_sha="a" * 40)]
        decision = ReuseDecision(
            authorized=True,
            eligible=True,
            action="reuse",
            validation_sha="a" * 40,
            changed_paths=(),
        )

        untimed = reuse_explanation(
            config,
            jobs,
            decision=decision,
            gate_runs=[
                {"name": "diff-check", "state": "failed", "duration_seconds": 5.0},
                {"name": "diff-check", "state": "success"},
                {"name": "", "state": "success", "duration_seconds": 1.0},
            ],
        )
        self.assertIsNone(untimed["estimated_savings"]["seconds"])
        self.assertEqual(untimed["estimated_savings"]["confidence"], "none")
        self.assertEqual(untimed["estimated_savings"]["timed_gate_count"], 0)

        timed = reuse_explanation(
            config,
            jobs,
            decision=decision,
            gate_runs=[
                {"name": "diff-check", "state": state, "duration_seconds": 2.0}
                for state in ("success", "reused") * 5
            ],
        )
        self.assertEqual(timed["estimated_savings"]["seconds"], 2.0)
        self.assertEqual(timed["estimated_savings"]["sample_count"], 10)
        self.assertEqual(timed["estimated_savings"]["confidence"], "high")


_BASE = "b" * 40
_VALIDATION = "c" * 40
_TREE = "d" * 40
_ENVIRONMENT = "e" * 64
_UNSHARED = "one shared non-empty SHA"


class _FingerprintGates:
    """The two GateRunner calls decide() makes, answering from a fixture."""

    def __init__(self, environment: str | BaseException) -> None:
        self.environment = environment

    def environment_fingerprint(self, **_: Any) -> str:
        if isinstance(self.environment, BaseException):
            raise self.environment
        return self.environment

    def changed_paths(self, **_: Any) -> tuple[str, ...]:
        raise AssertionError("no configured gate is scoped to paths")


def _validated_train(config: MergetrainConfig) -> list[Job]:
    jobs = [
        Job(
            id=index,
            task=f"task-{index}",
            branch=f"feature/{index}",
            train_id="train-1",
            train_size=2,
            validated_at="2026-07-22T11:55:00Z",
            validation_base_sha=_BASE,
            validation_sha=_VALIDATION,
            validated_head_sha=str(index) * 40,
            validation_tree_sha=_TREE,
            validation_gate_policy_sha=gate_policy_sha(config),
            validation_environment_sha=_ENVIRONMENT,
        )
        for index in (1, 2)
    ]
    identity = train_identity_sha(jobs)
    return [replace(job, validation_train_sha=identity) for job in jobs]


def _replaced(
    checks: list[tuple[Any, ...]], *, drop: tuple[str, ...] = (), **changes: tuple[Any, ...]
) -> list[tuple[Any, ...]]:
    return [changes.get(check[0], check) for check in checks if check[0] not in drop]


class ValidationReuseDecideTests(unittest.TestCase):
    """Pin every identity check decide() records, in order, and its reasons."""

    maxDiff = None

    def setUp(self) -> None:
        self.config = _config()
        self.jobs = _validated_train(self.config)
        self.identity = self.jobs[0].validation_train_sha
        self.policy = gate_policy_sha(self.config)

    def _decide(
        self,
        jobs: list[Job],
        *,
        age: float = 5.0,
        commit_exists: bool = True,
        tree: str = _TREE,
        reset_code: int = 0,
        environment: str | BaseException = _ENVIRONMENT,
    ) -> ReuseDecision:
        def run_command(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            restore = command == ["git", "reset", "--hard", _VALIDATION]
            return subprocess.CompletedProcess(command, reset_code if restore else 0, "", "")

        with (
            patch("mergetrain.validation_reuse.validation_age_minutes", return_value=age),
            patch("mergetrain.validation_reuse.git_ref_exists", return_value=commit_exists),
            patch("mergetrain.validation_reuse.git_tree_sha", return_value=tree),
            patch("mergetrain.validation_reuse.run_command", side_effect=run_command),
        ):
            return ValidationReuse(self.config, _FingerprintGates(environment)).decide(
                jobs,
                worktree=Path("/x/wt/train"),
                integration_base_sha=_BASE,
                log=io.StringIO(),
                pulse=None,
            )

    @staticmethod
    def _pinned(decision: ReuseDecision) -> tuple[list[tuple[Any, ...]], list[str]]:
        return (
            [
                (check.code, check.status, check.expected, check.actual, check.detail)
                for check in decision.checks
            ],
            list(decision.reasons),
        )

    def _matching_checks(self) -> list[tuple[Any, ...]]:
        return [
            ("authorization", "match", True, True, "reuse was explicitly authorized"),
            (
                "train_membership",
                "match",
                "one non-empty train id",
                ["train-1"],
                "train membership is complete",
            ),
            ("train_size", "match", 2, [2], "validated train size matches membership"),
            (
                "validation_commit",
                "match",
                "one shared validation SHA",
                [_VALIDATION],
                "validated jobs share one validation SHA",
            ),
            (
                "integration_base",
                "match",
                _BASE,
                [_BASE],
                "integration ref still matches validation",
            ),
            (
                "train_identity",
                "match",
                self.identity,
                self.identity,
                "train membership identity matches validation",
            ),
            (
                "gate_policy",
                "match",
                self.policy,
                self.policy,
                "gate and fingerprint policy matches validation",
            ),
            (
                "validation_age",
                "match",
                {"maximum_minutes": 60},
                {"age_minutes": 5.0},
                "validation is within the configured reuse age",
            ),
            (
                "shared_validation_tree_sha",
                "match",
                _UNSHARED,
                [_TREE],
                "validated jobs share validation_tree_sha",
            ),
            (
                "shared_validation_gate_policy_sha",
                "match",
                _UNSHARED,
                [self.policy],
                "validated jobs share validation_gate_policy_sha",
            ),
            (
                "shared_validation_environment_sha",
                "match",
                _UNSHARED,
                [_ENVIRONMENT],
                "validated jobs share validation_environment_sha",
            ),
            (
                "shared_validation_train_sha",
                "match",
                _UNSHARED,
                [self.identity],
                "validated jobs share validation_train_sha",
            ),
            (
                "validation_commit_available",
                "match",
                True,
                True,
                "validation commit exists in the local repository",
            ),
            (
                "validation_tree",
                "match",
                _TREE,
                _TREE,
                "validation commit tree matches recorded identity",
            ),
            (
                "environment",
                "match",
                _ENVIRONMENT,
                _ENVIRONMENT,
                "environment fingerprint matches validation",
            ),
        ]

    def _skipped_environment(self, expected: str = _ENVIRONMENT) -> tuple[Any, ...]:
        return (
            "environment",
            "not_evaluated",
            expected,
            None,
            "environment check was skipped because an earlier identity check did not match",
        )

    def test_a_matching_train_passes_every_check(self) -> None:
        decision = self._decide(self.jobs)

        self.assertEqual(self._pinned(decision), (self._matching_checks(), []))
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.action, "reuse")
        self.assertEqual(decision.reused_validation_sha, _VALIDATION)
        self.assertEqual(decision.changed_paths, ())

    def test_an_empty_train_fails_membership_but_gives_no_identity_reasons(self) -> None:
        decision = self._decide([])

        shared_fields = (
            "validation_tree_sha",
            "validation_gate_policy_sha",
            "validation_environment_sha",
            "validation_train_sha",
        )
        self.assertEqual(
            self._pinned(decision),
            (
                [
                    ("authorization", "match", True, True, "reuse was explicitly authorized"),
                    (
                        "train_membership",
                        "mismatch",
                        "one non-empty train id",
                        [],
                        "train membership is incomplete or mixed",
                    ),
                    (
                        "train_size",
                        "mismatch",
                        0,
                        [],
                        "train size does not match its validated membership",
                    ),
                    (
                        "validation_commit",
                        "mismatch",
                        "one shared validation SHA",
                        [],
                        "validated jobs do not share one validation SHA",
                    ),
                    (
                        "integration_base",
                        "mismatch",
                        _BASE,
                        [],
                        "integration ref moved since validation",
                    ),
                    (
                        "train_identity",
                        "mismatch",
                        "",
                        "",
                        "train membership identity changed since validation",
                    ),
                    (
                        "gate_policy",
                        "mismatch",
                        "",
                        self.policy,
                        "gate or fingerprint policy changed since validation",
                    ),
                    (
                        "validation_age",
                        "mismatch",
                        {"maximum_minutes": 60},
                        {"age_minutes": None},
                        "validation is older than the configured reuse age",
                    ),
                    *(
                        (
                            f"shared_{field}",
                            "mismatch",
                            _UNSHARED,
                            [],
                            f"validated jobs lack one shared {field}",
                        )
                        for field in shared_fields
                    ),
                    (
                        "validation_commit_available",
                        "mismatch",
                        True,
                        False,
                        "validation commit is missing from the local repository",
                    ),
                    self._skipped_environment(expected=""),
                ],
                [
                    "train membership is incomplete or mixed",
                    "train size does not match its validated membership",
                    "validated jobs do not share one validation SHA",
                    "integration ref moved since validation",
                    *(f"validated jobs lack one shared {field}" for field in shared_fields),
                ],
            ),
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.action, "rerun")

    def test_a_mixed_train_reports_every_identity_mismatch(self) -> None:
        first, second = self.jobs
        jobs = [
            replace(
                first,
                train_size=3,
                validation_gate_policy_sha="0" * 64,
                validation_train_sha="",
            ),
            replace(
                second,
                train_id="train-2",
                train_size=3,
                validation_sha="f" * 40,
                validation_base_sha="a" * 40,
                validation_tree_sha="",
                validation_gate_policy_sha="0" * 64,
                validation_train_sha="",
            ),
        ]
        current_identity = train_identity_sha(jobs)

        decision = self._decide(jobs, age=float("inf"))

        self.assertEqual(
            self._pinned(decision),
            (
                [
                    ("authorization", "match", True, True, "reuse was explicitly authorized"),
                    (
                        "train_membership",
                        "mismatch",
                        "one non-empty train id",
                        ["train-1", "train-2"],
                        "train membership is incomplete or mixed",
                    ),
                    (
                        "train_size",
                        "mismatch",
                        2,
                        [3],
                        "train size does not match its validated membership",
                    ),
                    (
                        "validation_commit",
                        "mismatch",
                        "one shared validation SHA",
                        [_VALIDATION, "f" * 40],
                        "validated jobs do not share one validation SHA",
                    ),
                    (
                        "integration_base",
                        "mismatch",
                        _BASE,
                        ["a" * 40, _BASE],
                        "integration ref moved since validation",
                    ),
                    (
                        "train_identity",
                        "mismatch",
                        "",
                        current_identity,
                        "train membership identity changed since validation",
                    ),
                    (
                        "gate_policy",
                        "mismatch",
                        "0" * 64,
                        self.policy,
                        "gate or fingerprint policy changed since validation",
                    ),
                    (
                        "validation_age",
                        "mismatch",
                        {"maximum_minutes": 60},
                        {"age_minutes": None},
                        "validation is older than the configured reuse age",
                    ),
                    (
                        "shared_validation_tree_sha",
                        "mismatch",
                        _UNSHARED,
                        [_TREE],
                        "validated jobs lack one shared validation_tree_sha",
                    ),
                    (
                        "shared_validation_gate_policy_sha",
                        "match",
                        _UNSHARED,
                        ["0" * 64],
                        "validated jobs share validation_gate_policy_sha",
                    ),
                    (
                        "shared_validation_environment_sha",
                        "match",
                        _UNSHARED,
                        [_ENVIRONMENT],
                        "validated jobs share validation_environment_sha",
                    ),
                    (
                        "shared_validation_train_sha",
                        "mismatch",
                        _UNSHARED,
                        [],
                        "validated jobs lack one shared validation_train_sha",
                    ),
                    (
                        "validation_commit_available",
                        "mismatch",
                        True,
                        False,
                        "validation commit is missing from the local repository",
                    ),
                    self._skipped_environment(),
                ],
                [
                    "train membership is incomplete or mixed",
                    "train size does not match its validated membership",
                    "validated jobs do not share one validation SHA",
                    "integration ref moved since validation",
                    "train membership identity changed since validation",
                    "gate or fingerprint policy changed since validation",
                    "validation is older than the configured reuse age",
                    "validated jobs lack one shared validation_tree_sha",
                    "validated jobs lack one shared validation_train_sha",
                ],
            ),
        )

    def test_a_stale_validation_is_older_than_the_reuse_age(self) -> None:
        decision = self._decide(self.jobs, age=61.0)

        self.assertEqual(
            self._pinned(decision),
            (
                _replaced(
                    self._matching_checks(),
                    validation_age=(
                        "validation_age",
                        "mismatch",
                        {"maximum_minutes": 60},
                        {"age_minutes": 61.0},
                        "validation is older than the configured reuse age",
                    ),
                    environment=self._skipped_environment(),
                ),
                ["validation is older than the configured reuse age"],
            ),
        )

    def test_a_missing_validation_commit_skips_the_tree_check(self) -> None:
        decision = self._decide(self.jobs, commit_exists=False)

        self.assertEqual(
            self._pinned(decision),
            (
                _replaced(
                    self._matching_checks(),
                    drop=("validation_tree",),
                    validation_commit_available=(
                        "validation_commit_available",
                        "mismatch",
                        True,
                        False,
                        "validation commit is missing from the local repository",
                    ),
                    environment=self._skipped_environment(),
                ),
                ["validation commit is missing from the local repository"],
            ),
        )

    def test_a_changed_validation_tree_does_not_match_its_identity(self) -> None:
        decision = self._decide(self.jobs, tree="9" * 40)

        self.assertEqual(
            self._pinned(decision),
            (
                _replaced(
                    self._matching_checks(),
                    validation_tree=(
                        "validation_tree",
                        "mismatch",
                        _TREE,
                        "9" * 40,
                        "validation commit tree does not match its recorded identity",
                    ),
                    environment=self._skipped_environment(),
                ),
                ["validation commit tree does not match its recorded identity"],
            ),
        )

    def test_an_unrestorable_commit_leaves_the_environment_unevaluated(self) -> None:
        decision = self._decide(self.jobs, reset_code=1)

        self.assertEqual(
            self._pinned(decision),
            (
                _replaced(self._matching_checks(), environment=self._skipped_environment()),
                ["validation commit could not be restored for fingerprinting"],
            ),
        )

    def test_an_unreproducible_fingerprint_is_an_environment_mismatch(self) -> None:
        decision = self._decide(self.jobs, environment=CommandFailed("fingerprint", 1))

        self.assertEqual(
            self._pinned(decision),
            (
                _replaced(
                    self._matching_checks(),
                    environment=(
                        "environment",
                        "mismatch",
                        _ENVIRONMENT,
                        "unavailable",
                        "required environment fingerprint could not be reproduced",
                    ),
                ),
                ["required environment fingerprint could not be reproduced"],
            ),
        )

    def test_a_changed_fingerprint_is_an_environment_mismatch(self) -> None:
        decision = self._decide(self.jobs, environment="f" * 64)

        self.assertEqual(
            self._pinned(decision),
            (
                _replaced(
                    self._matching_checks(),
                    environment=(
                        "environment",
                        "mismatch",
                        _ENVIRONMENT,
                        "f" * 64,
                        "environment or toolchain fingerprint changed",
                    ),
                ),
                ["environment or toolchain fingerprint changed"],
            ),
        )


if __name__ == "__main__":
    unittest.main()
