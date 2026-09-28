from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from mergetrain.errors import AmbiguousPush, MergetrainError
from mergetrain.push_liveness import (
    holding_push_lock,
    push_in_flight,
    push_job_name,
    push_lock_path,
)

SHA = "0123456789abcdef0123456789abcdef01234567"


def fake_config(root: Path) -> Any:
    return SimpleNamespace(state=SimpleNamespace(db=root / ".mergetrain" / "queue.sqlite"))


class PushLockNamingTests(unittest.TestCase):
    def test_lock_and_job_names_are_derived_from_the_commit(self) -> None:
        config = fake_config(Path("/state"))
        self.assertEqual(
            push_lock_path(config, SHA), Path("/state/.mergetrain/push-locks") / f"{SHA}.lock"
        )
        self.assertEqual(push_job_name(SHA), f"Local\\mergetrain-push-{SHA}")

    def test_no_marker_means_no_push_in_flight(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = fake_config(Path(td))
            self.assertFalse(push_in_flight(config, ""))
            self.assertFalse(push_in_flight(config, SHA))


@unittest.skipUnless(os.name == "posix", "the POSIX push lock is an inherited flock")
class PushLockTests(unittest.TestCase):
    def test_the_lock_is_held_during_the_push_and_removed_after(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = fake_config(Path(td))
            with holding_push_lock(config, SHA) as inherited:
                self.assertEqual(len(inherited), 1)
                self.assertTrue(push_in_flight(config, SHA))
                self.assertTrue(push_lock_path(config, SHA).exists())
            self.assertFalse(push_in_flight(config, SHA))
            self.assertFalse(push_lock_path(config, SHA).exists())

    def test_a_process_that_inherited_the_lock_keeps_the_push_in_flight(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = fake_config(Path(td))
            release = Path(td) / "release"
            waiter = (
                "import os, sys, time\n"
                "while not os.path.exists(sys.argv[1]):\n"
                "    time.sleep(0.02)\n"
            )
            with holding_push_lock(config, SHA) as inherited:
                child = subprocess.Popen(
                    [sys.executable, "-c", waiter, str(release)], pass_fds=inherited
                )
            try:
                # The runner let go, but a process of the push is still alive.
                self.assertTrue(push_in_flight(config, SHA))
                self.assertTrue(push_lock_path(config, SHA).exists())
            finally:
                release.touch()
                child.wait(timeout=30)
            self.assertFalse(push_in_flight(config, SHA))
            self.assertFalse(push_lock_path(config, SHA).exists())

    def test_a_lock_that_cannot_be_taken_means_the_push_is_not_attempted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = fake_config(Path(td))
            with (
                mock.patch("mergetrain.push_liveness.os.open", side_effect=PermissionError("denied")),
                self.assertRaisesRegex(MergetrainError, "push was not attempted"),
            ):
                with holding_push_lock(config, SHA):
                    self.fail("the push must not start without its lock")

    def test_a_second_push_of_a_commit_still_being_pushed_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = fake_config(Path(td))
            with holding_push_lock(config, SHA):
                with self.assertRaisesRegex(AmbiguousPush, "still running"):
                    with holding_push_lock(config, SHA):
                        self.fail("the second push must not start")
                # The refusal left the first push's lock alone.
                self.assertTrue(push_in_flight(config, SHA))
            self.assertFalse(push_in_flight(config, SHA))


if __name__ == "__main__":
    unittest.main()
