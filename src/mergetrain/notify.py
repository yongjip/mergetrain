"""Local notifications for daemon events.

macOS-only by design (issue #32 Stage 0): notifications go through
``osascript``, which every stock macOS has, so the feature adds no runtime
dependency. On other platforms — or when ``osascript`` is missing — the
notifier is a silent no-op rather than an error, because a notification is
an optional convenience and must never break a sweep.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from typing import Any

Notifier = Callable[[str, str], None]

# Outcomes that repeat sweep after sweep (a broken repo stays broken) notify
# only when the outcome *changes*; a processed train is new work every time.
_TRANSITION_ONLY = {"error", "reconcile_paused"}
_SILENT = {"idle", "skipped", "excluded"}


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
) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """Turn one sweep's outcomes into (title, message) pairs plus new state.

    Pure so it is unit-testable without threads: the daemon loop owns the
    ``previous`` mapping (repo path -> last outcome) and threads it through.
    """

    messages: list[tuple[str, str]] = []
    current: dict[str, str] = {}
    for item in outcomes:
        path = str(item.get("path") or "")
        name = str(item.get("name") or path)
        outcome = str(item.get("outcome") or "")
        current[path] = outcome
        if outcome in _SILENT:
            continue
        if outcome in _TRANSITION_ONLY and previous.get(path) == outcome:
            continue
        if outcome.startswith("processed:"):
            count = outcome.split(":", 1)[1]
            job_word = "job" if count == "1" else "jobs"
            messages.append((f"mergetrain · {name}", f"Train landed ({count} {job_word})"))
        elif outcome == "reconcile_paused":
            messages.append(
                (f"mergetrain · {name}", "Deploy paused: jobs need reconcile")
            )
        elif outcome == "error":
            messages.append(
                (f"mergetrain · {name}", str(item.get("error") or "sweep error"))
            )
    return messages, current
