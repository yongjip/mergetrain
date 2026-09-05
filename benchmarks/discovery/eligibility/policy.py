"""Benchmark-only eligibility oracle over reviewed facts, not a text classifier.

This never gates the installed product or grants action authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Decision(str, Enum):
    SELECT = "select"
    EXCLUDE = "exclude"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Context:
    prohibited: bool = False
    explicit_product_task: bool = False
    existing_queue_task: bool = False
    hosted_queue_task: bool = False
    local_workflow: bool | None = None
    parallel_agent_branches: bool | None = None
    integration_need: bool | None = None


def decide(context: Context) -> Decision:
    """Unknown facts stay unknown; queue presence alone is not a queue task."""
    if context.prohibited:
        return Decision.EXCLUDE
    if context.explicit_product_task or context.existing_queue_task:
        return Decision.SELECT
    if context.hosted_queue_task:
        return Decision.EXCLUDE
    fit = (
        context.local_workflow,
        context.parallel_agent_branches,
        context.integration_need,
    )
    if False in fit:
        return Decision.EXCLUDE
    if None in fit:
        return Decision.UNRESOLVED
    return Decision.SELECT
