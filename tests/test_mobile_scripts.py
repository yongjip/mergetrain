from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


class MobileScriptIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(__file__).resolve().parents[1]
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calls = self.root / "calls.jsonl"
        self.binary = self.root / "fake-mergetrain"
        self.binary.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json
                import pathlib
                import sys

                args = sys.argv[1:]
                with pathlib.Path({str(self.calls)!r}).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(args) + "\\n")
                if args and args[0] == "doctor":
                    payload = {{
                        "ok": True,
                        "git": {{"repo_root": "/repo", "integration_ref": "origin/main", "integration_ref_exists": True}},
                        "counts": {{"validated": 1}},
                        "lock": None,
                        "next_action": "deploy_validated_train",
                    }}
                elif args and args[0] == "status":
                    payload = {{"ok": True, "validated_trains": [], "jobs": []}}
                elif "--preview" in args:
                    payload = {{
                        "ok": True,
                        "preview": True,
                        "train_id": "train-1",
                        "deploy_plan_sha": "a" * 64,
                        "confirmed_command": "mergetrain run-batch --deploy --train-id train-1 --expected-plan " + "a" * 64,
                        "push_plan": {{
                            "remote": "origin",
                            "url": "ssh://git@example.invalid/repo",
                            "refs": [{{"spec": "HEAD:main"}}],
                        }},
                        "jobs": [{{
                            "id": 1,
                            "task": "safe patch",
                            "branch": "feature/safe",
                            "validated_head_sha": "b" * 40,
                        }}],
                    }}
                elif "--validate-only" in args:
                    payload = {{
                        "ok": True,
                        "jobs": [{{"id": 1, "status": "validated", "branch": "feature/safe", "note": "ok", "train_id": "train-1"}}],
                    }}
                else:
                    payload = {{
                        "ok": True,
                        "jobs": [{{"id": 1, "status": "deployed", "branch": "feature/safe", "deploy_sha": "c" * 40, "note": "ok"}}],
                    }}
                print(json.dumps(payload))
                """
            ),
            encoding="utf-8",
        )
        self.binary.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_script(self, name: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["MERGETRAIN_BIN"] = str(self.binary)
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / name), *args],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def recorded_calls(self) -> list[list[str]]:
        return [
            json.loads(line)
            for line in self.calls.read_text(encoding="utf-8").splitlines()
        ]

    def test_deploy_dry_run_uses_only_the_canonical_preview(self) -> None:
        completed = self.run_script("mt-deploy.sh")

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("Canonical deploy preview", completed.stdout)
        self.assertIn("deploy plan: " + "a" * 64, completed.stdout)
        self.assertEqual(len(self.recorded_calls()), 1)
        self.assertIn("--preview", self.recorded_calls()[0])

    def test_deploy_confirmation_carries_the_preview_hash(self) -> None:
        completed = self.run_script("mt-deploy.sh", "--confirm")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.recorded_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("--preview", calls[0])
        self.assertNotIn("--preview", calls[1])
        self.assertEqual(calls[1][calls[1].index("--expected-plan") + 1], "a" * 64)
        self.assertEqual(calls[1][calls[1].index("--train-id") + 1], "train-1")

    def test_status_and_validate_wrappers_execute_without_legacy_vocabulary(self) -> None:
        status = self.run_script("mt-status.sh")
        validate = self.run_script("mt-validate.sh")

        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("next_action: deploy_validated_train", status.stdout)
        self.assertEqual(validate.returncode, 0, validate.stderr)
        self.assertIn("validated train: train-1", validate.stdout)


if __name__ == "__main__":
    unittest.main()
