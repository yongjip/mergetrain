from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from mergetrain.cli import main
from mergetrain.config import load_config
from mergetrain.errors import QueueError
from mergetrain.hub import build_hub_snapshot, build_hub_summary
from mergetrain.persistence.connection import connect
from mergetrain.persistence.jobs import enqueue_job
from mergetrain.registry import add_repo, load_registry


def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    (repo / ".mergetrain.yaml").write_text(f"project:\n  name: {name}\n", encoding="utf-8")
    return repo


def seed_queue(repo: Path) -> None:
    config = load_config(repo=repo)
    conn = connect(config.state.db)
    try:
        enqueue_job(conn, task="seed", branch="agent/seed", worktree_path=str(repo))
    finally:
        conn.close()


class HubSnapshotTests(unittest.TestCase):
    def test_aggregates_live_empty_and_broken_repos_in_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            live = make_repo(root, "live")
            seed_queue(live)
            empty = make_repo(root, "empty")
            gone = make_repo(root, "gone")
            for repo in (live, empty, gone):
                add_repo(repo, registry)
            (gone / ".mergetrain.yaml").unlink()
            gone.rmdir()

            snapshot = build_hub_snapshot(load_registry(registry))

            self.assertTrue(snapshot["hub"])
            self.assertEqual(snapshot["repo_count"], 3)
            by_name = {entry.get("name", entry["path"]): entry for entry in snapshot["repos"]}
            live_entry = by_name["live"]
            self.assertTrue(live_entry["ok"])
            self.assertEqual(live_entry["snapshot"]["counts"]["queued"], 1)
            self.assertEqual(live_entry["snapshot"]["project"]["name"], "live")
            empty_entry = by_name["empty"]
            self.assertTrue(empty_entry["ok"])
            self.assertTrue(empty_entry["empty"])
            self.assertNotIn("snapshot", empty_entry)
            broken = [entry for entry in snapshot["repos"] if not entry["ok"]]
            self.assertEqual(len(broken), 1)
            self.assertIn("missing", broken[0]["error"])

    def test_config_problems_stay_isolated_to_their_repo_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            live = make_repo(root, "live")
            seed_queue(live)
            unconfigured = make_repo(root, "unconfigured")
            broken = make_repo(root, "broken")
            for repo in (live, unconfigured, broken):
                add_repo(repo, registry)
            (unconfigured / ".mergetrain.yaml").unlink()
            (broken / ".mergetrain.yaml").write_text("gates: [\n", encoding="utf-8")

            for build in (build_hub_snapshot, build_hub_summary):
                with self.subTest(build=build.__name__):
                    entries = {
                        Path(entry["path"]).name: entry
                        for entry in build(load_registry(registry))["repos"]
                    }
                    self.assertTrue(entries["live"]["ok"])
                    self.assertFalse(entries["unconfigured"]["ok"])
                    self.assertIn("no .mergetrain.yaml", entries["unconfigured"]["error"])
                    self.assertFalse(entries["broken"]["ok"])
                    self.assertTrue(entries["broken"]["error"])

    def test_observing_never_creates_or_migrates_repo_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            empty = make_repo(root, "empty")
            add_repo(empty, registry)

            build_hub_snapshot(load_registry(registry))

            # The read-only contract: peeking at a repo with no queue must not
            # scaffold .mergetrain/ inside it.
            self.assertFalse((empty / ".mergetrain").exists())

    def test_future_config_reports_the_upgrade_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            live = make_repo(root, "future")
            seed_queue(live)
            (live / ".mergetrain.yaml").write_text(
                "version: 999\nproject:\n  name: future\n", encoding="utf-8"
            )
            add_repo(live, registry)

            snapshot = build_hub_snapshot(load_registry(registry))

            self.assertEqual(
                snapshot["repos"][0]["snapshot"]["next_action"],
                "upgrade_mergetrain",
            )

    def test_next_action_weighs_git_readiness_like_status(self) -> None:
        # status recommends configuring the remote before any queue work, but
        # both hub views, which never looked at Git, said to validate the queue.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            repo = make_repo(root, "no-remote")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            seed_queue(repo)
            add_repo(repo, registry)
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--repo", str(repo), "status", "--json"])
            status = json.loads(out.getvalue())["next_action"]["code"]
            full = build_hub_snapshot(load_registry(registry))["repos"][0]
            summary = build_hub_summary(load_registry(registry))["repos"][0]

        self.assertEqual(status, "configure_git_remote")
        self.assertEqual(full["snapshot"]["next_action"], status)
        self.assertEqual(summary["summary"]["next_action"], status)

    def test_daemon_flag_comes_from_the_registry_on_every_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            live = make_repo(root, "live")
            seed_queue(live)
            add_repo(live, registry)

            self.assertTrue(build_hub_snapshot(load_registry(registry))["repos"][0]["daemon"])
            add_repo(live, registry, daemon=False)  # registry-only change
            self.assertFalse(build_hub_snapshot(load_registry(registry))["repos"][0]["daemon"])

    def test_summary_reads_queue_truth_without_building_full_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            live = make_repo(root, "live")
            seed_queue(live)
            empty = make_repo(root, "empty")
            for repo in (live, empty):
                add_repo(repo, registry)

            with patch(
                "mergetrain.hub.build_repo_snapshot",
                side_effect=AssertionError("full repo snapshot was built"),
            ):
                summary = build_hub_summary(load_registry(registry))

            self.assertEqual(summary["view"], "summary")
            by_name = {entry.get("name"): entry for entry in summary["repos"]}
            self.assertEqual(by_name["live"]["summary"]["counts"]["queued"], 1)
            # This repo is no Git repository, which status names first too.
            self.assertEqual(
                by_name["live"]["summary"]["next_action"],
                "open_git_repository",
            )
            self.assertNotIn("snapshot", by_name["live"])
            self.assertTrue(by_name["empty"]["empty"])
            self.assertFalse((empty / ".mergetrain").exists())

    def test_read_only_connect_refuses_missing_db_and_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaises(QueueError):
                connect(root / "absent.sqlite", read_only=True)
            repo = make_repo(root, "svc")
            seed_queue(repo)
            config = load_config(repo=repo)
            conn = connect(config.state.db, read_only=True)
            try:
                rows = conn.execute("SELECT COUNT(*) AS n FROM deploy_queue").fetchone()
                self.assertEqual(int(rows["n"]), 1)
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("DELETE FROM deploy_queue")
            finally:
                conn.close()

    def test_read_only_connect_bootstraps_missing_idle_wal_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            writer = connect(db)
            try:
                enqueue_job(writer, task="seed", branch="agent/seed")
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                writer.close()

            # Model an idle WAL queue after its last connection cleaned up.
            # The checkpoint makes removing these test-only sidecars safe.
            for suffix in ("-wal", "-shm"):
                Path(f"{db}{suffix}").unlink(missing_ok=True)

            observer = connect(db, read_only=True)
            try:
                row = observer.execute("SELECT COUNT(*) AS n FROM deploy_queue").fetchone()
                self.assertEqual(int(row["n"]), 1)
                with self.assertRaises(sqlite3.OperationalError):
                    observer.execute("DELETE FROM deploy_queue")
            finally:
                observer.close()

    def test_read_only_connect_does_not_mask_unrelated_sqlite_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            db.touch()
            observer = MagicMock()
            observer.execute.side_effect = sqlite3.OperationalError("disk I/O error")

            with patch(
                "mergetrain.persistence.connection.sqlite3.connect",
                return_value=observer,
            ):
                with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
                    connect(db, read_only=True)

            observer.close.assert_called_once_with()

    def test_read_only_connect_reports_bootstrap_failure_and_closes_handles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "queue.sqlite"
            db.touch()
            first_observer = MagicMock()
            first_observer.execute.side_effect = [
                None,
                sqlite3.OperationalError("unable to open database file"),
            ]
            bootstrap = MagicMock()
            final_observer = MagicMock()
            final_observer.execute.side_effect = [
                None,
                sqlite3.OperationalError("unable to open database file"),
            ]

            with patch(
                "mergetrain.persistence.connection.sqlite3.connect",
                side_effect=[first_observer, bootstrap, final_observer],
            ):
                with self.assertRaisesRegex(QueueError, "idle WAL database"):
                    connect(db, read_only=True)

            first_observer.close.assert_called_once_with()
            final_observer.close.assert_called_once_with()
            bootstrap.close.assert_called_once_with()


