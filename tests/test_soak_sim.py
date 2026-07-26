from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "mergetrain_soak_sim", ROOT / "scripts" / "soak_sim.py"
)
assert SPEC is not None
assert SPEC.loader is not None
SOAK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SOAK
SPEC.loader.exec_module(SOAK)


class RepositorySafetyTests(unittest.TestCase):
    def test_github_repo_slug_accepts_credential_free_https_and_ssh(self) -> None:
        cases = {
            "https://github.com/Owner/Target.git": "owner/target",
            "git@github.com:Owner/Target.git": "owner/target",
            "ssh://git@github.com/Owner/Target.git": "owner/target",
        }
        for origin, expected in cases.items():
            with self.subTest(origin=origin):
                self.assertEqual(SOAK.github_repo_slug(origin), expected)

    def test_github_repo_slug_rejects_credentials_local_and_non_github(self) -> None:
        origins = (
            "https://user:secret@github.com/owner/target.git",
            "/tmp/local-bare.git",
            "https://gitlab.com/owner/target.git",
            "https://github.com/owner/nested/target.git",
        )
        for origin in origins:
            with self.subTest(origin=origin):
                with self.assertRaises(SOAK.SoakError):
                    SOAK.github_repo_slug(origin)

    def test_product_repository_is_refused_before_fetch_or_reset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td).resolve()
            results = [
                subprocess.CompletedProcess([], 0, stdout=str(repo), stderr=""),
                subprocess.CompletedProcess(
                    [], 0, stdout="git@github.com:yongjip/mergetrain.git\n", stderr=""
                ),
            ]
            with mock.patch.object(SOAK, "git", side_effect=results) as git_mock:
                with self.assertRaisesRegex(SOAK.SoakError, "product repository"):
                    SOAK.validate_target_repository(
                        repo, confirmed_repository="yongjip/mergetrain"
                    )
            self.assertEqual(git_mock.call_count, 2)

    def test_sentinel_is_exact_and_evidence_cannot_dirty_target(self) -> None:
        expected = {
            "version": 1,
            "purpose": "mergetrain-soak-target",
            "repository": "owner/target",
        }
        SOAK.validate_sentinel(expected, repository="owner/target")
        with self.assertRaises(SOAK.SoakError):
            SOAK.validate_sentinel(
                {**expected, "extra": "not allowed"}, repository="owner/target"
            )

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td).resolve()
            SOAK.validate_evidence_path(
                repo, repo / ".mergetrain" / "soak-state.json"
            )
            SOAK.validate_evidence_path(repo, repo.parent / "soak-report.md")
            with self.assertRaisesRegex(SOAK.SoakError, "would dirty"):
                SOAK.validate_evidence_path(repo, repo / "soak-report.md")


