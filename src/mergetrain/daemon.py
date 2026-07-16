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
    """Run an auto-only mergetrain daemon loop.

    The daemon only claims jobs with ``auto_deploy = 1``. It does not decide
    whether a job is safe for unattended deployment; it trusts the enqueue-time
    ``--auto`` flag as the explicit approval boundary.
    """

    actual_owner = owner or default_owner()
    should_stop = False

    def request_stop(signum, frame):  # type: ignore[no-untyped-def]
        nonlocal should_stop
        should_stop = True
        say(f"mergetrain daemon received signal {signum}; finishing current tick")

    old_handlers: dict[int, Any] = {}
    if install_signal_handlers:
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)

    try:
        while True:
            lease_token = ""
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
                            lease_token = jobs[0].claim_token
                            say(f"mergetrain daemon processing {len(jobs)} auto job(s)")
                            process_batch(conn, jobs)
                    else:
                        say("mergetrain daemon tick: no auto-approved queued jobs")
                finally:
                    if lease_token:
                        release_runner_lock(conn, owner=actual_owner, token=lease_token)
                    conn.close()
            except Exception as exc:
                say(f"mergetrain daemon tick error: {exc}")
            if once or should_stop:
                break
            time.sleep(max(1, int(interval_seconds)))
    finally:
        if install_signal_handlers:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
