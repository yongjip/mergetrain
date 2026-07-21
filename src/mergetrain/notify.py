"""Local notifications for daemon events.

macOS-only by design (issue #32 Stage 0): notifications go through
``osascript``, which every stock macOS has, so the feature adds no runtime
dependency. On other platforms — or when ``osascript`` is missing — the
notifier is a silent no-op rather than an error, because a notification is
an optional convenience and must never break a sweep.
"""

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

Notifier = Callable[[str, str], None]

# Outcomes that repeat sweep after sweep (a broken repo stays broken) notify
# only when the outcome *changes*; a landed train is new work every time.
_TRANSITION_ONLY = {"error", "reconcile_paused"}
_SILENT = {"idle", "skipped", "excluded"}


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
    subprocess.run(
        [osascript, "-e", script],
        check=False,
        capture_output=True,
        timeout=10,
    )


def sweep_notifications(
    outcomes: list[dict[str, Any]],
    previous: dict[str, str],
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
    target = notify_state_path(registry)
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
    target = notify_state_path(registry)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=".hub-notify-",
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
