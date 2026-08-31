"""Stable persistence API backed by explicit SQLite responsibility modules.

Callers keep importing ``mergetrain.store`` for compatibility. Implementation
is grouped under :mod:`mergetrain.persistence` so transaction, schema, job,
lease, event, claim, and recovery semantics remain visible rather than hidden
behind a generic data-access abstraction.
"""

from __future__ import annotations

from .persistence.claims import claim_all_queued, claim_deploy_batch, claim_next_job
from .persistence.connection import connect
from .persistence.events import (
    RUN_EVENT_RETENTION,
    list_history_events,
    list_run_events,
    record_run_event,
)
from .persistence.events import _record_run_event as _record_run_event
from .persistence.jobs import (
    SupersedeReplacement,
    cancel_job,
    counts,
    dismiss_job,
    enqueue_job,
    get_job,
    has_queued_auto,
    has_queued_manual,
    list_attention_jobs,
    list_dismissable_jobs,
    list_history_jobs,
    list_jobs,
    list_jobs_fifo,
    list_train_jobs,
    list_verify_unknown_jobs,
    mark_job,
    resolve_verify_status,
    retry_job,
    select_validated_train,
    supersede_validated_train,
    terminal_branch_candidates,
    validated_train_summaries,
)
from .persistence.leases import (
    RUNNER_LOCK_NAME,
    Liveness,
    acquire_runner_lock,
    active_runner_lock,
    default_owner,
    force_clear_lock_and_split,
    get_lock,
    has_in_progress,
    live_worktree_path,
    owner_liveness,
    recover_orphans,
    refresh_runner_lock,
    release_runner_lock,
)
from .persistence.operations import (
    RECOVERY_OPERATION_STATES,
    RECOVERY_OPERATIONS,
    finish_recovery_operation,
    list_recovery_operation_events,
    start_recovery_operation,
)
from .persistence.recovery import (
    clear_rejected_push,
    deploy_reconcile_pending,
    pack_push_refs,
    record_pending_push,
    unpack_push_refs,
)
from .persistence.schema import SCHEMA_VERSION, ensure_schema
from .persistence.transactions import _parse_utc as _parse_utc
from .persistence.transactions import immediate, utc_now

__all__ = (
    "Liveness",
    "RUNNER_LOCK_NAME",
    "RUN_EVENT_RETENTION",
    "RECOVERY_OPERATIONS",
    "RECOVERY_OPERATION_STATES",
    "SCHEMA_VERSION",
    "SupersedeReplacement",
    "acquire_runner_lock",
    "active_runner_lock",
    "cancel_job",
    "claim_all_queued",
    "claim_deploy_batch",
    "claim_next_job",
    "clear_rejected_push",
    "connect",
    "counts",
    "default_owner",
    "deploy_reconcile_pending",
    "dismiss_job",
    "enqueue_job",
    "ensure_schema",
    "force_clear_lock_and_split",
    "finish_recovery_operation",
    "get_job",
    "get_lock",
    "has_in_progress",
    "has_queued_auto",
    "has_queued_manual",
    "immediate",
    "list_attention_jobs",
    "list_dismissable_jobs",
    "list_history_events",
    "list_history_jobs",
    "list_jobs",
    "list_jobs_fifo",
    "list_run_events",
    "list_recovery_operation_events",
    "list_train_jobs",
    "list_verify_unknown_jobs",
    "live_worktree_path",
    "mark_job",
    "owner_liveness",
    "pack_push_refs",
    "record_pending_push",
    "record_run_event",
    "recover_orphans",
    "refresh_runner_lock",
    "release_runner_lock",
    "resolve_verify_status",
    "retry_job",
    "select_validated_train",
    "start_recovery_operation",
    "supersede_validated_train",
    "terminal_branch_candidates",
    "unpack_push_refs",
    "utc_now",
    "validated_train_summaries",
)
