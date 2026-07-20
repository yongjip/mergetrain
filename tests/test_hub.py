from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from mergetrain.config import load_config
from mergetrain.dashboard import create_hub_server
from mergetrain.errors import QueueError
from mergetrain.hub import build_hub_snapshot
from mergetrain.registry import add_repo, load_registry
from mergetrain.store import connect, enqueue_job


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
                with self.assertRaises(Exception):
                    conn.execute("DELETE FROM deploy_queue")
            finally:
                conn.close()


class HubSnapshotCacheTests(unittest.TestCase):
    def test_cache_skips_rebuilds_until_queue_or_config_changes(self) -> None:
        import mergetrain.hub as hub_module
        from mergetrain.hub import HubSnapshotCache
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            live = make_repo(root, "live")
            seed_queue(live)
            add_repo(live, registry)
            cache = HubSnapshotCache()
            calls = []
            real_build = hub_module.build_dashboard_snapshot

            def counting_build(*args, **kwargs):
                calls.append(1)
                return real_build(*args, **kwargs)

            with mock.patch.object(hub_module, "build_dashboard_snapshot", counting_build):
                first = build_hub_snapshot(load_registry(registry), cache=cache)
                second = build_hub_snapshot(load_registry(registry), cache=cache)
                self.assertEqual(len(calls), 1)  # second build was a cache hit
                self.assertEqual(
                    second["repos"][0]["snapshot"]["counts"]["queued"],
                    first["repos"][0]["snapshot"]["counts"]["queued"],
                )

                # A queue write touches db/-wal → fingerprint changes.
                config = load_config(repo=live)
                conn = connect(config.state.db)
                try:
                    enqueue_job(conn, task="second", branch="agent/second", worktree_path=str(live))
                finally:
                    conn.close()
                third = build_hub_snapshot(load_registry(registry), cache=cache)
                self.assertEqual(len(calls), 2)
                self.assertEqual(third["repos"][0]["snapshot"]["counts"]["queued"], 2)

                config_file = live / ".mergetrain.yaml"
                config_file.write_text("project:\n  name: renamed\n", encoding="utf-8")
                fourth = build_hub_snapshot(load_registry(registry), cache=cache)
                self.assertEqual(len(calls), 3)
                self.assertEqual(fourth["repos"][0]["name"], "renamed")

    def test_daemon_flag_flip_is_visible_through_a_warm_cache(self) -> None:
        from mergetrain.hub import HubSnapshotCache

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            live = make_repo(root, "live")
            seed_queue(live)
            add_repo(live, registry)
            cache = HubSnapshotCache()

            warm = build_hub_snapshot(load_registry(registry), cache=cache)
            self.assertTrue(warm["repos"][0]["daemon"])

            add_repo(live, registry, daemon=False)  # registry-only change
            flipped = build_hub_snapshot(load_registry(registry), cache=cache)
            self.assertFalse(flipped["repos"][0]["daemon"])


class HubRegistryDegradationTests(unittest.TestCase):
    def test_broken_registry_degrades_to_visible_error_payload(self) -> None:
        from mergetrain.hub import build_hub_snapshot_safe

        with tempfile.TemporaryDirectory() as td:
            registry = Path(td) / "repos.json"
            registry.write_text("not json", encoding="utf-8")

            snapshot = build_hub_snapshot_safe(str(registry))

            self.assertTrue(snapshot["ok"])
            self.assertTrue(snapshot["hub"])
            self.assertEqual(snapshot["repos"], [])
            self.assertIn("unreadable", snapshot["registry_error"])

    def test_hub_server_survives_registry_corruption_mid_flight(self) -> None:
        from mergetrain.dashboard import create_hub_server

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            live = make_repo(root, "live")
            seed_queue(live)
            add_repo(live, registry)

            server = create_hub_server(host="127.0.0.1", port=0, registry=str(registry))
            port = int(server.server_address[1])
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                client.request("GET", "/api/snapshot")
                first = json.loads(client.getresponse().read())
                client.close()
                self.assertEqual(first["repo_count"], 1)

                registry.write_text("corrupted", encoding="utf-8")
                client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                client.request("GET", "/api/snapshot")
                response = client.getresponse()
                degraded = json.loads(response.read())
                client.close()
                self.assertEqual(response.status, 200)
                self.assertIn("registry_error", degraded)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


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
            self.assertIn("live: queued=1 | next: run_batch_validate", lines)
            self.assertTrue(any("gone" in line and "ERROR" in line for line in lines))


class HubServerTests(unittest.TestCase):
    def request(self, port: int, method: str, path: str):
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            client.request(method, path)
            response = client.getresponse()
            return response.status, response.read()
        finally:
            client.close()

    def test_hub_server_serves_snapshot_and_stays_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry = root / "repos.json"
            live = make_repo(root, "live")
            seed_queue(live)
            add_repo(live, registry)

            server = create_hub_server(host="127.0.0.1", port=0, registry=str(registry))
            port = int(server.server_address[1])
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, body = self.request(port, "GET", "/api/snapshot")
                self.assertEqual(status, 200)
                payload = json.loads(body)
                self.assertTrue(payload["hub"])
                self.assertEqual(payload["repo_count"], 1)
                self.assertEqual(payload["repos"][0]["name"], "live")

                # Registry edits show up without a server restart.
                extra = make_repo(root, "extra")
                add_repo(extra, registry)
                status, body = self.request(port, "GET", "/api/snapshot")
                self.assertEqual(json.loads(body)["repo_count"], 2)

                status, body = self.request(port, "POST", "/api/snapshot")
                self.assertEqual(status, 405)
                self.assertEqual(json.loads(body)["error"], "read_only")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
