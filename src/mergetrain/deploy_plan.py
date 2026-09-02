"""Stable identities for deploy destinations and human-reviewed plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from .config import MergetrainConfig
from .errors import redact_secrets
from .git_ops import DEPLOY_AUDIT_REF_PREFIX, git_remote_url
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

    remote_url = redact_secrets(git_remote_url(config.repo, config.git.remote))
    return _sha256_json(
        {
            "version": 1,
            "remote": config.git.remote,
            "remote_url": remote_url,
            "integration_ref": config.git.integration_ref,
            "push_refs": list(config.git.push_refs),
            "audit_ref_prefix": DEPLOY_AUDIT_REF_PREFIX,
        }
    )


def deploy_plan_sha(
    config: MergetrainConfig,
    jobs: Iterable[Job],
    *,
    reuse_validated: bool = False,
) -> str:
    """Hash the exact train, destination, and policy shown for approval."""

    ordered = list(jobs)
    return _sha256_json(
        {
            "version": 1,
            "train_identity_sha": train_identity_sha(ordered),
            "destination_sha": deploy_destination_sha(config),
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
    )
