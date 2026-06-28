"""Foreground daemon loop for auto-approved jobs."""

from __future__ import annotations

import signal
import time
from collections.abc import Callable
from typing import Any

from .models import Job
from .store import claim_all_queued, connect, default_owner, has_queued_auto, release_runner_lock

Say = Callable[[str], None]
ProcessBatch = Callable[[Any, list[Job]], object]


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
    """Run an auto-only trainyard daemon loop.

    The daemon only claims jobs with ``auto_deploy = 1``. It does not decide
    whether a job is safe for unattended deployment; it trusts the enqueue-time
    ``--auto`` flag as the explicit approval boundary.
    """

    actual_owner = owner or default_owner()
    should_stop = False

    def request_stop(signum, frame):  # type: ignore[no-untyped-def]
        nonlocal should_stop
        should_stop = True
        say(f"trainyard daemon received signal {signum}; finishing current tick")

    old_handlers: dict[int, Any] = {}
    if install_signal_handlers:
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)

    try:
        while True:
            try:
                conn = connect(db_path)
                try:
                    if has_queued_auto(conn):
                        jobs = claim_all_queued(
                            conn,
                            owner=actual_owner,
                            ttl_minutes=lock_ttl_minutes,
                            auto_only=True,
                        )
                        if jobs:
                            say(f"trainyard daemon processing {len(jobs)} auto job(s)")
                            process_batch(conn, jobs)
                    else:
                        say("trainyard daemon tick: no auto-approved queued jobs")
                finally:
                    release_runner_lock(conn, owner=actual_owner)
                    conn.close()
            except Exception as exc:
                say(f"trainyard daemon tick error: {exc}")
                try:
                    conn = connect(db_path)
                    try:
                        release_runner_lock(conn, owner=actual_owner)
                    finally:
                        conn.close()
                except Exception:
                    pass
            if once or should_stop:
                break
            time.sleep(max(1, int(interval_seconds)))
    finally:
        if install_signal_handlers:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