class HubStatusCliTests(unittest.TestCase):
    def test_hub_status_json_reports_every_registered_repo(self) -> None:
        import contextlib
        import io

        from mergetrain.cli import main

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            live = make_repo(root, "live")
            seed_queue(live)
            empty = make_repo(root, "empty")
            for repo in (live, empty):
                add_repo(repo, registry)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["hub", "status", "--registry", str(registry), "--json"])

            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["hub"])
            by_name = {entry.get("name"): entry for entry in payload["repos"]}
            self.assertEqual(by_name["live"]["snapshot"]["counts"]["queued"], 1)
            self.assertTrue(by_name["empty"]["empty"])

    def test_hub_status_human_lines_cover_all_states(self) -> None:
        import contextlib
        import io

        from mergetrain.cli import main

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            live = make_repo(root, "live")
            seed_queue(live)
            gone = make_repo(root, "gone")
            for repo in (live, gone):
                add_repo(repo, registry)
            (gone / ".mergetrain.yaml").unlink()
            gone.rmdir()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["hub", "status", "--registry", str(registry)])

            self.assertEqual(code, 0)
            lines = stdout.getvalue().splitlines()
            # This repo is no Git repository, which status names first too.
            self.assertIn("live: queued=1 | next: open_git_repository", lines)
            self.assertTrue(any("gone" in line and "ERROR" in line for line in lines))

    def test_hub_status_summary_json_omits_full_snapshots(self) -> None:
        import contextlib
        import io

        from mergetrain.cli import main

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            live = make_repo(root, "live")
            seed_queue(live)
            add_repo(live, registry)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "hub",
                        "status",
                        "--registry",
                        str(registry),
                        "--summary",
                        "--json",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            entry = payload["repos"][0]
            self.assertEqual(code, 0)
            self.assertEqual(payload["view"], "summary")
            self.assertIn("summary", entry)
            self.assertNotIn("snapshot", entry)
            self.assertNotIn("jobs", entry["summary"])

    def test_hub_status_reports_a_corrupted_registry_as_an_error(self) -> None:
        import contextlib
        import io

        from mergetrain.cli import main

        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "repos.json"
            registry.write_text("not json", encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["hub", "status", "--registry", str(registry), "--json"])

            self.assertEqual(code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("unreadable", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main()
