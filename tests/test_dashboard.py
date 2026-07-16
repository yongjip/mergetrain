from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from mergetrain.config import load_config
from mergetrain.dashboard import create_server
from mergetrain.snapshot import build_dashboard_snapshot
from mergetrain.store import claim_all_queued, connect, enqueue_job, release_runner_lock


class DashboardTests(unittest.TestCase):
    def make_config(self, root: Path):
        return load_config(repo=root, db_override=root / "queue.sqlite")

    def test_snapshot_is_live_and_omits_local_paths_and_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            try:
                enqueue_job(
                    conn,
                    task="dashboard",
                    branch="codex/dashboard",
                    worktree_path="/private/sensitive/worktree",
                )
                claimed = claim_all_queued(conn, owner=owner)
            finally:
                conn.close()

            payload = build_dashboard_snapshot(config)
            self.assertEqual(payload["train"]["selection"], "running")
            self.assertEqual(payload["progress"]["phase"], "claiming")
            self.assertEqual(payload["lock"]["owner"], f"local:{os.getpid()}")
            self.assertIn("heartbeat_at", payload["lock"])
            self.assertNotIn("worktree_path", payload["jobs"][0])
            self.assertNotIn("log_path", payload["jobs"][0])
            self.assertNotIn("claim_token", payload["events"][0])

            cleanup = connect(config.state.db)
            try:
                release_runner_lock(cleanup, owner=owner, token=claimed[0].claim_token)
            finally:
                cleanup.close()

    def test_http_server_serves_read_only_api_and_static_assets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            server = create_server(config, host="127.0.0.1", port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            host, port = server.server_address
            try:
                connection = http.client.HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/api/snapshot")
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertTrue(payload["ok"])
                self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
                self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
                connection.close()

                connection = http.client.HTTPConnection(host, port, timeout=3)
                connection.request("POST", "/api/cancel")
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 405)
                self.assertEqual(payload["error"], "read_only")
                connection.close()

                connection = http.client.HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/")
                response = connection.getresponse()
                body = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("mergetrain · live local status", body)
                connection.close()

                connection = http.client.HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/..%2Fpyproject.toml")
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 404)
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
