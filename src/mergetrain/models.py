"""Core data models for mergetrain."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from typing import Any

from .errors import redact_and_bound

ACTIVE_STATUSES = ("queued", "in_progress", "blocked", "failed", "validated", "needs_reconcile")
TERMINAL_STATUSES = ("deployed", "canceled")
ALL_STATUSES = ACTIVE_STATUSES + TERMINAL_STATUSES
PUSH_STATUSES = ("not_run", "pending", "succeeded", "failed")
VERIFY_STATUSES = ("not_run", "not_configured", "succeeded", "failed", "unknown")


def public_owner(owner: str) -> str:
    """A runner owner without its OS username: ``user:pid`` becomes ``local:pid``."""

    return f"local:{owner.rsplit(':', 1)[-1]}"


@dataclass(slots=True)
class Job:
    id: int
    task: str
    branch: str
    worktree_path: str = ""
    status: str = "queued"
    base_sha: str = ""
    head_sha: str = ""
    deploy_sha: str = ""
    requested_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    log_path: str = ""
    note: str = ""
    push_status: str = "not_run"
    verify_status: str = "not_run"
    auto_deploy: bool = False
    approval_destination_sha: str = ""
    approval_execution_policy_sha: str = ""
    train_id: str = ""
    train_size: int = 0
    validated_at: str = ""
    validation_base_sha: str = ""
    validation_sha: str = ""
    validated_head_sha: str = ""
    validation_tree_sha: str = ""
    validation_gate_policy_sha: str = ""
    validation_environment_sha: str = ""
    validation_train_sha: str = ""
    reused_validation_sha: str = ""
    claim_token: str = ""
    cancel_requested_at: str = ""
    pending_deploy_sha: str = ""
    conflict_with: str = ""
    pending_deploy_remote: str = ""
    pending_deploy_refs: str = ""
    pending_deploy_destination_sha: str = ""
    deployment_id: str = ""
    deployment_destination_sha: str = ""
    verification_policy_sha: str = ""
    supersession_id: str = ""
    supersedes_train_id: str = ""

    @classmethod
    def from_row(cls, row: Any) -> Job:
        values: dict[str, Any] = {}
        for name, coerce, default in _JOB_COLUMNS:
            value = row[name]
            # Required columns never fall back: a NULL status must not read as
            # 'queued' while SQL disagrees about the row.
            values[name] = coerce(value) if name in _REQUIRED_JOB_COLUMNS else coerce(value or default)
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        data = {
            name: value
            for name, value in asdict(self).items()
            if name not in _INTERNAL_JOB_FIELDS
        }
        data["auto_deploy"] = bool(self.auto_deploy)
        data["note"], data["note_truncated"] = redact_and_bound(self.note)
        return data


# Each Job field is a deploy_queue column of the same name, coerced by its
# annotation; an unsupported annotation fails at import time.
_COERCIONS: dict[str, Callable[[Any], Any]] = {"int": int, "str": str, "bool": bool}
_JOB_COLUMNS = tuple(
    (field.name, _COERCIONS[str(field.type)], field.default) for field in fields(Job)
)
_REQUIRED_JOB_COLUMNS = frozenset({"id", "task", "branch", "status"})
# Recovery and approval bookkeeping that is not part of the public job surface
# (keeps the contract fingerprint stable).
_INTERNAL_JOB_FIELDS = frozenset(
    {
        "claim_token",
        "pending_deploy_remote",
        "pending_deploy_refs",
        "pending_deploy_destination_sha",
        "approval_destination_sha",
        "approval_execution_policy_sha",
        "deployment_id",
        "deployment_destination_sha",
        "verification_policy_sha",
    }
)


@dataclass(slots=True)
class RunnerLock:
    name: str
    owner: str
    worktree_path: str = ""
    head_sha: str = ""
    acquired_at: str = ""
    heartbeat_at: str = ""
    expires_at: str = ""
    token: str = ""
    liveness: str = "unknown"

    @classmethod
    def from_row(cls, row: Any, *, liveness: str = "unknown") -> RunnerLock:
        return cls(
            name=str(row["name"]),
            owner=str(row["owner"]),
            worktree_path=str(row["worktree_path"] or ""),
            head_sha=str(row["head_sha"] or ""),
            acquired_at=str(row["acquired_at"] or ""),
            heartbeat_at=str(row["heartbeat_at"] or row["acquired_at"] or ""),
            expires_at=str(row["expires_at"] or ""),
            token=str(row["token"] or ""),
            liveness=liveness,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("token", None)
        return data


@dataclass(slots=True)
class RunEvent:
    """A structured, append-only observation of runner progress."""

    id: int
    phase: str
    state: str
    message: str
    created_at: str
    job_id: int | None = None
    detail: str = ""
    claim_token: str = ""

    @classmethod
    def from_row(cls, row: Any) -> RunEvent:
        return cls(
            id=int(row["id"]),
            phase=str(row["phase"]),
            state=str(row["state"]),
            message=str(row["message"]),
            created_at=str(row["created_at"]),
            job_id=int(row["job_id"]) if row["job_id"] is not None else None,
            detail=str(row["detail"] or ""),
            claim_token=str(row["claim_token"] or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("claim_token", None)
        return data


@dataclass(slots=True)
class RecoveryOperationEvent:
    """One append-only event in a recovery-command invocation ledger."""

    id: int
    invocation_id: str
    operation: str
    state: str
    applied: bool
    detail: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> RecoveryOperationEvent:
        return cls(
            id=int(row["id"]),
            invocation_id=str(row["invocation_id"] or ""),
            operation=str(row["operation"]),
            state=str(row["state"]),
            applied=bool(row["applied"]),
            detail=str(row["detail"] or ""),
            created_at=str(row["created_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("invocation_id", None)
        return data
