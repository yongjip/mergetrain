#!/usr/bin/env python3
"""Launch the MCP command a Registry client constructs and verify stdio tools."""

from __future__ import annotations

import argparse
import ast
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parents[1]


def source_version() -> str:
    """Read the checkout version without importing the package under test."""

    tree = ast.parse(
        (ROOT / "src/mergetrain/__init__.py").read_text(encoding="utf-8")
    )
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("mergetrain.__version__ must be a string literal")


def _argument_tokens(arguments: object) -> list[str]:
    tokens: list[str] = []
    if not isinstance(arguments, list):
        return tokens
    for argument in arguments:
        if not isinstance(argument, dict):
            raise ValueError("Registry arguments must be JSON objects")
        kind = argument.get("type")
        if kind == "positional":
            tokens.append(str(argument["value"]))
        elif kind == "named":
            tokens.append(str(argument["name"]))
            if "value" in argument:
                tokens.append(str(argument["value"]))
        else:
            raise ValueError(f"unsupported Registry argument type: {kind!r}")
    return tokens


def registry_command(path: Path) -> list[str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    packages = [
        package
        for package in manifest.get("packages") or []
        if package.get("registryType") == "pypi"
        and package.get("identifier") == "mergetrain"
    ]
    if len(packages) != 1:
        raise ValueError("server.json must contain one PyPI mergetrain package")
    package = packages[0]
    runtime = str(package.get("runtimeHint") or "")
    if not runtime:
        raise ValueError("the Registry package has no runtimeHint")
    return [
        runtime,
        *_argument_tokens(package.get("runtimeArguments")),
        str(package["identifier"]),
        *_argument_tokens(package.get("packageArguments")),
    ]


class _LineReader:
    def __init__(self, stream: TextIO):
        self._items: queue.Queue[dict[str, Any] | BaseException | None] = queue.Queue()

        def read() -> None:
            try:
                for line in stream:
                    self._items.put(json.loads(line))
            except BaseException as exc:
                self._items.put(exc)
            finally:
                self._items.put(None)

        threading.Thread(target=read, daemon=True).start()

    def response(self, request_id: int, *, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"MCP request {request_id} timed out")
            item = self._items.get(timeout=remaining)
            if item is None:
                raise RuntimeError("MCP server closed stdout before replying")
            if isinstance(item, BaseException):
                raise RuntimeError(f"invalid MCP server stdout: {item}") from item
            if item.get("id") == request_id:
                return item


def _send(stream: TextIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
    stream.flush()


def smoke(
    command: list[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    expected_version: str | None = None,
) -> None:
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=None if env is None else dict(env),
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    reader = _LineReader(process.stdout)
    stderr: list[str] = []

    def read_stderr() -> None:
        stderr.extend(process.stderr.readlines())

    threading.Thread(target=read_stderr, daemon=True).start()
    request_error: BaseException | None = None
    try:
        _send(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "mergetrain-registry-smoke", "version": "1"},
                },
            },
        )
        initialized = reader.response(1, timeout=timeout)
        if "error" in initialized:
            raise RuntimeError(f"MCP initialize failed: {initialized['error']}")
        if expected_version is not None:
            actual_version = str(
                ((initialized.get("result") or {}).get("serverInfo") or {}).get(
                    "version"
                )
                or ""
            )
            if actual_version != expected_version:
                raise RuntimeError(
                    "MCP serverInfo.version must match the built checkout; "
                    f"expected {expected_version!r}, received {actual_version!r}"
                )
        _send(
            process.stdin,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        _send(
            process.stdin,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        listed = reader.response(2, timeout=timeout)
        if "error" in listed:
            raise RuntimeError(f"MCP tools/list failed: {listed['error']}")
        tools = {
            str(tool.get("name")): tool
            for tool in (listed.get("result") or {}).get("tools") or []
        }
        expected_tools = {
            "mergetrain_status",
            "mergetrain_inspect",
            "mergetrain_validate",
            "mergetrain_enqueue",
            "mergetrain_deploy",
        }
        if set(tools) != expected_tools:
            raise RuntimeError(
                "MCP tools/list must expose the five stable v3 tools; "
                f"received {sorted(tools)}"
            )
        missing_descriptions = sorted(
            name
            for name, tool in tools.items()
            if not str(tool.get("description") or "").strip()
        )
        if missing_descriptions:
            raise RuntimeError(
                "MCP tools/list descriptions must be non-empty; "
                f"missing={missing_descriptions}"
            )
        deploy = tools.get("mergetrain_deploy")
        assert deploy is not None
        schema = deploy.get("inputSchema") or {}
        properties = schema.get("properties") or {}
        if properties or schema.get("required"):
            raise RuntimeError(
                "mergetrain_deploy must not accept model-supplied selection or "
                f"approval inputs; received properties={sorted(properties)}"
            )
    except BaseException as exc:
        request_error = exc
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    detail = "".join(stderr).strip()[-2000:]
    if request_error is not None:
        suffix = f"; stderr: {detail}" if detail else ""
        raise RuntimeError(f"{request_error}{suffix}") from request_error
    if process.returncode not in {0, -15}:
        raise RuntimeError(
            f"MCP server exited with {process.returncode}: {detail or 'no stderr'}"
        )


def isolated_uv_environment(cache_dir: Path) -> dict[str, str]:
    """Return a clean uv cache environment for one Registry launch attempt."""

    env = os.environ.copy()
    env["UV_CACHE_DIR"] = str(cache_dir)
    return env


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "server.json")
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument(
        "--require-version",
        action="store_true",
        help="require serverInfo.version to match this checkout",
    )
    parser.add_argument("--command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command or registry_command(args.manifest)
    last_error: BaseException | None = None
    for attempt in range(1, max(1, args.attempts) + 1):
        try:
            # PyPI permits metadata caches to stay fresh for several minutes.
            # A newly published version can therefore produce a cached
            # "version not found" result. Give each retry a fresh uv cache
            # while preserving the exact command constructed from server.json.
            with tempfile.TemporaryDirectory(prefix="mergetrain-registry-uv-") as td:
                smoke(
                    command,
                    timeout=args.timeout,
                    env=isolated_uv_environment(Path(td)),
                    expected_version=source_version() if args.require_version else None,
                )
            print("MCP Registry launch OK: initialize, five tools, deploy schema")
            return 0
        except BaseException as exc:
            last_error = exc
            if attempt < args.attempts:
                time.sleep(min(10, 2 ** (attempt - 1)))
    assert last_error is not None
    print(f"MCP Registry launch failed: {last_error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
