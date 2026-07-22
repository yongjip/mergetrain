"""Small stdlib HTTP server for the local read-only dashboard."""

from __future__ import annotations

import json
import mimetypes
import sys
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import unquote, urlsplit

from .config import MergetrainConfig
from .contract import CONTRACT_VERSION
from .errors import redact_secrets
from .hub import HubSnapshotCache, build_hub_snapshot_safe
from .snapshot import build_dashboard_snapshot

STATIC_ROOT = Path(__file__).with_name("dashboard_dist")
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; font-src 'self'; object-src 'none'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
SSE_POLL_SECONDS = 1.0
SSE_HEARTBEAT_SECONDS = 15.0
SSE_MAX_DURATION_SECONDS = 60.0
MAX_SSE_CLIENTS = 16


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.stopping = threading.Event()
        self._sse_slots = threading.BoundedSemaphore(MAX_SSE_CLIENTS)
        self._sse_lock = threading.Lock()
        self._active_sse_clients = 0
        super().__init__(*args, **kwargs)

    def acquire_sse_client(self) -> bool:
        acquired = self._sse_slots.acquire(blocking=False)
        if acquired:
            with self._sse_lock:
                self._active_sse_clients += 1
        return acquired

    def release_sse_client(self) -> None:
        with self._sse_lock:
            self._active_sse_clients -= 1
        self._sse_slots.release()

    @property
    def active_sse_clients(self) -> int:
        with self._sse_lock:
            return self._active_sse_clients

    def shutdown(self) -> None:
        self.stopping.set()
        super().shutdown()

    def server_close(self) -> None:
        self.stopping.set()
        super().server_close()

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)  # type: ignore[arg-type]


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _snapshot_error_payload(exc: Exception) -> dict[str, object]:
    return {
        "ok": False,
        "error": {
            "code": "snapshot_unavailable",
            "message": redact_secrets(str(exc) or exc.__class__.__name__),
            "retryable": True,
        },
    }


def _stable_snapshot_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _stable_snapshot_value(item)
            for key, item in value.items()
            if key != "generated_at"
        }
    if isinstance(value, list):
        return [_stable_snapshot_value(item) for item in value]
    return value


def _host_header_allowed(raw_host: str, *, bound_host: str, port: int) -> bool:
    try:
        parsed = urlsplit(f"//{raw_host.strip()}")
        requested_port = parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname or parsed.username is not None or parsed.password is not None:
        return False
    if requested_port is not None and requested_port != port:
        return False

    loopbacks = {"127.0.0.1", "localhost", "::1"}
    normalized_bound = bound_host.strip("[]").lower()
    if normalized_bound in loopbacks:
        return hostname in loopbacks
    if hostname == normalized_bound:
        return True
    try:
        ip_address(hostname)
    except ValueError:
        return False
    return True


def _safe_static_path(raw_path: str) -> Path | None:
    decoded = unquote(raw_path)
    relative = PurePosixPath(decoded.lstrip("/"))
    if any(part in {"", ".", ".."} for part in relative.parts):
        return None
    candidate = STATIC_ROOT.joinpath(*relative.parts)
    try:
        candidate.resolve().relative_to(STATIC_ROOT.resolve())
    except ValueError:
        return None
    return candidate


