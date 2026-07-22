"""mergetrain exception hierarchy."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|CREDENTIAL)[A-Z0-9_]*)=([^\s]+)"
)
_SENSITIVE_OPTION = re.compile(
    r"(?i)(--(?:token|secret|password|passwd|api[-_]?key|credential)(?:=|\s+))([^\s]+)"
)
_URL_PASSWORD = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@"
)


def redact_secrets(text: str) -> str:
    """Mask inline secrets (``KEY=...``, ``--token ...``) in free-form text.

    The one place secret masking is defined, so every surface that may persist
    or display a command line or its output — a failed-gate job ``note``, the
    on-disk log, ``status --json``, the dashboard — masks the same way and a
    credential passed inline to a gate is never echoed in cleartext. Idempotent.
    """

    text = _URL_PASSWORD.sub(r"\1\2:[redacted]@", text)
    text = _SENSITIVE_ASSIGNMENT.sub(r"\1=[redacted]", text)
    text = _SENSITIVE_OPTION.sub(r"\1[redacted]", text)
    return text


class MergetrainError(Exception):
    """Base class for expected mergetrain failures."""

    def __str__(self) -> str:
        # Expected errors often include subprocess stderr (notably push
        # classification errors). Mask once at the exception boundary before a
        # message can be persisted in a job note or emitted by a JSON command.
        return redact_secrets(super().__str__())


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


class AmbiguousPush(MergetrainError):
    """Raised when the atomic push fails for a NON-rejection reason (transport
    drop, timeout) after the write-ahead marker was already recorded.

    The remote may or may not have accepted the refs, so the outcome is
    ambiguous. The job is parked ``needs_reconcile`` (not ``failed``) with its
    marker preserved, so a later ``reconcile`` establishes remote truth and every
    deploy entrypoint refuses in the meantime — the exactly-once invariant
    (guarantee #4) must never re-push over a ref that may already have advanced.
    """


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
