"""Queue mutation commands."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..cli_support import (
    _preflight_config,
    _recovery_next_action,
    config_from_args,
    dump_json,
)
from ..command_runner import run_command
from ..deploy_plan import deploy_destination_sha, deploy_execution_policy_sha
from ..errors import CommandFailed, MergetrainError, QueueError
from ..git_ops import (
    git_current_branch,
    git_dirty_paths,
    git_repo_root,
    git_rev_parse,
    git_worktree_clean,
)
from ..snapshot import next_action as _doctor_next_action
from ..store import (
    SupersedeReplacement,
    cancel_job,
    connect,
    counts,
    dismiss_job,
    enqueue_job,
    get_job,
    list_dismissable_jobs,
    retry_job,
    supersede_validated_train,
    validated_train_summaries,
)


def _capture_sha_or_error(path: Path, ref: str, *, label: str) -> str:
    try:
        return git_rev_parse(path, ref)
    except CommandFailed as exc:
        raise QueueError(f"could not capture {label} SHA for {ref}: {exc}") from exc


def _validate_enqueue_worktree(
    worktree: Path,
    branch: str,
) -> None:
    if not worktree.exists():
        raise QueueError(f"worktree does not exist: {worktree}")
    if not git_repo_root(worktree):
        raise QueueError(f"not a git worktree: {worktree}")
    if not git_worktree_clean(worktree):
        dirty = git_dirty_paths(worktree)
        hint = f" ({', '.join(dirty)})" if dirty else ""
        raise QueueError(
            f"worktree has uncommitted changes{hint}; commit or stash them. "
            "(mergetrain's own .mergetrain/ state is "
            "self-ignored — if it appears here, upgrade mergetrain.)"
        )
    current = git_current_branch(worktree)
    if current != branch:
        raise QueueError(f"current branch {current!r} does not match --branch {branch!r}")


def cmd_enqueue(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    _preflight_config(config)
    worktree = Path(args.worktree or Path.cwd()).expanduser().resolve()
    _validate_enqueue_worktree(worktree, args.branch)
    # v3 has one enqueue path: verify a clean owning worktree and derive both
    # identities from Git. User-supplied SHAs and readiness bypasses no longer
    # exist, so the queue row always describes the branch that was inspected.
    base_sha = _capture_sha_or_error(config.repo, config.git.integration_ref, label="base")
    head_sha = _capture_sha_or_error(worktree, args.branch, label="head")
    approval_destination_sha = deploy_destination_sha(config) if args.auto else ""
    approval_execution_policy_sha = deploy_execution_policy_sha(config) if args.auto else ""
    conn = connect(config.state.db)
    try:
        job = enqueue_job(
            conn,
            task=args.task,
            branch=args.branch,
            worktree_path=str(worktree),
            base_sha=base_sha,
            head_sha=head_sha,
            note=args.note or "",
            allow_duplicate=False,
            auto_deploy=args.auto,
            approval_destination_sha=approval_destination_sha,
            approval_execution_policy_sha=approval_execution_policy_sha,
        )
    finally:
        conn.close()
    payload = {"ok": True, "job": job.to_dict()}
    if args.json:
        dump_json(payload)
    else:
        print(f"queued job {job.id}: {job.task} ({job.branch})")
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    """Replace a blocked/failed row with a freshly SHA-pinned queue job."""

    config = config_from_args(args)
    _preflight_config(config)
    conn = connect(config.state.db)
    try:
        original = get_job(conn, args.job_id)
        if original.status not in {"blocked", "failed"}:
            raise QueueError(
                f"only a blocked or failed job can be retried (job {original.id} "
                f"is {original.status})"
            )
        if not original.worktree_path:
            raise QueueError(
                f"job {original.id} has no recorded worktree path; enqueue its "
                "fixed branch manually"
            )
        worktree = Path(original.worktree_path).expanduser().resolve()
        _validate_enqueue_worktree(worktree, original.branch)
        if args.rebase:
            # Fetch/rebase before any queue mutation. A conflict intentionally
            # leaves the worktree in rebase state for the user to resolve, while
            # the original blocked/failed row remains untouched.
            run_command(["git", "fetch", config.git.remote], cwd=worktree)
            run_command(["git", "rebase", config.git.integration_ref], cwd=worktree)
            _validate_enqueue_worktree(worktree, original.branch)

        base_sha = _capture_sha_or_error(config.repo, config.git.integration_ref, label="base")
        head_sha = _capture_sha_or_error(worktree, original.branch, label="head")
        current_destination_sha = ""
        current_execution_policy_sha = ""
        if original.auto_deploy:
            current_execution_policy_sha = deploy_execution_policy_sha(config)
            try:
                current_destination_sha = deploy_destination_sha(config)
            except MergetrainError:
                # Retry remains available for repair, but cannot inherit an
                # unattended approval when the endpoint is no longer provable.
                current_destination_sha = ""
        dismissed, replacement = retry_job(
            conn,
            original.id,
            base_sha=base_sha,
            head_sha=head_sha,
            current_approval_destination_sha=current_destination_sha,
            current_approval_execution_policy_sha=current_execution_policy_sha,
        )
        next_action = _recovery_next_action(conn, config)
    finally:
        conn.close()
    payload = {
        "ok": True,
        "dismissed_job": dismissed.to_dict(),
        "job": replacement.to_dict(),
        "next_action": next_action,
    }
    if args.json:
        dump_json(payload)
    else:
        print(
            f"retried job {dismissed.id} as {replacement.id}: "
            f"{replacement.task} ({replacement.branch})"
        )
        print(f"next action: {next_action}")
    return 0


def cmd_supersede(args: argparse.Namespace) -> int:
    """Replace one validated train without transferring its approval."""

    config = config_from_args(args)
    _preflight_config(config)
    base_sha = _capture_sha_or_error(config.repo, config.git.integration_ref, label="base")
    replacements: list[SupersedeReplacement] = []
    for task, branch, worktree_value in args.replacement:
        worktree = Path(worktree_value).expanduser().resolve()
        _validate_enqueue_worktree(worktree, branch)
        head_sha = _capture_sha_or_error(worktree, branch, label="head")
        replacements.append(
            SupersedeReplacement(
                task=task,
                branch=branch,
                worktree_path=str(worktree),
                base_sha=base_sha,
                head_sha=head_sha,
                note=args.note or "",
            )
        )

    conn = connect(config.state.db)
    try:
        result = supersede_validated_train(
            conn,
            args.train_id,
            replacements,
        )
        validated_trains = validated_train_summaries(conn)
        next_action = _doctor_next_action(
            {
                "lock": None,
                "counts": counts(conn),
                "validated_trains": validated_trains,
                "gc": {"worktree_candidates": []},
                "config_exists": config.config_exists,
            },
            config_version=config.config_version,
        )
    finally:
        conn.close()
    payload = {
        "ok": True,
        "supersession_id": result["supersession_id"],
        "superseded_train_id": result["superseded_train_id"],
        "superseded_jobs": [job.to_dict() for job in result["superseded_jobs"]],
        "replacement_jobs": [job.to_dict() for job in result["replacement_jobs"]],
        "next_action": next_action,
    }
    if args.json:
        dump_json(payload)
    else:
        replacement_ids = ", ".join(str(job["id"]) for job in payload["replacement_jobs"])
        print(f"superseded train {payload['superseded_train_id']} with job(s) {replacement_ids}")
        print(f"next action: {next_action}")
    return 0


def cmd_dismiss(args: argparse.Namespace) -> int:
    """Clear superseded blocked/failed jobs so they stop pinning next_action.

    Non-destructive by construction: it only touches jobs that already failed
    to land, never queued or in-progress work.
    """

    config = config_from_args(args)
    conn = connect(config.state.db)
    try:
        if args.all:
            targets = list_dismissable_jobs(conn)
        else:
            if args.job_id is None:
                raise QueueError("dismiss requires a job id or --all")
            targets = [get_job(conn, args.job_id)]
        dismissed = [dismiss_job(conn, job.id, note=args.note or "").to_dict() for job in targets]
        next_action = _recovery_next_action(conn, config)
    finally:
        conn.close()
    payload = {"ok": True, "dismissed": dismissed, "next_action": next_action}
    if args.json:
        dump_json(payload)
    else:
        if not dismissed:
            print("no blocked/failed jobs to dismiss")
        for job in dismissed:
            print(f"dismissed job {job['id']}: {job['branch']}")
        print(f"next action: {next_action}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    conn = connect(config.state.db)
    try:
        job = cancel_job(conn, args.job_id, note=args.note or "")
    finally:
        conn.close()
    if args.json:
        dump_json({"ok": True, "job": job.to_dict()})
    else:
        action = "cancellation requested for" if job.cancel_requested_at else "canceled"
        print(f"{action} job {job.id}: {job.branch}")
    return 0
