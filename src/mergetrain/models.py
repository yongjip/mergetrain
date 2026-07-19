"""Core data models for mergetrain."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

ACTIVE_STATUSES = ("queued", "in_progress", "blocked", "failed", "validated")
TERMINAL_STATUSES = ("deployed", "canceled")
ALL_STATUSES = ACTIVE_STATUSES + TERMINAL_STATUSES
PUSH_STATUSES = ("not_run", "succeeded", "failed")
VERIFY_STATUSES = ("not_run", "not_configured", "succeeded", "failed")


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

    @classmethod
    def from_row(cls, row: Any) -> "Job":
        return cls(
            id=int(row["id"]),
            task=str(row["task"]),
            branch=str(row["branch"]),
            worktree_path=str(row["worktree_path"] or ""),
            status=str(row["status"]),
            base_sha=str(row["base_sha"] or ""),
            head_sha=str(row["head_sha"] or ""),
            deploy_sha=str(row["deploy_sha"] or ""),
            requested_at=str(row["requested_at"] or ""),
            started_at=str(row["started_at"] or ""),
            finished_at=str(row["finished_at"] or ""),
            log_path=str(row["log_path"] or ""),
            note=str(row["note"] or ""),
            push_status=str(row["push_status"] or "not_run"),
            verify_status=str(row["verify_status"] or "not_run"),
            auto_deploy=bool(row["auto_deploy"]),
            train_id=str(row["train_id"] or ""),
            train_size=int(row["train_size"] or 0),
            validated_at=str(row["validated_at"] or ""),
            validation_base_sha=str(row["validation_base_sha"] or ""),
            validation_sha=str(row["validation_sha"] or ""),
            validated_head_sha=str(row["validated_head_sha"] or ""),
            validation_tree_sha=str(row["validation_tree_sha"] or ""),
            validation_gate_policy_sha=str(
                row["validation_gate_policy_sha"] or ""
            ),
            validation_environment_sha=str(
                row["validation_environment_sha"] or ""
            ),
            validation_train_sha=str(row["validation_train_sha"] or ""),
            reused_validation_sha=str(row["reused_validation_sha"] or ""),
            claim_token=str(row["claim_token"] or ""),
            cancel_requested_at=str(row["cancel_requested_at"] or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["auto_deploy"] = bool(self.auto_deploy)
        data.pop("claim_token", None)
        return data


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
    def from_row(cls, row: Any, *, liveness: str = "unknown") -> "RunnerLock":
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
    def from_row(cls, row: Any) -> "RunEvent":
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
