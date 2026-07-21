"""mergetrain exception hierarchy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


class MergetrainError(Exception):
    """Base class for expected mergetrain failures."""


class ConfigError(MergetrainError):
    """Raised when configuration cannot be loaded or validated."""


class QueueError(MergetrainError):
    """Raised for queue and lock errors."""


class LockHeld(QueueError):
    """Raised when another runner owns the queue lock."""


class LostLease(QueueError):
    """Raised when a runner no longer owns the lease it was given."""


class DuplicateActiveBranch(QueueError):
    """Raised when a branch already has a non-terminal job in the queue.

    Distinct from a generic queue error so an agent can branch on
    error.code == "duplicate_active_branch" and take the documented escape
    (cancel the superseded job, or re-enqueue with --allow-duplicate)."""


class RemoteUnreachable(MergetrainError):
    """Raised when reconcile cannot reach the remote to establish deploy truth.

    Recovery treats this as a strict no-op: no job state is finalized so a later
    reconcile against a reachable remote is still the only thing that writes
    ``deployed``/``queued``/``blocked`` (0.3.0 Phase 2).
    """


class CancellationRequested(MergetrainError):
    """Raised when the active train has been asked to stop."""


class MergeBlocked(MergetrainError):
    """Raised when a task branch cannot be merged into the integration train."""


@dataclass(slots=True)
class CommandFailed(MergetrainError):
    """A subprocess returned a non-zero exit code."""

    command: Sequence[str] | str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    cwd: str | None = None

    def __str__(self) -> str:  # pragma: no cover - formatting only
        if isinstance(self.command, str):
            rendered = self.command
        else:
            rendered = " ".join(str(part) for part in self.command)
        location = f" in {self.cwd}" if self.cwd else ""
        tail = self.stderr.strip() or self.stdout.strip()
        if tail:
            return f"command failed ({self.returncode}){location}: {rendered}\n{tail}"
        return f"command failed ({self.returncode}){location}: {rendered}"
