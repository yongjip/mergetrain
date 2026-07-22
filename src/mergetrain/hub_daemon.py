"""Machine-wide auto-only daemon: one scheduler over every registered repo.

Phase 1 of RFC #23. The hub daemon owns no queue state and adds no new
execution semantics: every repo is processed by the same ``daemon_tick`` the
single-repo daemon runs, against that repo's own SQLite database, lock, and
gates. What the hub adds is *scheduling* — which repos get a turn, and how
many may run their gates at the same time on this machine (``concurrency``,
default 1, so heavy gates from different repos never stack).
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import CONFIG_VERSION, MergetrainConfig, load_config
from .daemon import ProcessBatch, Say, daemon_tick
from .hub import display_path
from .notify import (
    Notifier,
    load_notify_state,
    save_notify_state,
    sweep_notifications,
)
from .registry import load_registry, same_repo
from .store import default_owner

ProcessBatchFactory = Callable[[MergetrainConfig, str], ProcessBatch]


def _default_factory(keep_worktree: bool) -> ProcessBatchFactory:
    def factory(config: MergetrainConfig, owner: str) -> ProcessBatch:
        from .git_runner import GitRunner

        runner = GitRunner(config)

        def process_batch(conn: Any, jobs: list) -> object:
            return runner.process_batch(
                conn,
                jobs,
                deploy=True,
                keep_worktree=keep_worktree,
                owner=owner,
                ttl_minutes=config.queue.lock_ttl_minutes,
            )

        return process_batch

    return factory


def hub_sweep(
    registered: list[dict[str, Any]],
    *,
    concurrency: int = 1,
    keep_worktree: bool = False,
    say: Say = print,
    process_batch_factory: ProcessBatchFactory | None = None,
) -> list[dict[str, Any]]:
    """Run one auto-only pass over every registered repo.

    At most ``concurrency`` repos run at a time; each repo's outcome is
    isolated, so one broken repo never stops the sweep. Returns one outcome
    dict per repo: ``{"path", "name"?, "ok", "outcome", "error"?}`` where
    outcome is ``landed:<n>``/``partial:<d>/<n>``/``no_landing:<n>``/``idle``/``reconcile_paused``/``skipped``/
    ``excluded``/``error``.
    """

    factory = process_batch_factory or _default_factory(keep_worktree)
    excluded_paths = [
        str(item.get("path") or "")
        for item in registered
        if not item.get("daemon", True)
    ]

    def excluded_by_alias(raw: str) -> bool:
        # Belt-and-braces for the `--no-daemon` guarantee: if ANY roster entry
        # naming the same physical directory is excluded (case aliases on
        # macOS, symlinks, historical duplicates), this entry is excluded too.
        # Do not skip equal strings: an exact hand-edited duplicate can carry a
        # conflicting daemon flag just as an aliased duplicate can.
        return any(same_repo(other, raw) for other in excluded_paths)

    def tick_one(item: dict[str, Any]) -> dict[str, Any]:
        raw = str(item.get("path") or "")
        out: dict[str, Any] = {"path": display_path(raw)}
        if not item.get("daemon", True) or excluded_by_alias(raw):
            # Policy-level opt-out (`hub add --no-daemon`): this repo stays on
            # the dashboard but is never swept, regardless of any --auto jobs.
            out.update(ok=True, outcome="excluded", error="daemon excluded by registry flag")
            return out
        # Same isolation contract as the hub dashboard: any failure in one
        # repo becomes that repo's error outcome, so the catch is broad.
        try:
            repo = Path(raw)
            if not repo.is_dir():
                out.update(ok=False, outcome="error", error="repo directory is missing")
                return out
            config = load_config(repo=repo)
            out["name"] = config.project.name
            if not config.config_exists:
                out.update(ok=False, outcome="error", error="no .mergetrain.yaml in this repo")
                return out
            if config.config_version > CONFIG_VERSION:
                # An older hub binary must never deploy a repo whose config it
                # cannot read; report and skip, like a missing config (#84, defect 6).
                out.update(
                    ok=False,
                    outcome="error",
                    error=(
                        f"config version {config.config_version} is newer than this "
                        f"mergetrain (supports {CONFIG_VERSION}); upgrade before deploying"
                    ),
                )
                return out
            if not Path(config.state.db).is_file():
                # No queue database means no auto work can exist — and the
                # scheduler must not create the database to find out.
                out.update(ok=True, outcome="skipped", error="no queue database yet")
                return out
            owner = default_owner()
            outcome = daemon_tick(
                db_path=str(config.state.db),
                process_batch=factory(config, owner),
                owner=owner,
                lock_ttl_minutes=config.queue.lock_ttl_minutes,
                say=lambda message: say(f"[{config.project.name}] {message}"),
            )
            out.update(ok=True, outcome=outcome)
            return out
        except Exception as exc:  # noqa: BLE001 - per-repo isolation is the contract
            out.update(ok=False, outcome="error", error=str(exc) or exc.__class__.__name__)
            return out

    if concurrency <= 1:
        return [tick_one(item) for item in registered]
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(tick_one, registered))


def hub_daemon_loop(
    *,
    registry: str | None = None,
    interval_seconds: int = 15,
    concurrency: int = 1,
    keep_worktree: bool = False,
    once: bool = False,
    say: Say = print,
    install_signal_handlers: bool = True,
    process_batch_factory: ProcessBatchFactory | None = None,
    notifier: Notifier | None = None,
) -> list[dict[str, Any]]:
    """Sweep every registered repo on an interval until stopped.

    The registry is re-read on every sweep so ``hub add``/``hub remove``
    take effect live. Returns the outcomes of the final sweep (useful with
    ``once``).
    """

    stop = threading.Event()

    def request_stop(signum, frame):  # type: ignore[no-untyped-def]
        stop.set()
        say(f"mergetrain hub daemon received signal {signum}; finishing current sweep")

    old_handlers: dict[int, Any] = {}
    if install_signal_handlers:
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)

    outcomes: list[dict[str, Any]] = []
    # Persisted across invocations so --once/cron mode does not re-notify
    # every persistent error on every run, and a restart resumes dedup.
    last_outcomes: dict[str, str] = (
        load_notify_state(registry) if notifier is not None else {}
    )
    try:
        while True:
            # Top-of-loop check: a signal landing during the inter-sweep wait
            # must never trigger one more full (deploying) sweep — PEP 475
            # resumes the wait after the handler returns, so the wait alone
            # is not a reliable exit point.
            if stop.is_set():
                break
            try:
                registered = load_registry(registry)
                if registered:
                    outcomes = hub_sweep(
                        registered,
                        concurrency=concurrency,
                        keep_worktree=keep_worktree,
                        say=say,
                        process_batch_factory=process_batch_factory,
                    )
                    processed = sum(
                        1
                        for item in outcomes
                        if str(item.get("outcome", "")).split(":", 1)[0]
                        in {"landed", "partial", "no_landing", "processed"}
                    )
                    say(
                        f"mergetrain hub sweep: {len(outcomes)} repo(s), "
                        f"{processed} with work processed"
                    )
                    if notifier is not None:
                        messages, settled = sweep_notifications(outcomes, last_outcomes)
                        # Commit no-delivery outcomes immediately; a message's
                        # key is committed only once its notifier succeeds, so
                        # a failed delivery is retried next sweep instead of
                        # being silently marked as already-notified.
                        next_state = dict(settled)
                        for path, key, title, message in messages:
                            try:
                                notifier(title, message)
                                next_state[path] = key
                            except Exception as exc:  # noqa: BLE001 - never break a sweep
                                say(f"mergetrain hub notify error: {exc}")
                        last_outcomes = next_state
                        save_notify_state(last_outcomes, registry)
                else:
                    outcomes = []
                    say("mergetrain hub sweep: no repos registered")
            except Exception as exc:
                say(f"mergetrain hub sweep error: {exc}")
            if once or stop.is_set():
                break
            stop.wait(max(1, int(interval_seconds)))
    finally:
        if install_signal_handlers:
            for saved_signum, saved_handler in old_handlers.items():
                signal.signal(saved_signum, saved_handler)
    return outcomes
