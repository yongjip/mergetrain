"""mergetrain exception hierarchy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|CREDENTIAL)[A-Z0-9_]*)=([^\s]+)"
)
_SENSITIVE_OPTION = re.compile(
    r"(?i)(--(?:token|secret|password|passwd|api[-_]?key|credential)(?:=|\s+))([^\s]+)"
)


def redact_secrets(text: str) -> str:
    """Mask inline secrets (``KEY=...``, ``--token ...``) in free-form text.

    The one place secret masking is defined, so every surface that may persist
    or display a command line or its output — a failed-gate job ``note``, the
    on-disk log, ``status --json``, the dashboard — masks the same way and a
    credential passed inline to a gate is never echoed in cleartext. Idempotent.
    """

    text = _SENSITIVE_ASSIGNMENT.sub(r"\1=[redacted]", text)
    text = _SENSITIVE_OPTION.sub(r"\1[redacted]", text)
    return text


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


class PushRejected(MergetrainError):
    """Raised when the remote rejects the deploy push for a permission/policy
    reason (protected branch, required pull request, denied ref update).

    This is a repo-configuration issue, not a bad-code failure, so the job is
    parked ``blocked`` (not ``failed``) and an agent can branch on
    error.code == "push_rejected"."""


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

    def __str__(self) -> str:
        if isinstance(self.command, str):
            rendered = self.command
        else:
            rendered = " ".join(str(part) for part in self.command)
        location = f" in {self.cwd}" if self.cwd else ""
        tail = self.stderr.strip() or self.stdout.strip()
        if tail:
            text = f"command failed ({self.returncode}){location}: {rendered}\n{tail}"
        else:
            text = f"command failed ({self.returncode}){location}: {rendered}"
        # Redact at the source: this string becomes the persisted job note that
        # `status --json` and the dashboard emit, so a gate invoked with an
        # inline credential must never leak it in cleartext.
        return redact_secrets(text)
