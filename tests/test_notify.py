from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mergetrain import notify as notify_module
from mergetrain.notify import (
    load_notify_state,
    save_notify_state,
    sweep_notifications,
    system_notifier,
)


def outcome(path: str, result: str, **extra):
    return {"path": path, "name": Path(path).name, "outcome": result, **extra}


def deliver(outcomes, prev, *, fail_paths=()):
    """Mimic the loop: commit settled immediately, message keys on success."""
    messages, settled = sweep_notifications(outcomes, prev)
    next_state = dict(settled)
    delivered = []
    for path, key, title, body in messages:
        if path in fail_paths:
            continue  # delivery failed; do NOT commit the key
        next_state[path] = key
        delivered.append((title, body))
    return delivered, next_state


class SweepNotificationTests(unittest.TestCase):
    def test_processed_notifies_every_time_transitions_only_once(self) -> None:
        prev: dict[str, str] = {}
        first, prev = deliver(
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

        second, prev = deliver(
            [outcome("/w/api", "processed:1"), outcome("/w/web", "error", error="repo directory is missing")],
            prev,
        )
        # A landed train is new work every sweep; a still-broken repo is not.
        self.assertEqual(second, [("mergetrain · api", "Train landed (1 job)")])

        third, prev = deliver(
            [outcome("/w/api", "idle"), outcome("/w/web", "idle")],
            prev,
        )
        self.assertEqual(third, [])

        fourth, prev = deliver(
            [outcome("/w/web", "error", error="repo directory is missing")],
            prev,
        )
        # The error cleared (idle) and came back: that transition notifies again.
        self.assertEqual(fourth, [("mergetrain · web", "repo directory is missing")])

    def test_landing_grades_are_honest_and_no_landing_dedups(self) -> None:
        prev: dict[str, str] = {}
        msgs, prev = deliver(
            [
                outcome("/w/a", "landed:2"),
                outcome("/w/b", "partial:1/3"),
                outcome("/w/c", "no_landing:2"),
            ],
            prev,
        )
        self.assertEqual(
            msgs,
            [
                ("mergetrain · a", "Train landed (2 jobs)"),
                ("mergetrain · b", "Partial: 1/3 landed, rest blocked/failed"),
                ("mergetrain · c", "Nothing landed — 2 jobs blocked or failed"),
            ],
        )
        # A repo that keeps landing nothing is a persistent state — notify once.
        again, prev = deliver([outcome("/w/c", "no_landing:2")], prev)
        self.assertEqual(again, [])

    def test_reconcile_pause_notifies_on_transition_only(self) -> None:
        prev: dict[str, str] = {}
        first, prev = deliver([outcome("/w/api", "reconcile_paused")], prev)
        self.assertEqual(first, [("mergetrain · api", "Deploy paused: jobs need reconcile")])
        second, prev = deliver([outcome("/w/api", "reconcile_paused")], prev)
        self.assertEqual(second, [])

    def test_failed_delivery_is_retried_not_consumed(self) -> None:
        broken = [outcome("/w/web", "error", error="repo directory is missing")]
        # First delivery fails: the transition must NOT be marked as notified.
        _, prev = deliver(broken, {}, fail_paths={"/w/web"})
        second, prev = deliver(broken, prev)
        self.assertEqual(second, [("mergetrain · web", "repo directory is missing")])
        # Now that it delivered, the unchanged error goes quiet.
        third, prev = deliver(broken, prev)
        self.assertEqual(third, [])

    def test_changed_error_text_is_a_new_transition(self) -> None:
        prev: dict[str, str] = {}
        _, prev = deliver([outcome("/w/web", "error", error="disk full")], prev)
        second, prev = deliver([outcome("/w/web", "error", error="permission denied")], prev)
        # A materially different failure is a genuine transition, not the
        # "same broken repo" the dedup is meant to silence.
        self.assertEqual(second, [("mergetrain · web", "permission denied")])
        third, prev = deliver([outcome("/w/web", "error", error="permission denied")], prev)
        self.assertEqual(third, [])

    def test_state_round_trips_through_disk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = str(Path(td) / "repos.json")
            self.assertEqual(load_notify_state(registry), {})
            _, state = deliver([outcome("/w/web", "error", error="boom")], {})
            save_notify_state(state, registry)
            # A fresh process (--once/cron) resumes dedup instead of re-firing.
            resumed = load_notify_state(registry)
            quiet, _ = deliver([outcome("/w/web", "error", error="boom")], resumed)
            self.assertEqual(quiet, [])


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
