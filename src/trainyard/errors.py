"""trainyard exception hierarchy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


class TrainyardError(Exception):
    """Base class for expected trainyard failures."""


class ConfigError(TrainyardError):
    """Raised when configuration cannot be loaded or validated."""


class QueueError(TrainyardError):
    """Raised for queue and lock errors."""


class LockHeld(QueueError):
    """Raised when another runner owns the queue lock."""


class MergeBlocked(TrainyardError):
    """Raised when a task branch cannot be merged into the integration train."""


@dataclass(slots=True)
class CommandFailed(TrainyardError):
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
