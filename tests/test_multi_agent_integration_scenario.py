from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from benchmarks.multi_agent_integration.scenario import (
    ScenarioError,
    evaluate_scenario,
    prepare_scenario,
)

ROOT = Path(__file__).resolve().parents[1]

PROMO_TEST = '''from decimal import Decimal
import unittest
from checkout import discount_amount


class PromoTests(unittest.TestCase):
    def test_discount_is_fifteen_at_threshold(self) -> None:
        self.assertEqual(discount_amount(Decimal("100")), Decimal("15.00"))
'''

SHIPPING_TEST = '''from decimal import Decimal
import unittest
from checkout import shipping_fee


class ShippingTests(unittest.TestCase):
    def test_shipping_is_free(self) -> None:
        self.assertEqual(shipping_fee(Decimal("100")), Decimal("0.00"))
'''

REFERENCE_TEST = '''import unittest
from checkout import format_order_reference


class ReferenceTests(unittest.TestCase):
    def test_reference_is_padded(self) -> None:
        self.assertEqual(format_order_reference(42), "ORD-000042")
'''


class MultiAgentIntegrationScenarioTests(unittest.TestCase):
    def _launcher(self, root: Path) -> tuple[str, str]:
        launcher = root / "mergetrain_under_test.py"
        launcher.write_text(
            f"""import sys
sys.path.insert(0, {str(ROOT / 'src')!r})
from mergetrain.cli import main
raise SystemExit(main())
""",
            encoding="utf-8",
        )
        return sys.executable, str(launcher)

    def _run(self, command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=True)

    def _commit_and_enqueue(
        self,
        *,
        worktree: Path,
        launcher: tuple[str, str],
        branch: str,
        task: str,
    ) -> None:
        self._run(["git", "add", "."], cwd=worktree)
        self._run(["git", "commit", "-m", f"test: complete {task}"], cwd=worktree)
        self._run([*launcher, "doctor", "--json"], cwd=worktree)
        self._run(
            [
                *launcher,
                "enqueue",
                "--task",
                task,
                "--branch",
                branch,
                "--capture-sha",
                "--json",
            ],
            cwd=worktree,
        )

    def test_prepare_requires_an_absent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ScenarioError):
                prepare_scenario(root, mergetrain_command=["mergetrain"])

    def test_three_clean_handoffs_produce_the_expected_pair_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = self._launcher(root)
            run_dir = root / "run"
            manifest = prepare_scenario(run_dir, mergetrain_command=launcher)
            tasks = manifest["tasks"]

            promo = Path(tasks["promo"]["worktree"])
            promo_source = (promo / "checkout.py").read_text(encoding="utf-8")
            (promo / "checkout.py").write_text(
                promo_source.replace('return _money("10") if', 'return _money("15") if'),
                encoding="utf-8",
            )
            (promo / "tests" / "test_promo_discount.py").write_text(
                PROMO_TEST, encoding="utf-8"
            )
            self._commit_and_enqueue(
                worktree=promo,
                launcher=launcher,
                branch=tasks["promo"]["branch"],
                task=tasks["promo"]["task"],
            )

            shipping = Path(tasks["shipping"]["worktree"])
            shipping_source = (shipping / "checkout.py").read_text(encoding="utf-8")
            (shipping / "checkout.py").write_text(
                shipping_source.replace('return _money("5")', 'return _money("0")'),
                encoding="utf-8",
            )
            (shipping / "tests" / "test_free_shipping.py").write_text(
                SHIPPING_TEST, encoding="utf-8"
            )
            self._commit_and_enqueue(
                worktree=shipping,
                launcher=launcher,
                branch=tasks["shipping"]["branch"],
                task=tasks["shipping"]["task"],
            )

            reference = Path(tasks["reference"]["worktree"])
            reference_source = (reference / "checkout.py").read_text(encoding="utf-8")
            addition = '''\n\ndef format_order_reference(order_id: int) -> str:\n    if not 1 <= order_id <= 999999:\n        raise ValueError("order_id must be between 1 and 999999")\n    return f"ORD-{order_id:06d}"\n'''
            (reference / "checkout.py").write_text(
                reference_source + addition, encoding="utf-8"
            )
            (reference / "tests" / "test_order_reference.py").write_text(
                REFERENCE_TEST, encoding="utf-8"
            )
            self._commit_and_enqueue(
                worktree=reference,
                launcher=launcher,
                branch=tasks["reference"]["branch"],
                task=tasks["reference"]["task"],
            )

            result = evaluate_scenario(run_dir)

            self.assertTrue(result["ok"], json.dumps(result, indent=2))
            self.assertTrue(result["remote_integration_unchanged"])
            self.assertTrue(result["pairs"]["promo+reference"]["tests_pass"])
            self.assertTrue(result["pairs"]["shipping+reference"]["tests_pass"])
            semantic_pair = result["pairs"]["promo+shipping"]
            self.assertFalse(semantic_pair["tests_pass"])
            self.assertTrue(semantic_pair["semantic_floor_failure"])
            for task in result["tasks"].values():
                self.assertTrue(task["exact_sha_handoff"])
                self.assertTrue(task["clean"])
                self.assertTrue(task["tests_pass"])


if __name__ == "__main__":
    unittest.main()
