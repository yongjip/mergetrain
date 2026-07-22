from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mergetrain.config import load_config
from mergetrain.dashboard import _create_from_snapshot_fn, create_server
from mergetrain.snapshot import build_dashboard_snapshot
from mergetrain.store import (
    claim_all_queued,
    connect,
    enqueue_job,
    mark_job,
    record_run_event,
    release_runner_lock,
)


class DashboardTests(unittest.TestCase):
    def make_config(self, root: Path):
        return load_config(repo=root, db_override=root / "queue.sqlite")

    def test_snapshot_points_too_new_config_at_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".mergetrain.yaml").write_text(
                "version: 999\nproject:\n  name: future\n", encoding="utf-8"
            )
            payload = build_dashboard_snapshot(self.make_config(root))
            self.assertEqual(payload["next_action"], "upgrade_mergetrain")

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
                record_run_event(
                    conn,
                    claim_token=claimed[0].claim_token,
                    job_id=claimed[0].id,
                    phase="assembling",
                    state="success",
                    message=f"Merged {claimed[0].branch}",
                )
                record_run_event(
                    conn,
                    claim_token=claimed[0].claim_token,
                    phase="gating",
                    state="reused",
                    message="Reused gate 1/2: diff-check",
                    detail="a" * 40,
                )
                record_run_event(
                    conn,
                    claim_token=claimed[0].claim_token,
                    phase="gating",
                    state="active",
                    message="Running gate 2/2: diff-check",
                    detail="git diff --check ${integration_ref}..HEAD",
                )
            finally:
                conn.close()

            payload = build_dashboard_snapshot(config)
            self.assertEqual(payload["train"]["selection"], "running")
            self.assertEqual(payload["progress"]["phase"], "gating")
            self.assertEqual(payload["progress"]["completed_job_ids"], [claimed[0].id])
            self.assertNotIn("gating", payload["progress"]["completed_phases"])
            self.assertEqual(
                payload["progress"]["current_gate"],
                {
                    "index": 2,
                    "total": 2,
                    "name": "diff-check",
                    "state": "active",
                    "command": "git diff --check ${integration_ref}..HEAD",
                    "started_at": payload["progress"]["updated_at"],
                },
            )
            self.assertEqual(
                [gate["state"] for gate in payload["progress"]["gates"]],
                ["reused", "active"],
            )
            self.assertFalse(payload["project"]["reuse"]["enabled"])
            self.assertEqual(payload["project"]["reuse"]["max_age_minutes"], 60)
            self.assertEqual(payload["lock"]["owner"], f"local:{os.getpid()}")
            self.assertIn("heartbeat_at", payload["lock"])
            self.assertNotIn("worktree_path", payload["jobs"][0])
            self.assertNotIn("log_path", payload["jobs"][0])
            self.assertNotIn("claim_token", payload["events"][0])
            self.assertNotIn("runtime", payload)
            self.assertEqual(payload["project"]["terminology"]["completed"], "deployed")
            self.assertEqual(payload["project"]["push_specs"], ["HEAD:main"])

            cleanup = connect(config.state.db)
            try:
                release_runner_lock(cleanup, owner=owner, token=claimed[0].claim_token)
            finally:
                cleanup.close()

    def test_snapshot_exposes_configured_integration_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".mergetrain.yaml").write_text(
                """git:
  remote: upstream
  integration_branch: main
  push_refs:
    - main
    - release
terminology:
  git_operation: integrate
""",
                encoding="utf-8",
            )
            payload = build_dashboard_snapshot(self.make_config(root))
            self.assertEqual(payload["project"]["terminology"]["in_progress"], "integrating")
            self.assertEqual(payload["project"]["remote"], "upstream")
            self.assertEqual(payload["project"]["push_specs"], ["HEAD:main", "HEAD:release"])

    def test_http_server_serves_read_only_api_and_static_assets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            conn = connect(config.state.db)
            conn.close()
            server = create_server(config, host="127.0.0.1", port=0, preview=True)
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
                # Contract 1: the served snapshot is stamped at the HTTP boundary.
                self.assertEqual(payload["contract_version"], 1)
                self.assertTrue(payload["project"]["preview"])
                self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
                self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
                connection.close()

                connection = http.client.HTTPConnection(host, port, timeout=3)
                connection.request("POST", "/api/cancel")
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 405)
                self.assertEqual(payload["error"]["code"], "read_only")
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

    def test_http_server_rejects_untrusted_host_header(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            conn = connect(config.state.db)
            conn.close()
            server = create_server(config, host="127.0.0.1", port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            host, port = server.server_address
            try:
                connection = http.client.HTTPConnection(host, port, timeout=3)
                connection.request(
                    "GET", "/api/snapshot", headers={"Host": "attacker.example"}
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 421)
                self.assertEqual(payload["error"]["code"], "invalid_host")
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)

    def test_snapshot_error_returns_json_instead_of_dropping_connection(self) -> None:
        def fail_snapshot() -> dict:
            raise RuntimeError("snapshot failed")

        server = _create_from_snapshot_fn(
            fail_snapshot, host="127.0.0.1", port=0
        )
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        host, port = server.server_address
        try:
            for path in ("/api/snapshot", "/api/events"):
                connection = http.client.HTTPConnection(host, port, timeout=3)
                connection.request("GET", path)
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 503, path)
                self.assertEqual(payload["error"]["code"], "snapshot_unavailable")
                self.assertIn("snapshot failed", payload["error"]["message"])
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_event_stream_ignores_generated_at_and_stops_with_server(self) -> None:
        calls = 0

        def changing_timestamp_only() -> dict:
            nonlocal calls
            calls += 1
            return {"ok": True, "generated_at": str(calls), "value": "stable"}

        server = _create_from_snapshot_fn(
            changing_timestamp_only, host="127.0.0.1", port=0
        )
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        host, port = server.server_address
        connection = http.client.HTTPConnection(host, port, timeout=3)
        try:
            with patch("mergetrain.dashboard.SSE_POLL_SECONDS", 0.05), patch(
                "mergetrain.dashboard.SSE_HEARTBEAT_SECONDS", 10.0
            ):
                connection.request("GET", "/api/events")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.fp.readline(), b"event: snapshot\n")
                self.assertTrue(response.fp.readline().startswith(b"data: "))
                self.assertEqual(response.fp.readline(), b"\n")
                time.sleep(0.2)
                response.fp.raw._sock.settimeout(0.1)
                with self.assertRaises((TimeoutError, socket.timeout)):
                    response.fp.readline()
                self.assertGreater(calls, 1)
                server.shutdown()
                worker.join(timeout=3)
                deadline = time.monotonic() + 3
                while server.active_sse_clients and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(server.active_sse_clients, 0)
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_event_stream_rejects_clients_over_the_cap(self) -> None:
        with patch("mergetrain.dashboard.MAX_SSE_CLIENTS", 1):
            server = _create_from_snapshot_fn(
                lambda: {"ok": True}, host="127.0.0.1", port=0
            )
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        host, port = server.server_address
        first = http.client.HTTPConnection(host, port, timeout=3)
        second = http.client.HTTPConnection(host, port, timeout=3)
        try:
            first.request("GET", "/api/events")
            first_response = first.getresponse()
            self.assertEqual(first_response.status, 200)
            self.assertEqual(first_response.fp.readline(), b"event: snapshot\n")
            first_response.fp.readline()
            first_response.fp.readline()

            second.request("GET", "/api/events")
            second_response = second.getresponse()
            payload = json.loads(second_response.read())
            self.assertEqual(second_response.status, 503)
            self.assertEqual(payload["error"]["code"], "too_many_streams")
        finally:
            first.close()
            second.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_dashboard_get_does_not_create_queue_database(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = self.make_config(Path(td))
            self.assertFalse(config.state.db.exists())
            server = create_server(config, host="127.0.0.1", port=0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            host, port = server.server_address
            try:
                connection = http.client.HTTPConnection(host, port, timeout=3)
                connection.request("GET", "/api/snapshot")
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 503)
                self.assertEqual(payload["error"]["code"], "snapshot_unavailable")
                self.assertFalse(config.state.db.exists())
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)

    def test_snapshot_exposes_deployed_verification_attention(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="deploy", branch="codex/deploy")
                mark_job(
                    conn,
                    job.id,
                    status="deployed",
                    push_status="succeeded",
                    verify_status="failed",
                    note="post-push verify warning: health check failed",
                )
                record_run_event(
                    conn,
                    job_id=job.id,
                    phase="complete",
                    state="warning",
                    message=f"Job #{job.id} deployed; verification needs attention",
                    detail="post-push verify warning: health check failed",
                )
            finally:
                conn.close()

            payload = build_dashboard_snapshot(config)
            self.assertEqual(payload["jobs"][0]["status"], "deployed")
            self.assertEqual(payload["jobs"][0]["push_status"], "succeeded")
            self.assertEqual(payload["jobs"][0]["verify_status"], "failed")
            self.assertEqual(payload["events"][-1]["state"], "warning")

    def test_snapshot_removes_worktree_path_embedded_in_job_note(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = self.make_config(root)
            sensitive = "/private/sensitive/integration-worktree"
            conn = connect(config.state.db)
            try:
                job = enqueue_job(
                    conn,
                    task="failed gate",
                    branch="codex/failure",
                    worktree_path=sensitive,
                )
                mark_job(
                    conn,
                    job.id,
                    status="failed",
                    note=f"command failed (1) in {sensitive}: make test",
                )
            finally:
                conn.close()

            payload = build_dashboard_snapshot(config)
            note = payload["jobs"][0]["note"]
            self.assertNotIn(sensitive, note)
            self.assertIn("[worktree]", note)


if __name__ == "__main__":
    unittest.main()