class PersistentStateTests(unittest.TestCase):
    def test_first_run_requires_baseline_and_later_runs_reuse_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            with self.assertRaisesRegex(SOAK.SoakError, "requires.*--baseline"):
                SOAK.load_state(
                    state_path, repository="owner/target", baseline=""
                )

            state = SOAK.load_state(
                state_path,
                repository="owner/target",
                baseline="2026-07-26T10:56:04+09:00",
            )
            self.assertEqual(state["baseline"], "2026-07-26T01:56:04Z")
            self.assertRegex(state["namespace"], r"^[0-9a-f]{12}$")
            namespace = state["namespace"]
            first = SOAK.allocate_batch(state, state_path)

            resumed = SOAK.load_state(
                state_path,
                repository="owner/target",
                baseline="2026-07-26T01:56:04Z",
            )
            second = SOAK.allocate_batch(resumed, state_path)
            self.assertEqual((first, second), (1, 2))
            self.assertEqual(resumed["namespace"], namespace)

    def test_persisted_baseline_and_repository_cannot_be_reinterpreted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            SOAK.load_state(
                state_path,
                repository="owner/target",
                baseline="2026-07-26T01:56:04Z",
            )
            with self.assertRaisesRegex(SOAK.SoakError, "differs"):
                SOAK.load_state(
                    state_path,
                    repository="owner/target",
                    baseline="2026-07-27T01:56:04Z",
                )
            with self.assertRaisesRegex(SOAK.SoakError, "repository"):
                SOAK.load_state(
                    state_path,
                    repository="owner/other",
                    baseline="",
                )

    def test_stats_always_carries_the_persisted_lower_bound(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = SOAK.Logger(Path(td) / "log.jsonl")
            try:
                mt = SOAK.MT("mergetrain", Path(td), log)
                with mock.patch.object(
                    mt, "call", return_value={"ok": True}
                ) as call:
                    mt.stats(
                        scenario="loop", since="2026-07-26T01:56:04Z"
                    )
                call.assert_called_once_with(
                    "stats",
                    "--since",
                    "2026-07-26T01:56:04Z",
                    scenario="loop",
                )
            finally:
                log.close()

    def test_post_crash_verify_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = SOAK.Logger(Path(td) / "log.jsonl")
            try:
                mt = SOAK.MT("mergetrain", Path(td), log)
                with mock.patch.object(
                    mt,
                    "call",
                    return_value={"ok": True, "result": "failed"},
                ):
                    with self.assertRaisesRegex(
                        SOAK.SoakError, "post-reconcile verify failed"
                    ):
                        mt.verify(7, scenario="crash-verify")
            finally:
                log.close()


class ScenarioMutationTests(unittest.TestCase):
    def test_conflict_branches_preserve_behavior_while_editing_same_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            core = repo / "src" / "soaktarget" / "core.py"
            core.parent.mkdir(parents=True)
            original = (
                "def add(a: int, b: int) -> int:\n"
                "    return a + b\n"
            )
            core.write_text(original, encoding="utf-8")

            SOAK.write_conflicting_change("a")(repo)
            branch_a = core.read_text(encoding="utf-8")
            core.write_text(original, encoding="utf-8")
            SOAK.write_conflicting_change("b")(repo)
            branch_b = core.read_text(encoding="utf-8")

            self.assertNotEqual(branch_a, branch_b)
            for body in (branch_a, branch_b):
                namespace: dict[str, object] = {}
                exec(body, namespace)
                self.assertEqual(namespace["add"](1, 2), 3)


class EvidenceTests(unittest.TestCase):
    def _state(self, crash_status: str = "pending") -> dict[str, object]:
        return {
            "baseline": "2026-07-26T01:56:04Z",
            "namespace": "0123456789ab",
            "recovery_events": [{"kind": "gatefail"}],
            "crash_status": crash_status,
            "crash_queue_pending": False,
        }

    def _doctor(self) -> dict[str, object]:
        return {"lock": None, "counts": {}}

    def test_skip_crash_can_finish_smoke_without_falsifying_overall_soak(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = SOAK.Logger(Path(td) / "log.jsonl")
            try:
                log.event(
                    event="cli_call",
                    scenario="gatefail",
                    intervention={
                        "type": "retry",
                        "planned": True,
                        "classification": "expected",
                        "reason": "planned gate recovery",
                        "issue_url": "",
                    },
                )
                criteria = SOAK.evaluate_criteria(
                    {"trains": {"landed": 22}},
                    log,
                    self._state("pending"),
                    self._doctor(),
                    repository="owner/target",
                )
                self.assertFalse(criteria["crash_met"])
                self.assertFalse(criteria["complete"])
                self.assertTrue(
                    SOAK._session_goal_met(
                        criteria, target_landed=6, skip_crash=True
                    )
                )
                self.assertFalse(
                    SOAK._session_goal_met(
                        criteria, target_landed=20, skip_crash=False
                    )
                )
            finally:
                log.close()

    def test_intervention_ledger_reloads_redacts_and_requires_triage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "log.jsonl"
            first = SOAK.Logger(path)
            first.event(
                event="manual_intervention",
                scenario="operator",
                detail="https://user:secret@example.invalid/path",
                intervention={
                    "type": "manual_git_surgery",
                    "planned": False,
                    "classification": "bug",
                    "reason": "remote and queue disagreed",
                    "issue_url": "https://github.com/owner/target/issues/1",
                },
            )
            first.close()

            on_disk = path.read_text(encoding="utf-8")
            self.assertNotIn("secret", on_disk)
            second = SOAK.Logger(path)
            try:
                self.assertEqual(len(second.interventions), 1)
                self.assertEqual(second.untriaged_interventions(), [])
                second.event(
                    event="manual_intervention",
                    scenario="operator",
                    intervention={
                        "type": "unlock",
                        "planned": False,
                        "classification": "docs_gap",
                        "reason": "operator needed missing guidance",
                        "issue_url": "",
                    },
                )
                self.assertEqual(len(second.untriaged_interventions()), 1)
            finally:
                second.close()

    def test_report_marks_incomplete_crash_and_uses_credential_free_repo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log = SOAK.Logger(root / "log.jsonl")
            try:
                report = root / "report.md"
                criteria = SOAK.write_report(
                    report,
                    {"trains": {"landed": 22}},
                    log,
                    self._state("failed"),
                    self._doctor(),
                    repository="owner/target",
                    repo=root,
                    session_target=22,
                    skip_crash=False,
                )
                body = report.read_text(encoding="utf-8")
                self.assertIn("- [ ] one deliberate crash-recovery", body)
                self.assertIn("Overall soak complete: **NO**", body)
                self.assertIn("https://github.com/owner/target", body)
                self.assertFalse(criteria["complete"])
            finally:
                log.close()

    def test_manual_intervention_requires_classification_reason_and_issue(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = SOAK.Logger(Path(td) / "log.jsonl")
            try:
                incomplete = argparse.Namespace(
                    record_intervention="unlock",
                    classification="docs_gap",
                    reason="missing explanation",
                    issue_url="",
                )
                with self.assertRaisesRegex(SOAK.SoakError, "requires"):
                    SOAK._record_manual_intervention(incomplete, log)
                credentialed = argparse.Namespace(
                    record_intervention="unlock",
                    classification="bug",
                    reason="unexpected lock",
                    issue_url="https://user:secret@github.com/owner/target/issues/1",
                )
                with self.assertRaisesRegex(SOAK.SoakError, "credential-free"):
                    SOAK._record_manual_intervention(credentialed, log)
            finally:
                log.close()


if __name__ == "__main__":
    unittest.main()
