from __future__ import annotations

import os
import shlex
import tempfile
import unittest
from pathlib import Path

from benchmarks.agent_adoption.agy_launcher import (
    _build_command,
    _configure_zsh_login_path,
)


class AgyLauncherTests(unittest.TestCase):
    def test_build_command_is_fresh_sandboxed_and_non_interactive(self) -> None:
        command = _build_command(
            executable="/usr/local/bin/agy",
            prompt="Fix the fixture.",
            task_repo="/tmp/run/task",
            control_repo="/tmp/run/control",
            model="gemini-3.1-pro-high",
            effort="high",
            print_timeout="10m",
        )

        self.assertEqual(command[:3], ["/usr/local/bin/agy", "--print", "Fix the fixture."])
        self.assertIn("--sandbox", command)
        self.assertIn("--new-project", command)
        self.assertIn("--disable-slash-commands", command)
        self.assertNotIn("--continue", command)
        self.assertNotIn("--conversation", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertEqual(command[command.index("--mode") + 1], "accept-edits")
        add_dirs = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--add-dir"
        ]
        self.assertEqual(add_dirs, ["/tmp/run/task", "/tmp/run/control"])
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")

    def test_zprofile_restores_wrapper_precedence_after_login_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary)
            wrapper_bin = run_root / "bin"
            trace = run_root / "artifacts" / "trace.jsonl"
            wrapper_bin.mkdir()
            trace.parent.mkdir()
            environment = {
                "PATH": os.pathsep.join((str(wrapper_bin), "/opt/homebrew/bin", "/usr/bin")),
                "MERGETRAIN_BENCHMARK_TRACE": str(trace),
            }

            _configure_zsh_login_path(environment)

            profile_dir = Path(environment["ZDOTDIR"])
            self.assertEqual(profile_dir, run_root.resolve() / "agy-zdotdir")
            self.assertEqual(
                (profile_dir / ".zprofile").read_text(encoding="utf-8"),
                f'export PATH={shlex.quote(str(wrapper_bin.resolve()))}:"$PATH"\n',
            )


if __name__ == "__main__":
    unittest.main()
