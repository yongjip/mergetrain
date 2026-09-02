"""Stable identities for deploy destinations and human-reviewed plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from .config import MergetrainConfig
from .git_destination import ResolvedGitDestination, resolve_git_destination
from .models import Job
from .reuse import gate_policy_sha, train_identity_sha


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deploy_destination_sha(config: MergetrainConfig) -> str:
    """Hash the exact Git destination without persisting remote credentials."""

    return resolve_git_destination(config).destination_sha


def _execution_policy_identity(
    config: MergetrainConfig,
    *,
    reuse_validated: bool,
) -> dict[str, Any]:
    """Return the configured QA/deploy policy that an approval authorizes."""

    return {
        "version": 1,
        "gate_policy_sha": gate_policy_sha(config),
        "reuse": {
            "authorized": bool(reuse_validated),
            "configured": config.deploy.reuse.enabled,
            "max_age_minutes": config.deploy.reuse.max_age_minutes,
            "on_mismatch": config.deploy.reuse.on_mismatch,
        },
        "verify": [
            {
                "name": hook.name,
                "run": hook.run,
                "always_rerun_on_deploy": hook.always_rerun_on_deploy,
            }
            for hook in config.deploy.verify
        ],
    }


def deploy_execution_policy_sha(
    config: MergetrainConfig,
    *,
    reuse_validated: bool = False,
) -> str:
    """Hash the configured gates, reuse authorization, and verify hooks."""

    return _sha256_json(
        _execution_policy_identity(config, reuse_validated=reuse_validated)
    )


def deploy_plan_sha(
    config: MergetrainConfig,
    jobs: Iterable[Job],
    *,
    reuse_validated: bool = False,
    destination: ResolvedGitDestination | None = None,
) -> str:
    """Hash the exact train, destination, and policy shown for approval."""

    ordered = list(jobs)
    return _sha256_json(
        {
            "version": 2,
            "train_identity_sha": train_identity_sha(ordered),
            "destination_sha": (
                destination or resolve_git_destination(config)
            ).destination_sha,
            "execution_policy": _execution_policy_identity(
                config,
                reuse_validated=reuse_validated,
            ),
        }
    )
