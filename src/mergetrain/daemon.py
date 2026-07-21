"""Foreground daemon loop for auto-approved jobs."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from typing import Any

from .errors import QueueError
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


def _grade_batch(results: object, claimed: int, say: Say) -> str:
    """Turn a processed batch into an honest outcome string.

    ``processed:<n>`` used to mean only "n jobs ran", but the notifier read it
    as "n landed" — so a sweep where every job blocked on a conflict reported a
    green deploy. Grade by what actually landed:
    ``landed:<n>`` (all n deployed) / ``partial:<d>/<n>`` (some) /
    ``no_landing:<n>`` (nothing deployed — blocked or failed).
    """

    statuses = [getattr(job, "status", "") for job in results] if isinstance(results, list) else []
    if not statuses:
        # A caller (or test double) that returns no inspectable results: fall
        # back to the neutral "ran" report rather than claim a landing.
        return f"processed:{claimed}"
    deployed = sum(1 for status in statuses if status == "deployed")
    total = len(statuses)
    if deployed == total:
        return f"landed:{deployed}"
    if deployed:
        say(f"mergetrain daemon: {deployed}/{total} landed, rest blocked/failed")
        return f"partial:{deployed}/{total}"
    say(f"mergetrain daemon: 0/{total} landed — jobs blocked or failed")
    return f"no_landing:{total}"


def daemon_tick(
    *,
    db_path: str,
    process_batch: ProcessBatch,
    owner: str,
    lock_ttl_minutes: int = 30,
    say: Say = print,
    sovereign: bool = False,
) -> str:
    """Run one auto-only pass over a single repo's queue.

    This is the daemon's whole per-tick policy in one reusable place — the
    hub daemon calls it per registered repo so auto-only claiming, the
    reconcile pause, and lease release cannot drift between the two paths.
    Returns ``"reconcile_paused"``, ``"processed:<n>"``, or ``"idle"``.
    Exceptions propagate; callers decide how to isolate them.

    Policy probes run on a read-only connection first, so an idle tick never
    creates, migrates, or otherwise writes the queue database. Only when
    there is actual auto work does the tick open a writable connection.
    ``sovereign`` marks a daemon running inside its own repo: it may create
    a missing database and migrate an old schema (first run). The hub sweeps
    other people's repos and must leave both to a runner invoked in-repo.
    """

    lease_token = ""
    probe_failed: QueueError | None = None
    try:
        probe = connect(db_path, read_only=True)
    except QueueError as exc:
        if not sovereign:
            raise
        # First run (no database) or pending migration: the repo's own
        # daemon is allowed to create/migrate below.
        probe_failed = exc
    if probe_failed is None:
        try:
            pending = deploy_reconcile_pending(probe)
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
            has_work = has_queued_auto(probe)
        finally:
            probe.close()
        if not has_work:
            say("mergetrain daemon tick: no auto-approved queued jobs")
            return "idle"
    conn = connect(db_path)
    try:
        pending = deploy_reconcile_pending(conn)
        if pending:
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
                results = process_batch(conn, jobs)
                return _grade_batch(results, len(jobs), say)
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
                    # The single-repo daemon runs inside its own repo and may
                    # create/migrate its own queue database.
                    sovereign=True,
                )
            except Exception as exc:
                say(f"mergetrain daemon tick error: {exc}")
            if once or stop.is_set():
                break
            stop.wait(max(1, int(interval_seconds)))
    finally:
        if install_signal_handlers:
            for saved_signum, saved_handler in old_handlers.items():
                signal.signal(saved_signum, saved_handler)
