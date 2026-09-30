"""Transition-deduped provider-neutral webhook notifications."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import NotifyConfig

Notifier = Callable[[str, str], None]

# Outcomes that repeat sweep after sweep (a broken repo stays broken) notify
# only when the outcome *changes*; a landed train is new work every time.
_TRANSITION_ONLY = {"error", "reconcile_paused"}
_SILENT = {"idle", "skipped", "excluded"}


def notification_transition(outcome: str) -> str:
    """Map detailed daemon outcomes onto stable configuration categories."""

    if outcome.startswith(("landed:", "processed:", "unverified:")):
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


def _open_webhook(request: Any, *, timeout_seconds: int) -> Any:
    """Send ``request`` without following redirects.

    A redirect could carry the POST to any host, loopback included, and its
    answer would then count as delivery (#231). A 3xx is an HTTPError instead.
    """

    from urllib import request as urllib_request

    class _RefuseRedirects(urllib_request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:
            return None

    opener = urllib_request.build_opener(_RefuseRedirects)
    return opener.open(request, timeout=timeout_seconds)


def webhook_notifier(url: str, *, timeout_seconds: int = 10) -> Notifier:
    """Build a notifier that POSTs a small provider-neutral JSON envelope."""

    def send(title: str, message: str) -> None:
        # Keep the CLI's cold import path small. Webhook networking is only
        # needed when a notification is actually delivered.
        from urllib import error as urllib_error
        from urllib import request as urllib_request

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
            with _open_webhook(request, timeout_seconds=timeout_seconds) as response:
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


def configured_notifier(config: NotifyConfig) -> Notifier:
    """Build the configured webhook notifier; without a webhook it sends nothing."""

    if not config.webhook_url:
        return lambda title, message: None
    return webhook_notifier(config.webhook_url, timeout_seconds=config.timeout_seconds)


def sweep_notifications(
    outcomes: list[dict[str, Any]],
    previous: dict[str, str],
    *,
    transitions: tuple[str, ...] | None = None,
) -> tuple[list[tuple[str, str, str, str]], dict[str, str]]:
    """Turn one sweep's outcomes into messages plus already-settled state.

    Pure so it is unit-testable without threads. Returns:

    * ``messages`` — ``(path, key, title, body)`` still awaiting delivery.
      ``deliver_notifications`` commits ``key`` for ``path`` once the
      message is delivered and keeps an event message that fails, so a
      failed delivery is retried rather than silently consumed.
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
        elif outcome.startswith("unverified:"):
            count = outcome.split(":", 1)[1]
            job_word = "job" if count == "1" else "jobs"
            messages.append(
                (
                    path,
                    key,
                    title,
                    f"Train landed ({count} {job_word}); verification needs attention",
                )
            )
        elif outcome.startswith("partial:"):
            messages.append((path, key, title, f"Partial: {outcome.split(':', 1)[1]} landed, rest blocked/failed"))
        elif outcome.startswith("no_landing:"):
            count = outcome.split(":", 1)[1]
            job_word = "job" if count == "1" else "jobs"
            messages.append((path, key, title, f"Nothing landed — {count} {job_word} blocked or failed"))
        elif outcome == "reconcile_paused":
            messages.append((path, key, title, "Deploy paused: jobs need reconcile"))
        elif outcome == "error":
            # The error text stays in the local log and dedup state: it can
            # name the OS user, home-directory paths, or command output, none
            # of which a third-party webhook should receive.
            messages.append((path, key, title, "Deploy paused: the daemon hit an error; see its log"))
    return messages, settled


# The notify state entry that keeps event messages no sweep could deliver yet.
# Repo paths are absolute or home-relative, so it never names a repo. A webhook
# that stays down keeps at most the newest _UNDELIVERED_LIMIT of them.
_UNDELIVERED = "undelivered"
_UNDELIVERED_LIMIT = 20


def _is_event(key: str) -> bool:
    # A landing, or jobs that did not land, happens once: the next sweep,
    # usually idle, never reports it again. An error or a reconcile pause is a
    # state that each sweep reports again while it lasts.
    return key.startswith(("landed:", "processed:", "unverified:", "partial:", "no_landing:"))


def _undelivered(state: dict[str, str]) -> list[tuple[str, str, str, str]]:
    try:
        entries = json.loads(state.get(_UNDELIVERED, "[]"))
    except ValueError:
        return []
    if not isinstance(entries, list):
        return []
    return [
        (entry[0], entry[1], entry[2], entry[3])
        for entry in entries
        if isinstance(entry, list)
        and len(entry) == 4
        and all(isinstance(part, str) for part in entry)
    ]


def deliver_notifications(
    outcomes: list[dict[str, Any]],
    previous: dict[str, str],
    deliver: Callable[[str, str, str, str], None],
    *,
    transitions: tuple[str, ...] | None = None,
    on_error: Callable[[Exception], None],
) -> dict[str, str]:
    """Deliver one sweep's notifications and return the state for the next.

    ``deliver(path, key, title, body)`` raises when delivery fails. A state
    message's key is committed only once it is delivered, so the next sweep
    that reports the same state sends it again. An event message that fails
    is kept and sent again by later sweeps, before newer messages for its
    repo, until it is delivered. After one failure for a repo, its remaining
    messages wait for the next sweep instead of each waiting out a timeout.
    """

    messages, state = sweep_notifications(outcomes, previous, transitions=transitions)
    kept: list[tuple[str, str, str, str]] = []
    failed: set[str] = set()

    def sent(message: tuple[str, str, str, str]) -> bool:
        if message[0] in failed:
            return False
        try:
            deliver(*message)
        except Exception as exc:  # noqa: BLE001 - never break a sweep
            on_error(exc)
            failed.add(message[0])
            return False
        return True

    for message in _undelivered(previous):
        if not sent(message):
            kept.append(message)
    for message in messages:
        path, key = message[0], message[1]
        if sent(message):
            state[path] = key
        elif _is_event(key):
            # Kept now, so the key settles its transition: the next sweep
            # must not build the same message again beside the kept one.
            state[path] = key
            kept.append(message)
    if kept:
        state[_UNDELIVERED] = json.dumps(kept[-_UNDELIVERED_LIMIT:], ensure_ascii=False)
    return state


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
