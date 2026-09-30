"""Machine-wide auto-only daemon: one scheduler over every registered repo.

Phase 1 of RFC #23. The hub daemon owns no queue state and adds no new
execution semantics: every repo is processed by the same ``daemon_tick`` the
single-repo daemon runs, against that repo's own SQLite database, lock, and
gates. What the hub adds is *scheduling* — which repos get a turn, and how
many may run their gates at the same time on this machine (``concurrency``,
default 1, so heavy gates from different repos never stack).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import CONFIG_VERSION, MergetrainConfig, load_config, shared_state_root
from .daemon import ProcessBatch, Say, daemon_tick, handling_stop_signals
from .deploy_plan import deploy_destination_sha, deploy_execution_policy_sha
from .hub import display_path
from .notify import (
    Notifier,
    deliver_notifications,
    load_notify_state,
    save_notify_state,
)
from .persistence.leases import default_owner
from .registry import load_registry, same_repo

ProcessBatchFactory = Callable[[MergetrainConfig, str], ProcessBatch]
NotifierResolver = Callable[[str, str], Notifier | None]


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


def _path_key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _queue_keys(raw: str) -> set[str]:
    """Every identity under which a registered path reaches a queue.

    The shared state root needs only the filesystem, so it is known even when
    the config cannot be loaded; the configured database also covers a queue
    moved with ``state.db``.
    """

    if not raw:
        return set()
    try:
        keys = {_path_key(shared_state_root(raw))}
    except OSError:
        return set()
    try:
        keys.add(_path_key(load_config(repo=raw).state.db))
    except Exception:  # noqa: BLE001 - an unreadable config still has a state root
        pass
    return keys


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
    outcome is ``landed:<n>``/``unverified:<n>``/``partial:<d>/<n>``/``no_landing:<n>``/
    ``idle``/``reconcile_paused``/``skipped``/``excluded``/``error``.
    """

    factory = process_batch_factory or _default_factory(keep_worktree)
    excluded_paths = [
        str(item.get("path") or "")
        for item in registered
        if not item.get("daemon", True)
    ]
    # A linked worktree is a different directory that reaches the same queue,
    # so the opt-out and de-duplication follow the queue, not the path (#229).
    queue_keys = [_queue_keys(str(item.get("path") or "")) for item in registered]
    excluded_queues: set[str] = set().union(
        *(
            keys
            for item, keys in zip(registered, queue_keys, strict=True)
            if not item.get("daemon", True)
        )
    )
    first_entry: dict[str, int] = {}
    duplicate_of: dict[int, str] = {}
    for index, keys in enumerate(queue_keys):
        earlier = sorted({first_entry[key] for key in keys if key in first_entry})
        if earlier:
            duplicate_of[index] = str(registered[earlier[0]].get("path") or "")
        for key in keys:
            first_entry.setdefault(key, index)

    def excluded_by_alias(raw: str, keys: set[str]) -> bool:
        # Belt-and-braces for the `--no-daemon` guarantee: if ANY roster entry
        # naming the same physical directory or the same queue is excluded
        # (case aliases on macOS, symlinks, linked worktrees, historical
        # duplicates), this entry is excluded too. Do not skip equal strings:
        # an exact hand-edited duplicate can carry a conflicting daemon flag
        # just as an aliased duplicate can.
        return bool(keys & excluded_queues) or any(
            same_repo(other, raw) for other in excluded_paths
        )

    def tick_one(index: int) -> dict[str, Any]:
        item = registered[index]
        raw = str(item.get("path") or "")
        out: dict[str, Any] = {"path": display_path(raw)}
        if not item.get("daemon", True) or excluded_by_alias(raw, queue_keys[index]):
            # Policy-level opt-out (`hub add --no-daemon`): this repo stays in
            # `hub status` but is never swept, regardless of any --auto jobs.
            out.update(ok=True, outcome="excluded", error="daemon excluded by registry flag")
            return out
        if index in duplicate_of:
            # One queue gets one turn per sweep, however many paths reach it.
            out.update(
                ok=True,
                outcome="skipped",
                error=f"same queue as {display_path(duplicate_of[index])}",
            )
            return out
        # Same isolation contract as `hub status`: any failure in one
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
                approval_destination_sha=lambda: deploy_destination_sha(config),
                approval_execution_policy_sha=lambda: deploy_execution_policy_sha(
                    config
                ),
            )
            out.update(ok=True, outcome=outcome)
            return out
        except Exception as exc:  # noqa: BLE001 - per-repo isolation is the contract
            out.update(ok=False, outcome="error", error=str(exc) or exc.__class__.__name__)
            return out

    indexes = range(len(registered))
    if concurrency <= 1:
        return [tick_one(index) for index in indexes]
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(tick_one, indexes))


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
    notifier_resolver: NotifierResolver | None = None,
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

    def deliver(path: str, key: str, title: str, message: str) -> None:
        delivery = notifier_resolver(path, key) if notifier_resolver is not None else None
        if delivery is not None:
            delivery(title, message)

    outcomes: list[dict[str, Any]] = []
    with handling_stop_signals(request_stop, install=install_signal_handlers):
        # Persisted across invocations so --once/cron mode does not re-notify
        # every persistent error on every run, and a restart resumes dedup.
        last_outcomes: dict[str, str] = (
            load_notify_state(registry) if notifier_resolver is not None else {}
        )
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
                    if notifier_resolver is not None:
                        last_outcomes = deliver_notifications(
                            outcomes,
                            last_outcomes,
                            deliver,
                            on_error=lambda exc: say(f"mergetrain hub notify error: {exc}"),
                        )
                        save_notify_state(last_outcomes, registry)
                else:
                    outcomes = []
                    say("mergetrain hub sweep: no repos registered")
            except Exception as exc:
                say(f"mergetrain hub sweep error: {exc}")
            if once or stop.is_set():
                break
            stop.wait(max(1, int(interval_seconds)))
    return outcomes
