"""Pin the approval and plan hashes to their released values.

An ``--auto`` job stores ``deploy_execution_policy_sha`` when it is enqueued,
and a confirmed deploy carries a ``deploy_plan_sha``. Both are recomputed and
compared later, so any change to what they hash, even a refactor that means to
change nothing, invalidates approvals given before an upgrade. These values
were computed by mergetrain 3.1.1.
"""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mergetrain.config import load_config
from mergetrain.deploy_plan import deploy_execution_policy_sha, deploy_plan_sha

CONFIG = """version: 2
project:
  name: golden
git:
  remote: origin
  integration_branch: main
  push_refs:
    - main
gates:
  - name: tests
    run: python -m pytest -q
deploy:
  verify:
    - name: ci
      run: scripts/verify-ci.sh
"""

JOBS = [
    SimpleNamespace(
        id=7, task="a", branch="agent/a", train_id="t" * 32, train_size=2,
        validated_head_sha="a" * 40,
    ),
    SimpleNamespace(
        id=8, task="b", branch="agent/b", train_id="t" * 32, train_size=2,
        validated_head_sha="b" * 40,
    ),
]
DESTINATION = SimpleNamespace(destination_sha="d" * 64)

EXECUTION_POLICY_SHA = "3e929322c3029c763b8b4549f4557fd0df9b40e4d8f0a9b85cc14a488bbbeed2"
EXECUTION_POLICY_WITH_REUSE_SHA = (
    "7ab9d147c4c8bf85fe5dd1eb699ec07622c4358e968b32c36f46773d89c5de41"
)
PLAN_SHA = "2969544a2bd7e4b4dfbc6f48b3f0645ceec155ef64e8364b9be67a326a2410b0"


class PolicyHashGoldenTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / ".mergetrain.yaml").write_text(CONFIG, encoding="utf-8")
        self.config = load_config(repo=root)

    def test_execution_policy_hash_is_unchanged(self) -> None:
        self.assertEqual(deploy_execution_policy_sha(self.config), EXECUTION_POLICY_SHA)

    def test_execution_policy_hash_with_reuse_enabled_is_unchanged(self) -> None:
        deploy = self.config.deploy
        config = dataclasses.replace(
            self.config,
            deploy=dataclasses.replace(
                deploy, reuse=dataclasses.replace(deploy.reuse, enabled=True)
            ),
        )
        self.assertEqual(deploy_execution_policy_sha(config), EXECUTION_POLICY_WITH_REUSE_SHA)

    def test_plan_hash_is_unchanged(self) -> None:
        plan = deploy_plan_sha(self.config, JOBS, destination=DESTINATION)  # type: ignore[arg-type]
        self.assertEqual(plan, PLAN_SHA)


if __name__ == "__main__":
    unittest.main()
