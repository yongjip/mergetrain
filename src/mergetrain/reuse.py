"""Safety identity primitives for opt-in validated-gate reuse."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .config import MergetrainConfig
from .models import Job


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def gate_policy_sha(config: MergetrainConfig) -> str:
    """Hash the semantic inputs that define the pre-push gate policy."""

    return _sha256_json(
        {
            "version": 1,
            "project": config.project.name,
            "integration_ref": config.git.integration_ref,
            "built_in_gates": [
                {
                    "name": "diff-check",
                    "run": "git diff --check ${integration_ref}..HEAD",
                    "always_rerun_on_deploy": False,
                }
            ],
            "gates": [
                {
                    "name": gate.name,
                    "run": gate.run,
                    "always_rerun_on_deploy": gate.always_rerun_on_deploy,
                }
                for gate in config.gates
            ],
            "environment_fingerprints": [
                {"name": item.name, "run": item.run}
                for item in config.deploy.reuse.fingerprints
            ],
        }
    )


def environment_sha(values: Iterable[tuple[str, str]]) -> str:
    """Hash opaque adapter-provided fingerprint values without persisting them."""

    return _sha256_json(
        [{"name": name, "value": value} for name, value in values]
    )


def train_identity_sha(
    jobs: Iterable[Job],
    *,
    train_id: str | None = None,
    train_size: int | None = None,
    validated_heads: dict[int, str] | None = None,
) -> str:
    ordered = list(jobs)
    resolved_train_id = train_id if train_id is not None else (
        ordered[0].train_id if ordered else ""
    )
    resolved_train_size = train_size if train_size is not None else (
        ordered[0].train_size if ordered else 0
    )
    return _sha256_json(
        {
            "version": 1,
            "train_id": resolved_train_id,
            "train_size": resolved_train_size,
            "members": [
                {
                    "job_id": job.id,
                    "task": job.task,
                    "branch": job.branch,
                    "validated_head_sha": (
                        validated_heads[job.id]
                        if validated_heads is not None
                        else job.validated_head_sha
                    ),
                }
                for job in ordered
            ],
        }
    )


def validation_age_minutes(validated_at: str, *, now: datetime | None = None) -> float:
    if not validated_at:
        return float("inf")
    try:
        parsed = datetime.fromisoformat(validated_at.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age = (current - parsed.astimezone(timezone.utc)).total_seconds() / 60
    return age if age >= 0 else float("inf")


@dataclass(frozen=True, slots=True)
class ReuseDecision:
    authorized: bool
    eligible: bool
    action: str
    validation_sha: str
    reused_validation_sha: str = ""
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reasons"] = list(self.reasons)
        return data
