from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


@unittest.skipIf(os.name == "nt", "mobile convenience scripts require a POSIX shell")
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
                if args and args[0] == "status":
                    payload = {{
                        "ok": True,
                        "health": "healthy",
                        "state": "ready",
                        "summary": "1 job is ready to deploy",
                        "next_action": {{"code": "deploy_when_approved", "command": "mergetrain deploy", "requires_approval": "deploy"}},
                    }}
                elif args and args[0] == "validate":
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
        # Invoke the fake through the active interpreter. Git Bash on Windows
        # does not reliably execute an extensionless temporary shebang file.
        env["MERGETRAIN_BIN"] = f"{sys.executable} {self.binary}"
        return subprocess.run(
            ["bash", str(self.repo / "scripts" / name), *args],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def recorded_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]

    def test_deploy_wrapper_delegates_to_the_canonical_command(self) -> None:
        completed = self.run_script("mt-deploy.sh")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.recorded_calls(), [["deploy"]])

    def test_deploy_wrapper_preserves_global_options(self) -> None:
        completed = self.run_script("mt-deploy.sh", "--repo", "/repo")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.recorded_calls(), [["deploy", "--repo", "/repo"]])

    def test_status_and_validate_wrappers_execute_without_legacy_vocabulary(self) -> None:
        status = self.run_script("mt-status.sh")
        validate = self.run_script("mt-validate.sh")

        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(self.recorded_calls()[0], ["status"])
        self.assertEqual(validate.returncode, 0, validate.stderr)
        self.assertEqual(self.recorded_calls()[1], ["validate"])


if __name__ == "__main__":
    unittest.main()
