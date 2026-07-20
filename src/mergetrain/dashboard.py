"""Small stdlib HTTP server for the local read-only dashboard."""

from __future__ import annotations

import json
import mimetypes
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import unquote, urlsplit

from .config import MergetrainConfig
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


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


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
    snapshot_fn: Callable[[], dict], *, preview: bool = False
) -> type[BaseHTTPRequestHandler]:
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

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            path = urlsplit(self.path).path
            if path == "/api/health":
                self._send_json({"ok": True, "mode": "read-only", "preview": preview})
                return
            if path == "/api/snapshot":
                self._send_json(snapshot_fn())
                return
            if path == "/api/events":
                self._serve_events()
                return
            if path in {"/", "/index.html"}:
                static_path = STATIC_ROOT / "index.html"
            else:
                static_path = _safe_static_path(path)
            if static_path is None or not static_path.is_file():
                self._send_json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_file(static_path)

        def _read_only(self) -> None:
            self.close_connection = True
            self._send_json(
                {"ok": False, "error": "read_only"},
                HTTPStatus.METHOD_NOT_ALLOWED,
            )

        do_POST = _read_only
        do_PUT = _read_only
        do_PATCH = _read_only
        do_DELETE = _read_only

        def _serve_events(self) -> None:
            self._headers(
                HTTPStatus.OK,
                content_type="text/event-stream; charset=utf-8",
                cache_control="no-cache",
            )
            last_body = b""
            try:
                while True:
                    body = _json_bytes(snapshot_fn())
                    if body != last_body:
                        self.wfile.write(b"event: snapshot\n")
                        self.wfile.write(b"data: " + body + b"\n\n")
                        self.wfile.flush()
                        last_body = body
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                return

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
    return DashboardHTTPServer((host, port), make_handler(snapshot_fn, preview=preview))


def create_server(
    config: MergetrainConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    preview: bool = False,
) -> DashboardHTTPServer:
    return _create_from_snapshot_fn(
        lambda: build_dashboard_snapshot(config, preview=preview),
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
