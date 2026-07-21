"""Foreground daemon loop for auto-approved jobs."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from typing import Any

from .models import Job
from .store import (
    claim_all_queued,
    connect,
    default_owner,
    deploy_reconcile_pending,
    has_queued_auto,
    release_runner_lock,
)

Say = Callable[[str], None]
ProcessBatch = Callable[[Any, list[Job]], object]


def daemon_tick(
    *,
    db_path: str,
    process_batch: ProcessBatch,
    owner: str,
    lock_ttl_minutes: int = 30,
    say: Say = print,
) -> str:
    """Run one auto-only pass over a single repo's queue.

    This is the daemon's whole per-tick policy in one reusable place — the
    hub daemon calls it per registered repo so auto-only claiming, the
    reconcile pause, and lease release cannot drift between the two paths.
    Returns ``"reconcile_paused"``, ``"processed:<n>"``, or ``"idle"``.
    Exceptions propagate; callers decide how to isolate them.
    """

    lease_token = ""
    conn = connect(db_path)
    try:
        pending = deploy_reconcile_pending(conn)
        if pending:
            # A crash left a possibly-landed push unresolved. The daemon
            # deploys to the same push refs, so it must not push over a
            # pending reconcile (0.3.0 Phase 2, decision Q4). Pause until
            # an operator runs `mergetrain reconcile --apply`.
            say(
                f"mergetrain daemon tick: {pending} job(s) pending reconcile; "
                "deploy paused (run 'mergetrain reconcile --apply')"
            )
            return "reconcile_paused"
        if has_queued_auto(conn):
            jobs = claim_all_queued(
                conn,
                owner=owner,
                ttl_minutes=lock_ttl_minutes,
                auto_only=True,
            )
            if jobs:
                lease_token = jobs[0].claim_token
                say(f"mergetrain daemon processing {len(jobs)} auto job(s)")
                process_batch(conn, jobs)
                return f"processed:{len(jobs)}"
            if deploy_reconcile_pending(conn):
                # The claim itself parked orphans as needs_reconcile and
                # refused to proceed (TOCTOU guard in claim_all_queued).
                say(
                    "mergetrain daemon tick: jobs pending reconcile; deploy "
                    "paused (run 'mergetrain reconcile --apply')"
                )
                return "reconcile_paused"
        say("mergetrain daemon tick: no auto-approved queued jobs")
        return "idle"
    finally:
        try:
            if lease_token:
                release_runner_lock(conn, owner=owner, token=lease_token)
        except Exception as exc:  # noqa: BLE001 - lease expires at TTL anyway
            say(
                "mergetrain daemon: failed to release runner lock "
                f"(lease expires at TTL): {exc}"
            )
        finally:
            conn.close()


def daemon_loop(
    *,
    db_path: str,
    process_batch: ProcessBatch,
    owner: str | None = None,
    interval_seconds: int = 15,
    lock_ttl_minutes: int = 30,
    once: bool = False,
    say: Say = print,
    install_signal_handlers: bool = True,
) -> None:
    """Run an auto-only mergetrain daemon loop.

    The daemon only claims jobs with ``auto_deploy = 1``. It does not decide
    whether a job is safe for unattended deployment; it trusts the enqueue-time
    ``--auto`` flag as the explicit approval boundary.
    """

    actual_owner = owner or default_owner()
    stop = threading.Event()

    def request_stop(signum, frame):  # type: ignore[no-untyped-def]
        stop.set()
        say(f"mergetrain daemon received signal {signum}; finishing current tick")

    old_handlers: dict[int, Any] = {}
    if install_signal_handlers:
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)

    try:
        while True:
            # Checked at the TOP of the loop: a signal that lands during the
            # inter-tick wait must never start one more tick (PEP 475 resumes
            # the wait after the handler returns, so the wait alone is not a
            # reliable exit point).
            if stop.is_set():
                break
            try:
                daemon_tick(
                    db_path=db_path,
                    process_batch=process_batch,
                    owner=actual_owner,
                    lock_ttl_minutes=lock_ttl_minutes,
                    say=say,
                )
            except Exception as exc:
                say(f"mergetrain daemon tick error: {exc}")
            if once or stop.is_set():
                break
            stop.wait(max(1, int(interval_seconds)))
    finally:
        if install_signal_handlers:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
