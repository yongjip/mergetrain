"""Shared CLI serialization, configuration, and rendering helpers."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Any

from .config import (
    CONFIG_VERSION,
    DEFAULT_CONFIG_NAME,
    MergetrainConfig,
    load_config,
)
from .contract import CONTRACT_VERSION
from .errors import ConfigError
from .snapshot import next_action as _doctor_next_action
from .store import counts, get_lock, validated_train_summaries

GLOBAL_OPTIONS_WITH_VALUES = {"--config", "--repo", "--db"}

def normalize_global_options(argv: Sequence[str]) -> list[str]:
    """Allow global options before or after the subcommand.

    Many coding agents place ``--repo`` or ``--config`` after the subcommand.
    argparse normally rejects that. This lightweight normalizer moves known
    global options to the front while leaving command-specific arguments intact.
    """

    moved: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            # Everything after the POSIX option terminator is command data.
            # Never hoist a literal global option from passthrough arguments
            # and silently retarget the command.
            rest.extend(argv[index:])
            break
        matched_equals = False
        for option in GLOBAL_OPTIONS_WITH_VALUES:
            if token.startswith(option + "="):
                moved.append(token)
                matched_equals = True
                break
        if matched_equals:
            index += 1
            continue
        if token in GLOBAL_OPTIONS_WITH_VALUES and index + 1 < len(argv):
            moved.extend([token, argv[index + 1]])
            index += 2
            continue
        rest.append(token)
        index += 1
    return moved + rest


def dump_json(payload: Any) -> None:
    # Stamp the contract version on every one-shot JSON payload at the single
    # serializer (machine contract). sort_keys places it deterministically. Payloads
    # that already carry the field (or aren't dicts) pass through untouched;
    # nested sub-objects (job dicts, embedded snapshots) are deliberately NOT
    # stamped — the outer frame owns the number.
    if isinstance(payload, dict) and "contract_version" not in payload:
        payload = {"contract_version": CONTRACT_VERSION, **payload}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def config_from_args(args: argparse.Namespace) -> MergetrainConfig:
    return load_config(config_path=args.config, repo=args.repo, db_override=args.db)


def _preflight_config(config: MergetrainConfig) -> None:
    """Fail closed before any state-shipping work (#44, #84 defect 6).

    Enforced on the deploy-capable paths — ``enqueue``/``run-batch``/
    ``run-next`` and both daemons — never inside ``load_config``, so a version
    mismatch or a missing file after a rollback can still run
    ``reconcile``/``recover``/``unlock`` and every read-only command.

    Two configs are unsafe to ship against:

    - Newer than this binary understands: an older mergetrain may misread it.
    - Absent: ``load_config`` otherwise falls back to ``origin/main`` and
      minimal default gates, so a deploy would run against guessed settings.
      Require an explicit ``mergetrain init`` instead.
    """

    if config.config_version > CONFIG_VERSION:
        raise ConfigError(
            f"config version {config.config_version} is newer than this "
            f"mergetrain understands (supports {CONFIG_VERSION}); upgrade "
            "mergetrain before enqueuing or deploying. Recovery and read-only "
            "commands still work."
        )
    if not config.config_exists:
        raise ConfigError(
            f"no {DEFAULT_CONFIG_NAME} in this repo; run 'mergetrain init' before "
            "enqueuing or deploying. mergetrain will not ship against guessed "
            "defaults (origin/main and minimal gates). Recovery and read-only "
            "commands still work."
        )


def _dump_jsonl(payload: dict[str, Any]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        flush=True,
    )


def _error_payload(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    next_action: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """The one failure envelope: ``{ok:false, error{code,message,
    retryable}, next_action?}``. Every failing --json command emits exactly
    this, so a consumer parses one shape and branches on ``error.code``."""

    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": code, "message": message, "retryable": retryable},
    }
    if next_action is not None:
        payload["next_action"] = next_action
    payload.update(extra)
    return payload


def _job_result_line(job: dict[str, Any]) -> str:
    outcomes: list[str] = []
    if job.get("push_status", "not_run") != "not_run":
        outcomes.append(f"push={job['push_status']}")
    if job.get("verify_status", "not_run") != "not_run":
        outcomes.append(f"verify={job['verify_status']}")
    if job.get("reused_validation_sha"):
        outcomes.append(f"reused={job['reused_validation_sha']}")
    outcome_text = f" ({', '.join(outcomes)})" if outcomes else ""
    return f"#{job['id']} {job['status']}{outcome_text}: {job['branch']}"


def _recovery_next_action(conn, config: MergetrainConfig) -> str:
    lock = get_lock(conn)
    return _doctor_next_action(
        {
            "lock": lock.to_dict() if lock else None,
            "counts": counts(conn),
            "validated_trains": validated_train_summaries(conn),
            "gc": {"worktree_candidates": []},
            # Recovery runs without a config on purpose, so it is exactly where
            # a reader needs to be told that shipping still needs one.
            "config_exists": config.config_exists,
        },
        config_version=config.config_version,
    )
