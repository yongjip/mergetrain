"""Transition-deduped desktop and provider-neutral webhook notifications."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from .config import NotifyConfig

Notifier = Callable[[str, str], None]

# Outcomes that repeat sweep after sweep (a broken repo stays broken) notify
# only when the outcome *changes*; a landed train is new work every time.
_TRANSITION_ONLY = {"error", "reconcile_paused"}
_SILENT = {"idle", "skipped", "excluded"}


def notification_transition(outcome: str) -> str:
    """Map detailed daemon outcomes onto stable configuration categories."""

    if outcome.startswith(("landed:", "processed:")):
        return "landed"
    if outcome.startswith(("partial:", "no_landing:")):
        return "blocked"
    if outcome == "reconcile_paused":
        return "needs_reconcile"
    if outcome == "error" or outcome.startswith("error:"):
        return "daemon_paused"
    return ""


def _is_transition_only(outcome: str) -> bool:
    # A repo that lands nothing every sweep (all jobs blocked/failed) is a
    # persistent state like `error` — notify once, not every tick.
    return outcome in _TRANSITION_ONLY or outcome.startswith("no_landing:")


def _dedup_key(outcome: str, error: str) -> str:
    # Key transition-only outcomes on their full identity, not the bare class:
    # a repo whose failure changes from one error to a materially different
    # one is a genuine transition and must re-notify.
    if outcome == "error":
        return f"error:{error or 'sweep error'}"
    return outcome


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def system_notifier(title: str, message: str) -> None:
    """Post one desktop notification; silently do nothing off-macOS."""

    if sys.platform != "darwin":
        return
    osascript = shutil.which("osascript")
    if not osascript:
        return
    script = f'display notification "{_escape(message)}" with title "{_escape(title)}"'
    try:
        subprocess.run(
            [osascript, "-e", script],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def webhook_notifier(url: str, *, timeout_seconds: int = 10) -> Notifier:
    """Build a notifier that POSTs a small provider-neutral JSON envelope."""

    def send(title: str, message: str) -> None:
        body = json.dumps(
            {"title": title, "message": message},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib_request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "mergetrain-notifier/1",
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                if not 200 <= status < 300:
                    raise RuntimeError(f"webhook delivery returned HTTP {status}")
                response.read(1)
        except urllib_error.HTTPError as exc:
            # Never include the credential-bearing URL from HTTPError.__str__.
            raise RuntimeError(
                f"webhook delivery returned HTTP {exc.code}"
            ) from None
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(
                f"webhook delivery failed ({type(exc).__name__})"
            ) from None

    return send


def notifier_chain(*notifiers: Notifier) -> Notifier:
    """Deliver to every configured backend, in order."""

    def send(title: str, message: str) -> None:
        failures: list[Exception] = []
        for notifier in notifiers:
            try:
                notifier(title, message)
            except Exception as exc:  # noqa: BLE001 - other backends still get a turn
                failures.append(exc)
        if failures:
            raise failures[0]

    return send


def configured_notifier(config: NotifyConfig) -> Notifier:
    """Webhook first (retryable), then the best-effort desktop backend."""

    backends: list[Notifier] = []
    if config.webhook_url:
        backends.append(
            webhook_notifier(
                config.webhook_url,
                timeout_seconds=config.timeout_seconds,
            )
        )
    backends.append(system_notifier)
    return notifier_chain(*backends)


def sweep_notifications(
    outcomes: list[dict[str, Any]],
    previous: dict[str, str],
    *,
    transitions: tuple[str, ...] | None = None,
) -> tuple[list[tuple[str, str, str, str]], dict[str, str]]:
    """Turn one sweep's outcomes into messages plus already-settled state.

    Pure so it is unit-testable without threads. Returns:

    * ``messages`` — ``(path, key, title, body)`` still awaiting delivery.
      The caller commits ``key`` for ``path`` only after the notifier
      succeeds, so a failed delivery is retried rather than silently
      consumed.
    * ``settled`` — ``path -> key`` for outcomes that need no delivery
      (silent, or an unchanged transition-only outcome). These carry no
      delivery risk, so the caller can commit them immediately.
    """

    messages: list[tuple[str, str, str, str]] = []
    settled: dict[str, str] = {}
    for item in outcomes:
        path = str(item.get("path") or "")
        name = str(item.get("name") or path)
        outcome = str(item.get("outcome") or "")
        key = _dedup_key(outcome, str(item.get("error") or ""))
        if transitions is not None and notification_transition(outcome) not in transitions:
            settled[path] = key
            continue
        if outcome in _SILENT:
            settled[path] = key
            continue
        if _is_transition_only(outcome) and previous.get(path) == key:
            settled[path] = key
            continue
        title = f"mergetrain · {name}"
        if outcome.startswith("landed:") or outcome.startswith("processed:"):
            count = outcome.split(":", 1)[1]
            job_word = "job" if count == "1" else "jobs"
            messages.append((path, key, title, f"Train landed ({count} {job_word})"))
        elif outcome.startswith("partial:"):
            messages.append((path, key, title, f"Partial: {outcome.split(':', 1)[1]} landed, rest blocked/failed"))
        elif outcome.startswith("no_landing:"):
            count = outcome.split(":", 1)[1]
            job_word = "job" if count == "1" else "jobs"
            messages.append((path, key, title, f"Nothing landed — {count} {job_word} blocked or failed"))
        elif outcome == "reconcile_paused":
            messages.append((path, key, title, "Deploy paused: jobs need reconcile"))
        elif outcome == "error":
            messages.append((path, key, title, str(item.get("error") or "sweep error")))
    return messages, settled


def notify_state_path(registry: str | None) -> Path:
    """Where the per-sweep dedup state lives, beside the hub registry.

    Persisting it means ``hub daemon --once`` (cron) does not re-notify
    every persistent error on every invocation, and a restart of the loop
    resumes its dedup instead of firing a storm.
    """

    from .registry import registry_path

    base = Path(registry) if registry else registry_path()
    return base.with_name("hub-notify-state.json")


def load_notify_state(registry: str | None) -> dict[str, str]:
    return load_notify_state_file(notify_state_path(registry))


def load_notify_state_file(target: str | Path) -> dict[str, str]:
    target = Path(target)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A missing or corrupt state file is not an error: dedup degrades to
        # "notify once more", never a crash.
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_notify_state(state: dict[str, str], registry: str | None) -> None:
    save_notify_state_file(state, notify_state_path(registry))


def save_notify_state_file(state: dict[str, str], target: str | Path) -> None:
    target = Path(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=".notify-",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
            os.replace(handle.name, target)
        except Exception:
            Path(handle.name).unlink(missing_ok=True)
            raise
    except OSError:
        # Best-effort: notifications must never break a sweep, and losing the
        # dedup state only risks one extra notification.
        pass


def repo_notify_state_path(db_path: str | Path) -> Path:
    return Path(db_path).expanduser().with_name("notify-state.json")
