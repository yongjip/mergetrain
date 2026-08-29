from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from benchmarks.agent_adoption.codex_launcher import _configure_zsh_login_path


class CodexLauncherTests(unittest.TestCase):
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
            self.assertEqual(profile_dir, run_root.resolve() / "codex-zdotdir")
            self.assertEqual(
                (profile_dir / ".zprofile").read_text(encoding="utf-8"),
                f'export PATH={wrapper_bin.resolve()}:"$PATH"\n',
            )


if __name__ == "__main__":
    unittest.main()
