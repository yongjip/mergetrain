"""Know whether a deploy push outlived the runner that started it (#220).

Managed commands run in their own process group (a Job Object on Windows), so
a runner killed mid-push leaves ``git push`` running, and the push can still
land after recovery has read the remote. Recovery therefore has to know that a
push is still going before it trusts what the remote says.

On POSIX the runner takes an exclusive ``flock`` before it pushes and hands the
descriptor to ``git push``. The push's own processes inherit it -- the local
``receive-pack`` and its hooks included -- so the lock is released only when
the runner and every process of the push have exited. On Windows the push's
Job Object carries a name derived from the commit, and the job lives on while
any process in it runs.

Network remotes keep one gap this cannot close: a server that already received
the whole push may still apply it after the client has died.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from .config import MergetrainConfig
from .errors import AmbiguousPush
from .windows_job import named_job_active

if os.name == "posix":
    import fcntl


def push_lock_path(config: MergetrainConfig, deploy_sha: str) -> Path:
    return config.state.db.parent / "push-locks" / f"{deploy_sha}.lock"


def push_job_name(deploy_sha: str) -> str:
    return f"Local\\mergetrain-push-{deploy_sha}"


def _lock_is_free(path: Path) -> bool:
    """Take and drop the lock; remove the file when nothing holds it."""

    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        with suppress(FileNotFoundError):
            path.unlink()
        return True
    finally:
        os.close(fd)


def push_in_flight(config: MergetrainConfig, deploy_sha: str) -> bool:
    """Whether a process of an earlier push of ``deploy_sha`` is still running."""

    if not deploy_sha:
        return False
    if os.name != "posix":  # pragma: no cover - Windows compatibility
        return named_job_active(push_job_name(deploy_sha))
    return not _lock_is_free(push_lock_path(config, deploy_sha))


@contextmanager
def holding_push_lock(config: MergetrainConfig, deploy_sha: str) -> Iterator[tuple[int, ...]]:
    """Hold the push lock and yield the descriptors the push must inherit.

    Raises ``AmbiguousPush`` when an earlier push of the same commit is still
    running: that push may yet land, so only the remote can settle the job.
    """

    refusal = AmbiguousPush(
        f"an earlier push of {deploy_sha[:12]} is still running, so it may yet "
        "land; this push was not started -- parked for reconcile"
    )
    if os.name != "posix":  # pragma: no cover - Windows compatibility
        if named_job_active(push_job_name(deploy_sha)):
            raise refusal
        yield ()
        return
    path = push_lock_path(config, deploy_sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise refusal from None
        yield (fd,)
    finally:
        os.close(fd)
        # Only a push that fully exited releases the lock; one of its processes
        # that survived the stop keeps the file, and reconcile keeps waiting.
        _lock_is_free(path)
