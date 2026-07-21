from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mergetrain import notify as notify_module
from mergetrain.notify import sweep_notifications, system_notifier


def outcome(path: str, result: str, **extra):
    return {"path": path, "name": Path(path).name, "outcome": result, **extra}


class SweepNotificationTests(unittest.TestCase):
    def test_processed_notifies_every_time_transitions_only_once(self) -> None:
        prev: dict[str, str] = {}
        first, prev = sweep_notifications(
            [
                outcome("/w/api", "processed:2"),
                outcome("/w/web", "error", error="repo directory is missing"),
                outcome("/w/idle", "idle"),
                outcome("/w/off", "excluded"),
            ],
            prev,
        )
        self.assertEqual(
            first,
            [
                ("mergetrain · api", "Train landed (2 jobs)"),
                ("mergetrain · web", "repo directory is missing"),
            ],
        )

        second, prev = sweep_notifications(
            [outcome("/w/api", "processed:1"), outcome("/w/web", "error", error="repo directory is missing")],
            prev,
        )
        # A landed train is new work every sweep; a still-broken repo is not.
        self.assertEqual(second, [("mergetrain · api", "Train landed (1 job)")])

        third, prev = sweep_notifications(
            [outcome("/w/api", "idle"), outcome("/w/web", "idle")],
            prev,
        )
        self.assertEqual(third, [])

        fourth, prev = sweep_notifications(
            [outcome("/w/web", "error", error="repo directory is missing")],
            prev,
        )
        # The error cleared (idle) and came back: that transition notifies again.
        self.assertEqual(fourth, [("mergetrain · web", "repo directory is missing")])

    def test_reconcile_pause_notifies_on_transition_only(self) -> None:
        prev: dict[str, str] = {}
        first, prev = sweep_notifications([outcome("/w/api", "reconcile_paused")], prev)
        self.assertEqual(first, [("mergetrain · api", "Deploy paused: jobs need reconcile")])
        second, prev = sweep_notifications([outcome("/w/api", "reconcile_paused")], prev)
        self.assertEqual(second, [])


class SystemNotifierTests(unittest.TestCase):
    def test_darwin_invokes_osascript_with_escaped_strings(self) -> None:
        calls = []
        with mock.patch.object(notify_module.sys, "platform", "darwin"), mock.patch.object(
            notify_module.shutil, "which", return_value="/usr/bin/osascript"
        ), mock.patch.object(notify_module.subprocess, "run", lambda *a, **k: calls.append(a[0])):
            system_notifier('mergetrain · "api"', 'landed \\ "ok"')
        self.assertEqual(len(calls), 1)
        script = calls[0][2]
        self.assertIn('with title "mergetrain · \\"api\\""', script)
        self.assertIn('display notification "landed \\\\ \\"ok\\""', script)

    def test_non_darwin_is_a_silent_noop(self) -> None:
        with mock.patch.object(notify_module.sys, "platform", "linux"), mock.patch.object(
            notify_module.subprocess, "run", side_effect=AssertionError("must not run")
        ):
            system_notifier("t", "m")


class HubDaemonNotifyIntegrationTests(unittest.TestCase):
    def test_once_sweep_delivers_notifications_to_injected_notifier(self) -> None:
        from mergetrain.config import load_config
        from mergetrain.hub_daemon import hub_daemon_loop
        from mergetrain.registry import add_repo
        from mergetrain.store import connect, enqueue_job

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            repo = root / "svc"
            repo.mkdir()
            (repo / ".mergetrain.yaml").write_text("project:\n  name: svc\n", encoding="utf-8")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                enqueue_job(conn, task="t", branch="agent/t", worktree_path=str(repo), auto_deploy=True)
            finally:
                conn.close()
            add_repo(repo, registry)

            received = []
            hub_daemon_loop(
                registry=str(registry),
                once=True,
                say=lambda _: None,
                install_signal_handlers=False,
                process_batch_factory=lambda config, owner: (lambda conn, jobs: None),
                notifier=lambda title, message: received.append((title, message)),
            )
            self.assertEqual(received, [("mergetrain · svc", "Train landed (1 job)")])


if __name__ == "__main__":
    unittest.main()