def make_handler(
    raw_snapshot_fn: Callable[[], dict], *, preview: bool = False,
    bound_host: str = "127.0.0.1",
) -> type[BaseHTTPRequestHandler]:
    def snapshot_fn() -> dict:
        # Stamp contract_version at the HTTP boundary, not inside
        # build_dashboard_snapshot — so a hub payload's embedded per-repo
        # snapshots stay bare and only the outer served frame carries the
        # number (contract 1).
        payload = raw_snapshot_fn()
        if isinstance(payload, dict) and "contract_version" not in payload:
            return {"contract_version": CONTRACT_VERSION, **payload}
        return payload

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "mergetrain-dashboard"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _headers(
            self,
            status: HTTPStatus,
            *,
            content_type: str,
            length: int | None = None,
            cache_control: str = "no-store",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache_control)
            if length is not None:
                self.send_header("Content-Length", str(length))
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()

        def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = _json_bytes(payload)
            self._headers(
                status,
                content_type="application/json; charset=utf-8",
                length=len(body),
            )
            self.wfile.write(body)

        def _send_file(self, path: Path) -> None:
            body = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            cache = "public, max-age=31536000, immutable" if path.parent.name == "assets" else "no-store"
            self._headers(
                HTTPStatus.OK,
                content_type=f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type,
                length=len(body),
                cache_control=cache,
            )
            self.wfile.write(body)

        def _reject_invalid_host(self) -> bool:
            hosts = self.headers.get_all("Host") or []
            port = int(cast(DashboardHTTPServer, self.server).server_port)
            if len(hosts) == 1 and _host_header_allowed(
                hosts[0], bound_host=bound_host, port=port
            ):
                return False
            self.close_connection = True
            self._send_json(
                {"ok": False, "error": {"code": "invalid_host",
                                        "message": "request Host is not allowed",
                                        "retryable": False}},
                HTTPStatus.MISDIRECTED_REQUEST,
            )
            return True

        def _send_snapshot_error(self, exc: Exception) -> None:
            self.close_connection = True
            self._send_json(
                _snapshot_error_payload(exc), HTTPStatus.SERVICE_UNAVAILABLE
            )

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self._reject_invalid_host():
                return
            path = urlsplit(self.path).path
            if path == "/api/health":
                self._send_json({"ok": True, "mode": "read-only", "preview": preview})
                return
            if path == "/api/snapshot":
                try:
                    payload = snapshot_fn()
                except Exception as exc:
                    self._send_snapshot_error(exc)
                    return
                self._send_json(payload)
                return
            if path == "/api/events":
                self._serve_events()
                return
            if path in {"/", "/index.html"}:
                static_path: Path | None = STATIC_ROOT / "index.html"
            else:
                static_path = _safe_static_path(path)
            if static_path is None or not static_path.is_file():
                self._send_json(
                    {"ok": False, "error": {"code": "not_found", "message": "not found",
                                            "retryable": False}},
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_file(static_path)

        def _read_only(self) -> None:
            if self._reject_invalid_host():
                return
            self.close_connection = True
            self._send_json(
                {"ok": False, "error": {"code": "read_only",
                                        "message": "the dashboard is read-only",
                                        "retryable": False}},
                HTTPStatus.METHOD_NOT_ALLOWED,
            )

        do_POST = _read_only
        do_PUT = _read_only
        do_PATCH = _read_only
        do_DELETE = _read_only

        def _serve_events(self) -> None:
            server = cast(DashboardHTTPServer, self.server)
            if not server.acquire_sse_client():
                self.close_connection = True
                self._send_json(
                    {"ok": False, "error": {"code": "too_many_streams",
                                            "message": "too many dashboard event streams",
                                            "retryable": True}},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            try:
                try:
                    payload = snapshot_fn()
                except Exception as exc:
                    self._send_snapshot_error(exc)
                    return
                self._headers(
                    HTTPStatus.OK,
                    content_type="text/event-stream; charset=utf-8",
                    cache_control="no-cache",
                )
                last_signature = b""
                started = time.monotonic()
                last_heartbeat = started
                while not server.stopping.is_set():
                    body = _json_bytes(payload)
                    signature = _json_bytes(_stable_snapshot_value(payload))
                    if signature != last_signature:
                        self.wfile.write(b"event: snapshot\n")
                        self.wfile.write(b"data: " + body + b"\n\n")
                        self.wfile.flush()
                        last_signature = signature
                    now = time.monotonic()
                    if now - last_heartbeat >= SSE_HEARTBEAT_SECONDS:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_heartbeat = now
                    if now - started >= SSE_MAX_DURATION_SECONDS:
                        return
                    if server.stopping.wait(SSE_POLL_SECONDS):
                        return
                    try:
                        payload = snapshot_fn()
                    except Exception as exc:
                        payload = _snapshot_error_payload(exc)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return
            finally:
                server.release_sse_client()

    return DashboardHandler


def _create_from_snapshot_fn(
    snapshot_fn: Callable[[], dict],
    *,
    host: str,
    port: int,
    preview: bool = False,
) -> DashboardHTTPServer:
    if not STATIC_ROOT.joinpath("index.html").is_file():
        raise FileNotFoundError("dashboard assets are missing from this installation")
    return DashboardHTTPServer(
        (host, port), make_handler(snapshot_fn, preview=preview, bound_host=host)
    )


def create_server(
    config: MergetrainConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    preview: bool = False,
) -> DashboardHTTPServer:
    return _create_from_snapshot_fn(
        lambda: build_dashboard_snapshot(config, preview=preview, read_only=True),
        host=host,
        port=port,
        preview=preview,
    )


def create_hub_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    registry: str | None = None,
) -> DashboardHTTPServer:
    # The registry is re-read on every snapshot so `hub add`/`hub remove`
    # show up live without restarting the server; a broken roster degrades
    # to a visible registry_error payload instead of killing the stream.
    # One cache per server: unchanged repos cost stat calls, not DB opens.
    cache = HubSnapshotCache()
    return _create_from_snapshot_fn(
        lambda: build_hub_snapshot_safe(registry, cache=cache),
        host=host,
        port=port,
    )


def _serve(server: DashboardHTTPServer, host: str, ready: Callable[[str], None] | None) -> None:
    actual_port = int(server.server_address[1])
    url = f"http://{host}:{actual_port}/"
    if ready:
        ready(url)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


def serve_dashboard(
    config: MergetrainConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    preview: bool = False,
    ready: Callable[[str], None] | None = None,
) -> None:
    _serve(create_server(config, host=host, port=port, preview=preview), host, ready)


def serve_hub(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    registry: str | None = None,
    ready: Callable[[str], None] | None = None,
) -> None:
    _serve(create_hub_server(host=host, port=port, registry=registry), host, ready)
