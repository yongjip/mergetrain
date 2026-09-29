"""Git worktree runner for mergetrain."""

from __future__ import annotations

import io
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import IO, Any

from .atomic_push import (
    AtomicPush,
)
from .atomic_push import (
    PushVerifyState as _PushVerifyState,
)
from .atomic_push import (
    post_push_verify_status as _post_push_verify_status,
)
from .command_runner import run_command
from .config import MergetrainConfig, load_config
from .deploy_plan import deploy_execution_policy_sha, deploy_plan_sha
from .errors import (
    AmbiguousPush,
    ApprovalDestinationChanged,
    ApprovalExecutionPolicyChanged,
    CancellationRequested,
    CommandFailed,
    DeployPlanChanged,
    LostLease,
    MergeBlocked,
    MergetrainError,
    PushRejected,
    QueueBusy,
)
from .gate_runner import GateProgress, GateRunner
from .git_destination import resolve_git_destination
from .git_ops import (
    git_output,
    git_rev_parse,
    git_worktree_clean,
)
from .models import Job
from .persistence.events import record_run_event
from .persistence.jobs import get_job, mark_job
from .persistence.leases import refresh_runner_lock
from .persistence.transactions import utc_now
from .reuse import ReuseCheck, ReuseDecision
from .validation_reuse import ValidationReuse, unauthorized_reuse_decision
from .worktree_manager import WorktreeManager

Pulse = Callable[[], None]


class _BisectAbort(Exception):
    """Bisect isolation cannot classify the failure from gate evidence."""


class _AlreadyLanded(PushRejected):
    """The remote refused the push, but its push refs hold every job of the train.

    Nothing of the refused push landed, so the jobs go back to the queue, and
    their next deploy finds them merged and records the deployment. Anything
    that does not expect it treats it as the push rejection it also is.
    """


class GitRunner:
    """Executes queued branches in temporary Git worktrees."""

    def __init__(self, config: MergetrainConfig):
        self.config = config
        self.repo = config.repo
        self._gates = GateRunner(config)
        self._validation = ValidationReuse(config, self._gates)
        self._worktrees = WorktreeManager(config, self._gates)
        self._pushes = AtomicPush(config)

    def _refresh_lease(
        self,
        conn: sqlite3.Connection,
        *,
        owner: str | None,
        lease_token: str,
        ttl_minutes: int,
        worktree: Path,
        head_sha: str = "",
        check_cancel: bool = True,
    ) -> None:
        """Extend the runner lease so a long-running job is never seen as stale.

        A healthy runner keeps its lease valid for the whole job; only a dead,
        hung, or recycled-PID owner lets the lease expire and become reclaimable.
        No-op when ``owner`` is None (e.g. direct test calls without a lock).
        """
        if owner is None:
            return
        refresh_runner_lock(
            conn,
            owner=owner,
            token=lease_token,
            ttl_minutes=ttl_minutes,
            worktree_path=str(worktree),
            head_sha=head_sha,
            check_cancel=check_cancel,
        )

    def _mark_job(
        self,
        conn: sqlite3.Connection,
        job_id: int,
        *,
        lease_token: str,
        **values: Any,
    ) -> Job:
        return mark_job(
            conn,
            job_id,
            expected_claim_token=lease_token or None,
            **values,
        )

    def _event(
        self,
        conn: sqlite3.Connection,
        *,
        lease_token: str,
        phase: str,
        state: str,
        message: str,
        job_id: int | None = None,
        detail: str = "",
    ) -> None:
        record_run_event(
            conn,
            claim_token=lease_token,
            job_id=job_id,
            phase=phase,
            state=state,
            message=message,
            detail=detail,
        )

    def _finish_job(
        self,
        conn: sqlite3.Connection,
        job_id: int,
        *,
        lease_token: str,
        **values: Any,
    ) -> Job:
        try:
            result = self._mark_job(conn, job_id, lease_token=lease_token, **values)
        except CancellationRequested:
            result = self._mark_job(
                conn,
                job_id,
                lease_token=lease_token,
                status="canceled",
                log_path=str(values.get("log_path", "")),
                note="canceled by user while the train was running",
            )
        event_map = {
            "validated": ("ready", "success", f"Job #{job_id} validated"),
            "blocked": ("blocked", "error", f"Job #{job_id} blocked"),
            "failed": ("failed", "error", f"Job #{job_id} failed"),
            "canceled": ("canceled", "warning", f"Job #{job_id} canceled"),
        }
        if result.status == "deployed":
            # 'unknown' needs the same attention as 'failed': the refs landed but
            # verification was never established, so a completion event that reads
            # plain success would hide the one thing the operator has to discharge
            # (mergetrain verify).
            if result.verify_status in {"failed", "unknown"}:
                event_map["deployed"] = (
                    "complete",
                    "warning",
                    f"Job #{job_id} deployed; verification needs attention",
                )
            else:
                event_map["deployed"] = (
                    "complete",
                    "success",
                    f"Job #{job_id} deployed",
                )
        event = event_map.get(result.status)
        if event:
            phase, state, message = event
            self._event(
                conn,
                lease_token=lease_token,
                job_id=job_id,
                phase=phase,
                state=state,
                message=message,
                detail="",
            )
        return result

    def _log_path(self, prefix: str, first_job_id: int) -> Path:
        stamp = utc_now().replace(":", "").replace("-", "").replace("Z", "")
        suffix = uuid.uuid4().hex[:8]
        return self.config.state.logs / f"{prefix}-{first_job_id}-{stamp}-{suffix}.log"

    def preview_validated_reuse(self, jobs: Sequence[Job]) -> ReuseDecision:
        """Evaluate reuse without claiming jobs, running gates, or pushing refs."""

        if not self.config.deploy.reuse.enabled:
            return unauthorized_reuse_decision(jobs)
        validation_shas = {job.validation_sha for job in jobs if job.validation_sha}
        validation_sha = next(iter(validation_shas)) if len(validation_shas) == 1 else ""
        self._worktrees.ensure_state_dirs()
        worktree = self._worktrees.worktree_path(jobs[0].id if jobs else 0)
        log = io.StringIO()
        try:
            self._worktrees.prepare(worktree=worktree, log=log, pulse=None)
            for job in jobs:
                self._merge_sha_for_job(job, deploying_validated=True)
            return self._validation.decide(
                jobs,
                worktree=worktree,
                integration_base_sha=git_rev_parse(worktree, "HEAD"),
                log=log,
                pulse=None,
            )
        except MergeBlocked as exc:
            return ReuseDecision(
                authorized=True,
                eligible=False,
                action=self.config.deploy.reuse.on_mismatch,
                validation_sha=validation_sha,
                reasons=(str(exc),),
                checks=(
                    ReuseCheck(
                        code="assembly",
                        status="mismatch",
                        expected="validated branch SHAs assemble cleanly",
                        actual=False,
                        detail=str(exc),
                    ),
                ),
            )
        finally:
            self._worktrees.cleanup(worktree, log=None, keep_worktree=False)

    def reverify_deploy(self, *, deploy_sha: str, log: IO[str]) -> bool:
        """Re-run the configured post-push verify hooks against a deploy_sha.

        Used by ``mergetrain verify`` to discharge a job left
        verify_status='unknown' by a crash in the post-push verify window.
        Assembles a throwaway detached worktree at the deployed commit and runs
        the hooks there; returns True iff every hook passed. Raises if the
        commit cannot be checked out (the caller reports it, does not guess).
        """

        if not self.config.deploy.verify:
            raise MergeBlocked(
                "verification policy is unavailable; use --ack succeeded/failed "
                "after explicit review"
            )
        self._worktrees.ensure_state_dirs()
        worktree = self._worktrees.worktree_path(0)
        run_command(
            ["git", "fetch", self.config.git.remote],
            cwd=self.repo,
            log=log,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )
        run_command(
            ["git", "worktree", "add", "--detach", str(worktree), deploy_sha],
            cwd=self.repo,
            log=log,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )
        try:
            self._gates.run_verify_hooks(worktree=worktree, log=log, pulse=None)
            return True
        except CommandFailed:
            return False
        finally:
            self._worktrees.cleanup(worktree, log=log, keep_worktree=False)

    def _gate_progress_callback(
        self,
        conn: sqlite3.Connection,
        *,
        lease_token: str,
        job_id: int | None = None,
    ) -> GateProgress:
        def report(name: str, state: str, index: int, total: int, command: str) -> None:
            verb = {
                "active": "Running",
                "reused": "Reused",
                "skipped": "Skipped",
                "failure": "Failed",
                "canceled": "Canceled",
            }.get(state, "Passed")
            self._event(
                conn,
                lease_token=lease_token,
                job_id=job_id,
                phase="gating",
                state=state,
                message=f"{verb} gate {index}/{total}: {name}",
                detail=command,
            )

        return report

    def _push_and_verify(
        self,
        conn: sqlite3.Connection,
        *,
        job_ids: list[int],
        deploy_sha: str,
        lease_token: str,
        worktree: Path,
        log: IO[str],
        before_push: Pulse,
        ownership_pulse: Pulse,
        state: _PushVerifyState,
        event_job_id: int | None = None,
        expected_plan_sha: str = "",
        task_commits: Sequence[str] = (),
    ) -> None:
        current_jobs = [get_job(conn, job_id) for job_id in job_ids]
        approved_destinations = {
            job.approval_destination_sha
            for job in current_jobs
            if job.auto_deploy
        }
        try:
            destination = resolve_git_destination(self.config)
        except MergetrainError as exc:
            if approved_destinations:
                raise ApprovalDestinationChanged(
                    "approval_destination_changed: unattended deploy destination "
                    "is no longer one exact supported push endpoint; nothing was pushed"
                ) from exc
            if expected_plan_sha:
                raise DeployPlanChanged(
                    "deploy_plan_changed: the confirmed destination is no longer "
                    "one exact supported push endpoint; nothing was pushed"
                ) from exc
            raise MergeBlocked(
                "deploy_destination_invalid: deploy requires one exact supported "
                "push endpoint; nothing was pushed"
            ) from exc
        current_destination = destination.destination_sha
        if approved_destinations and approved_destinations != {current_destination}:
            raise ApprovalDestinationChanged(
                "approval_destination_changed: unattended deploy approval no "
                "longer matches the current remote or push refs; nothing was pushed"
            )
        self._assert_auto_execution_policy(current_jobs)
        if expected_plan_sha:
            try:
                current_config = load_config(
                    repo=self.config.repo,
                    config_path=self.config.config_path,
                )
            except MergetrainError as exc:
                raise DeployPlanChanged(
                    "deploy_plan_changed: the confirmed execution policy can "
                    "no longer be resolved; nothing was pushed"
                ) from exc
            current_plan_sha = deploy_plan_sha(
                current_config,
                current_jobs,
                destination=destination,
            )
            if current_plan_sha != expected_plan_sha:
                raise DeployPlanChanged(
                    "deploy_plan_changed: the confirmed train, destination, gates, "
                    "reuse, or verify policy changed before push; nothing was pushed"
                )
        self._gates.check_verify_hooks(worktree=worktree)
        try:
            self._pushes.deploy_and_verify(
                conn,
                job_ids=job_ids,
                deploy_sha=deploy_sha,
                lease_token=lease_token,
                worktree=worktree,
                log=log,
                before_push=before_push,
                ownership_pulse=ownership_pulse,
                state=state,
                event=self._event,
                destination=destination,
                event_job_id=event_job_id,
                run_verify_hooks=self._gates.run_verify_hooks,
            )
        except PushRejected as exc:
            # An earlier push of these jobs that reconcile found had not landed
            # can land later on a network remote. It moves the refs, so this
            # push is refused although every job is already live.
            if self._pushes.contains_commits(
                task_commits, destination=destination, log=log, pulse=ownership_pulse
            ):
                raise _AlreadyLanded(
                    "the remote refused this push, but its push refs already "
                    "contain every job of this train, most likely from an "
                    "earlier push of these jobs that landed after reconcile read "
                    "the remote; nothing of this push landed, so the jobs are "
                    f"requeued for a deploy that records it: {exc}"
                ) from exc
            raise

    def _assert_auto_execution_policy(self, jobs: Iterable[Job]) -> None:
        auto_jobs = [job for job in jobs if job.auto_deploy]
        if not auto_jobs:
            return
        approved_policies = {
            job.approval_execution_policy_sha for job in auto_jobs
        }
        try:
            # Reload from the control checkout at every irreversible boundary.
            # The runner executes the immutable policy snapshot loaded at
            # startup, while this comparison detects an on-disk policy edit
            # made after claim or while gates were running.
            current_policy = deploy_execution_policy_sha(
                load_config(
                    repo=self.config.repo,
                    config_path=self.config.config_path,
                )
            )
        except MergetrainError as exc:
            raise ApprovalExecutionPolicyChanged(
                "approval_execution_policy_changed: unattended deploy policy "
                "can no longer be resolved; nothing was pushed"
            ) from exc
        if approved_policies != {current_policy}:
            raise ApprovalExecutionPolicyChanged(
                "approval_execution_policy_changed: unattended deploy approval "
                "no longer matches the configured gates, validation reuse, or "
                "verify hooks; nothing was pushed"
            )

    def _merge_sha_for_job(self, job: Job, *, deploying_validated: bool) -> str:
        """Resolve and verify the exact task commit that may be merged."""

        try:
            current_sha = git_rev_parse(self.repo, f"refs/heads/{job.branch}")
        except CommandFailed as exc:
            raise MergeBlocked(f"task branch cannot be resolved: {job.branch}") from exc
        expected_ref = job.validated_head_sha if deploying_validated else job.head_sha
        if not expected_ref:
            return current_sha
        try:
            expected_sha = git_rev_parse(self.repo, expected_ref)
        except CommandFailed as exc:
            checkpoint = "validation" if deploying_validated else "enqueue"
            raise MergeBlocked(
                f"recorded {checkpoint} HEAD cannot be resolved for {job.branch}"
            ) from exc
        if current_sha != expected_sha:
            checkpoint = "validation" if deploying_validated else "enqueue"
            raise MergeBlocked(
                f"branch HEAD changed since {checkpoint}: {job.branch} "
                f"(expected {expected_sha}, found {current_sha}); "
                "commit the intended result in the owning branch with a clean "
                f"worktree, then run mergetrain retry {job.id}"
            )
        return expected_sha

    def _process_isolated_jobs(
        self,
        conn: sqlite3.Connection,
        jobs: Sequence[Job],
        *,
        deploy: bool,
        keep_worktree: bool,
        owner: str | None,
        ttl_minutes: int,
        lease_token: str,
        expected_plan_sha: str = "",
    ) -> list[Job]:
        """Process isolated jobs in order, stopping at an ambiguous deploy.

        Isolation happens after the whole batch has already been claimed. If an
        isolated push becomes ambiguous, no later job may target the same refs
        until reconcile resolves that outcome. Return the untouched suffix to
        ``queued`` so it is neither stranded in-progress nor pushed out of FIFO
        order.
        """

        results: list[Job] = []
        for index, job in enumerate(jobs):
            finished = self.process_batch(
                conn,
                [job],
                deploy=deploy,
                keep_worktree=keep_worktree,
                owner=owner,
                ttl_minutes=ttl_minutes,
                expected_plan_sha=expected_plan_sha,
            )
            results.extend(finished)
            if not deploy or not any(item.status == "needs_reconcile" for item in finished):
                continue

            note = (
                f"deferred because isolated job {job.id} has an unresolved "
                "push; reconcile before deploying this job"
            )
            self._event(
                conn,
                lease_token=lease_token,
                phase="pushing",
                state="warning",
                message="Isolation stopped for pending reconcile",
                detail=f"job_id={job.id}",
            )
            for pending in jobs[index + 1 :]:
                current = get_job(conn, pending.id)
                if current.status == "in_progress" and (
                    not lease_token or current.claim_token == lease_token
                ):
                    current = self._finish_job(
                        conn,
                        pending.id,
                        lease_token=lease_token,
                        status="queued",
                        note=note,
                    )
                results.append(current)
            break
        return results

    def _bisect_failed_train(
        self,
        conn: sqlite3.Connection,
        merged_jobs: list[Job],
        *,
        merge_shas: dict[int, str],
        integration_base_sha: str,
        worktree: Path,
        log: IO[str],
        log_path: Path,
        lease_token: str,
        deploy: bool,
        keep_worktree: bool,
        owner: str | None,
        ttl_minutes: int,
        expected_plan_sha: str = "",
    ) -> list[Job]:
        """Classify a failed multi-job train with subset gate probes.

        Bisection only ever *removes* jobs from the train: individually
        failing jobs finish as ``failed``, and combinations whose members
        pass alone but fail together finish as ``blocked`` semantic
        conflicts with ``conflict_with`` naming the partners. Surviving
        jobs are re-run through ``process_batch``, so nothing ships without
        a full gate pass over the exact final combination.
        """
        order = {job.id: index for index, job in enumerate(merged_jobs)}
        probe_cache: dict[frozenset[int], bool] = {}
        probe_count = 0
        probe_worktree = self._worktrees.worktree_path(merged_jobs[0].id)

        def pulse() -> None:
            # The lease names the one worktree gc must spare. While probes run,
            # that is the probe worktree, not the idle train worktree (#231).
            self._refresh_lease(
                conn,
                owner=owner,
                lease_token=lease_token,
                ttl_minutes=ttl_minutes,
                worktree=probe_worktree,
            )

        def probe(subset: Sequence[Job]) -> bool:
            """Assemble ``subset`` on the recorded base and run the gates.

            Returns True iff the merges are clean and every gate passes.
            Raises ``_BisectAbort`` on a merge conflict: a subset whose merge
            does not reproduce the train's context cannot be classified by
            gate evidence, so the caller falls back to linear isolation.
            """
            nonlocal probe_count
            members = sorted(subset, key=lambda job: order[job.id])
            key = frozenset(job.id for job in members)
            if key in probe_cache:
                return probe_cache[key]
            probe_count += 1
            ids = [job.id for job in members]
            log.write(f"\n## bisect probe {probe_count}: jobs {ids}\n")
            self._event(
                conn,
                lease_token=lease_token,
                phase="gating",
                state="active",
                message=f"Bisect probe {probe_count}: jobs {ids}",
            )
            pulse()
            run_command(
                ["git", "reset", "--hard", integration_base_sha],
                cwd=probe_worktree,
                log=log,
            )
            run_command(["git", "clean", "-fdx"], cwd=probe_worktree, log=log, check=False)
            for job in members:
                merge = run_command(
                    ["git", "merge", "--no-edit", merge_shas[job.id]],
                    cwd=probe_worktree,
                    log=log,
                    check=False,
                    pulse=pulse,
                    pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                    timeout_seconds=self.config.queue.command_timeout_seconds,
                )
                if merge.returncode != 0:
                    run_command(
                        ["git", "merge", "--abort"],
                        cwd=probe_worktree,
                        log=log,
                        check=False,
                    )
                    raise _BisectAbort(
                        f"probe merge of job {job.id} ({job.branch}) conflicted "
                        f"without its train predecessors"
                    )
            try:
                self._gates.run_gates(
                    worktree=probe_worktree,
                    log=log,
                    pulse=pulse,
                    base_ref=integration_base_sha,
                    head_ref=git_rev_parse(probe_worktree, "HEAD"),
                )
                passed = True
            except CommandFailed:
                passed = False
            probe_cache[key] = passed
            return passed

        singles: list[Job] = []
        conflict_sets: list[list[Job]] = []

        def minimize_joint_failure(subset: list[Job]) -> None:
            """Both halves of ``subset`` pass alone, so the failure is joint.

            Greedily shrink to a minimal failing set, then verify each
            remaining member really passes alone before calling the set a
            semantic conflict. Members proven unnecessary rejoin the
            survivors; a failure that does not reproduce aborts to linear
            isolation instead of blaming anyone.
            """
            if probe(subset):
                raise _BisectAbort(
                    "train gate failure did not reproduce when the full "
                    "subset was re-assembled (flaky gate?)"
                )
            minimal = list(subset)
            for job in list(minimal):
                if len(minimal) == 1:
                    break
                reduced = [item for item in minimal if item.id != job.id]
                if not probe(reduced):
                    minimal = reduced
            if len(minimal) == 1:
                singles.append(minimal[0])
                return
            solo_failures = [job for job in minimal if not probe([job])]
            if solo_failures:
                # A member fails alone: the joint attribution is unsound, so
                # only the proven-solo failures are removed; the rest rejoin
                # the survivors (a remaining real conflict re-surfaces there).
                singles.extend(solo_failures)
                return
            conflict_sets.append(minimal)

        def descend(subset: list[Job]) -> None:
            # Invariant: subset is known to fail as a combination — proven by
            # the original train gate run (top level) or by a probe.
            if len(subset) == 1:
                singles.append(subset[0])
                return
            mid = len(subset) // 2
            left, right = subset[:mid], subset[mid:]
            left_fails = not probe(left)
            right_fails = not probe(right)
            if left_fails:
                descend(left)
            if right_fails:
                descend(right)
            if left_fails or right_fails:
                return
            minimize_joint_failure(subset)

        try:
            # Claim the path before it exists, so gc never sees it unowned.
            pulse()
            run_command(
                [
                    "git",
                    "worktree",
                    "add",
                    "--detach",
                    str(probe_worktree),
                    integration_base_sha,
                ],
                cwd=self.repo,
                log=log,
            )
            try:
                descend(list(merged_jobs))
            finally:
                self._worktrees.cleanup(probe_worktree, log=log, keep_worktree=False)
        except _BisectAbort as abort:
            log.write(f"\nbisect aborted: {abort}; falling back to linear isolation\n")
            self._event(
                conn,
                lease_token=lease_token,
                phase="gating",
                state="warning",
                message="Bisect inconclusive; isolating jobs one-by-one",
                detail=str(abort),
            )
            return self._process_isolated_jobs(
                conn,
                merged_jobs,
                deploy=deploy,
                keep_worktree=keep_worktree,
                owner=owner,
                ttl_minutes=ttl_minutes,
                lease_token=lease_token,
                expected_plan_sha=expected_plan_sha,
            )

        culprit_ids = {job.id for job in singles}
        for group in conflict_sets:
            culprit_ids.update(job.id for job in group)
        goods = [job for job in merged_jobs if job.id not in culprit_ids]

        results = []
        for job in singles:
            results.append(
                self._finish_job(
                    conn,
                    job.id,
                    lease_token=lease_token,
                    status="failed",
                    log_path=str(log_path),
                    note=(
                        "failed train gates individually during bisect isolation; "
                        "fix the owning branch, commit a clean result, then run "
                        f"mergetrain retry {job.id}"
                    ),
                )
            )
        for group in conflict_sets:
            for job in group:
                others = [item for item in group if item.id != job.id]
                partners = ", ".join(
                    f"job {other.id} ({other.branch} @ {merge_shas[other.id][:12]})"
                    for other in others
                )
                note = (
                    "semantic conflict: passes gates alone but fails combined "
                    f"with {partners}; rebase onto the integration branch with "
                    "the other side merged, fix the joint breakage, commit a "
                    f"clean result, then run mergetrain retry {job.id}"
                )
                results.append(
                    self._finish_job(
                        conn,
                        job.id,
                        lease_token=lease_token,
                        status="blocked",
                        log_path=str(log_path),
                        note=note,
                        conflict_with=",".join(str(other.id) for other in others),
                    )
                )
        summary = (
            f"bisect isolation: {probe_count} probe(s), {len(singles)} failing alone, "
            f"{sum(len(group) for group in conflict_sets)} in conflict, "
            f"{len(goods)} rejoining"
        )
        log.write(f"\n{summary}\n")
        self._event(
            conn,
            lease_token=lease_token,
            phase="gating",
            state="warning" if conflict_sets else "success",
            message=f"Bisect isolation complete: {len(goods)} job(s) rejoin the train",
            detail=summary,
        )
        self._worktrees.cleanup(worktree, log=log, keep_worktree=keep_worktree)
        if goods:
            results.extend(
                self.process_batch(
                    conn,
                    goods,
                    deploy=deploy,
                    keep_worktree=keep_worktree,
                    owner=owner,
                    ttl_minutes=ttl_minutes,
                    # A smaller train was never confirmed; the push-time plan
                    # check must run and refuse it rather than be skipped.
                    expected_plan_sha=expected_plan_sha,
                )
            )
        return results

    def process_batch(
        self,
        conn: sqlite3.Connection,
        jobs: Iterable[Job],
        *,
        deploy: bool,
        keep_worktree: bool = False,
        owner: str | None = None,
        ttl_minutes: int = 30,
        expected_plan_sha: str = "",
    ) -> list[Job]:
        jobs = list(jobs)
        if not jobs:
            return []
        claim_tokens = {job.claim_token for job in jobs}
        if owner is not None and (len(claim_tokens) != 1 or not next(iter(claim_tokens))):
            raise LostLease("batch jobs do not share one valid claim token")
        lease_token = next(iter(claim_tokens)) if owner is not None else ""
        validated_train_ids = {job.train_id for job in jobs if job.train_id}
        deploying_validated = deploy and bool(validated_train_ids)
        self._worktrees.ensure_state_dirs()
        log_path = self._log_path("batch", jobs[0].id)
        worktree, persistent_workspace = self._worktrees.primary_path(jobs[0].id, deploy=deploy)
        merged_jobs: list[Job] = []
        results: list[Job] = []
        merge_shas: dict[int, str] = {}
        deploy_sha = ""
        integration_base_sha = ""
        deploy_state = _PushVerifyState()
        reused_validation_sha = ""
        reuse_fallback_reason = ""
        reuse_authorized = self.config.deploy.reuse.enabled

        def pulse(*, check_cancel: bool = True) -> None:
            self._refresh_lease(
                conn,
                owner=owner,
                lease_token=lease_token,
                ttl_minutes=ttl_minutes,
                worktree=worktree,
                head_sha=deploy_sha,
                check_cancel=check_cancel,
            )

        def normal_pulse() -> None:
            pulse(check_cancel=True)

        def ownership_pulse() -> None:
            pulse(check_cancel=False)

        # A one-job train is that job's own run, so its events carry the job:
        # linear isolation runs several one-job trains under one claim, and
        # inspect must never show one job another job's progress.
        event_job_id = jobs[0].id if len(jobs) == 1 else None
        gate_progress = self._gate_progress_callback(
            conn, lease_token=lease_token, job_id=event_job_id
        )

        def finish(item: Job, **values: Any) -> Job:
            return self._finish_job(conn, item.id, lease_token=lease_token, **values)

        def finish_active_jobs(*, status: str, note: str) -> list[Job]:
            finished: list[Job] = []
            for item in jobs:
                current = get_job(conn, item.id)
                if current.status == "in_progress" and current.claim_token == lease_token:
                    finished.append(
                        finish(item, status=status, log_path=str(log_path), note=note)
                    )
                else:
                    finished.append(current)
            return finished

        def cancel_active_jobs() -> list[Job]:
            return finish_active_jobs(
                status="canceled", note="canceled by user while the train was running"
            )

        def finish_active_after_error(*, status: str, note: str) -> list[Job]:
            affected_jobs = jobs if deploying_validated else merged_jobs or jobs
            if deploy_state.push_status == "succeeded":
                status = "deployed"
                note = f"post-push completion warning: {note}"
                post_push_verify_status = _post_push_verify_status(deploy_state)
            else:
                post_push_verify_status = deploy_state.verify_status
            deployed_ids: list[int] = []
            for item in affected_jobs:
                current = get_job(conn, item.id)
                if current.status == "in_progress" and current.claim_token == lease_token:
                    result = finish(
                        item,
                        status=status,
                        deploy_sha=deploy_sha,
                        log_path=str(log_path),
                        note=note,
                        push_status=deploy_state.push_status,
                        verify_status=post_push_verify_status,
                        reused_validation_sha=reused_validation_sha,
                    )
                    results.append(result)
                    if result.status == "deployed":
                        deployed_ids.append(item.id)
            if deployed_ids:
                self._pushes.clear_pending_refs(deployed_ids, log=log)
            # A claimed job the train never reached is still in progress under
            # this lease. It rode no push and was never judged, so it goes back
            # to the queue instead of stranding in progress (#231).
            affected_ids = {item.id for item in affected_jobs}
            for item in jobs:
                if item.id in affected_ids:
                    continue
                current = get_job(conn, item.id)
                if current.status == "in_progress" and current.claim_token == lease_token:
                    results.append(
                        finish(
                            item,
                            status="queued",
                            log_path=str(log_path),
                            note=f"requeued: the train stopped before merging this job ({note})",
                        )
                    )
            return results

        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"mergetrain batch starting at job {jobs[0].id}\n")
            mode = "deploy" if deploy else "validate"
            log.write(f"jobs: {[job.id for job in jobs]}\nmode: {mode}\n")
            log.flush()
            try:
                for job in jobs:
                    self._mark_job(
                        conn,
                        job.id,
                        lease_token=lease_token,
                        status="in_progress",
                        log_path=str(log_path),
                        note=job.note,
                    )
                if deploying_validated and (
                    len(validated_train_ids) != 1
                    or any(not job.train_id for job in jobs)
                    or {job.train_size for job in jobs} != {len(jobs)}
                ):
                    note = "validated train identity is incomplete or mixes multiple trains; enqueue a fresh train"
                    return [
                        finish(job, status="blocked", log_path=str(log_path), note=note)
                        for job in jobs
                    ]
                self._event(
                    conn,
                    lease_token=lease_token,
                    job_id=event_job_id,
                    phase="fetching",
                    state="active",
                    message=f"Fetching {self.config.git.integration_ref}",
                )
                workspace_reused = self._worktrees.prepare(
                    worktree=worktree,
                    log=log,
                    pulse=normal_pulse,
                    persistent=persistent_workspace,
                )
                self._event(
                    conn,
                    lease_token=lease_token,
                    job_id=event_job_id,
                    phase="fetching",
                    state="success",
                    message=(
                        "Persistent validation workspace reused"
                        if workspace_reused
                        else (
                            "Persistent validation workspace created"
                            if persistent_workspace
                            else "Integration worktree prepared"
                        )
                    ),
                )
                integration_base_sha = git_rev_parse(worktree, "HEAD")
                if deploying_validated:
                    validation_bases = {job.validation_base_sha for job in jobs}
                    try:
                        merge_shas = {
                            job.id: self._merge_sha_for_job(job, deploying_validated=True)
                            for job in jobs
                        }
                    except MergeBlocked as exc:
                        note = f"validated train identity check failed: {exc}"
                        return [
                            finish(job, status="blocked", log_path=str(log_path), note=note)
                            for job in jobs
                        ]
                    if reuse_authorized:
                        reuse_decision = self._validation.decide(
                            jobs,
                            worktree=worktree,
                            integration_base_sha=integration_base_sha,
                            log=log,
                            pulse=normal_pulse,
                        )
                        if reuse_decision.eligible:
                            reused_validation_sha = reuse_decision.reused_validation_sha
                            self._event(
                                conn,
                                lease_token=lease_token,
                                job_id=event_job_id,
                                phase="assembling",
                                state="active",
                                message="Restoring exact validated train commit",
                                detail=reused_validation_sha,
                            )
                            run_command(
                                ["git", "reset", "--hard", reused_validation_sha],
                                cwd=worktree,
                                log=log,
                                pulse=normal_pulse,
                                pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                                timeout_seconds=self.config.queue.command_timeout_seconds,
                            )
                            deploy_sha = git_rev_parse(worktree, "HEAD")
                            if deploy_sha != reused_validation_sha or not git_worktree_clean(
                                worktree
                            ):
                                raise MergeBlocked(
                                    "exact validation commit could not be restored cleanly"
                                )
                            merged_jobs.extend(jobs)
                            self._event(
                                conn,
                                lease_token=lease_token,
                                job_id=event_job_id,
                                phase="assembling",
                                state="success",
                                message="Exact validated train commit restored",
                                detail=reused_validation_sha,
                            )
                        else:
                            reuse_fallback_reason = "; ".join(reuse_decision.reasons)
                            log.write(f"\nvalidated gate reuse declined: {reuse_fallback_reason}\n")
                            if reuse_decision.action == "fail":
                                raise MergeBlocked(
                                    "validated gate reuse policy failed closed: "
                                    f"{reuse_fallback_reason}"
                                )
                    if not reused_validation_sha and validation_bases != {integration_base_sha}:
                        log.write(
                            "\nintegration ref moved since validation; "
                            "reassembling the exact train and rerunning gates\n"
                        )

                if not reused_validation_sha:
                    self._event(
                        conn,
                        lease_token=lease_token,
                        job_id=event_job_id,
                        phase="assembling",
                        state="active",
                        message=f"Assembling train with {len(jobs)} job(s)",
                    )
                    for job in jobs:
                        log.write(f"\n## merge job {job.id}: {job.branch}\n")
                        normal_pulse()
                        self._event(
                            conn,
                            lease_token=lease_token,
                            job_id=job.id,
                            phase="assembling",
                            state="active",
                            message=f"Merging {job.branch}",
                        )
                        if not deploying_validated:
                            try:
                                merge_shas[job.id] = self._merge_sha_for_job(
                                    job, deploying_validated=False
                                )
                            except MergeBlocked as exc:
                                results.append(
                                    finish(
                                        job, status="blocked", log_path=str(log_path), note=str(exc)
                                    )
                                )
                                continue
                        pre_merge_head = git_output(["rev-parse", "HEAD"], cwd=worktree)
                        merge = run_command(
                            ["git", "merge", "--no-edit", merge_shas[job.id]],
                            cwd=worktree,
                            log=log,
                            check=False,
                            pulse=normal_pulse,
                            pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                            timeout_seconds=self.config.queue.command_timeout_seconds,
                        )
                        if merge.returncode != 0:
                            note = (
                                merge.stderr.strip()
                                or merge.stdout.strip()
                                or f"merge failed for {job.branch}"
                            )
                            if deploying_validated:
                                run_command(
                                    ["git", "merge", "--abort"], cwd=worktree, log=log, check=False
                                )
                                note = f"validated train could not be reassembled: {note}"
                                return [
                                    finish(
                                        item, status="blocked", log_path=str(log_path), note=note
                                    )
                                    for item in jobs
                                ]
                            results.append(
                                finish(job, status="blocked", log_path=str(log_path), note=note)
                            )
                            run_command(
                                ["git", "merge", "--abort"], cwd=worktree, log=log, check=False
                            )
                            continue
                        if not git_worktree_clean(worktree):
                            if deploying_validated:
                                note = "validated train produced a dirty integration worktree after reassembly"
                                return [
                                    finish(
                                        item, status="blocked", log_path=str(log_path), note=note
                                    )
                                    for item in jobs
                                ]
                            results.append(
                                finish(
                                    job,
                                    status="blocked",
                                    log_path=str(log_path),
                                    note="integration worktree is dirty after merge",
                                )
                            )
                            # the merge already committed (HEAD advanced), so
                            # `reset --hard HEAD` would only drop the stray dirt
                            # and keep this blocked job's merge commit in the
                            # assembled tree. Reset to the pre-merge tip instead
                            # so a blocked job can never ride the train.
                            run_command(
                                ["git", "reset", "--hard", pre_merge_head],
                                cwd=worktree,
                                log=log,
                                check=True,
                            )
                            continue
                        merged_jobs.append(job)
                        self._event(
                            conn,
                            lease_token=lease_token,
                            job_id=job.id,
                            phase="assembling",
                            state="success",
                            message=f"Merged {job.branch}",
                        )
                    if not merged_jobs:
                        log.write("\nno jobs were merged\n")
                        return results
                    self._event(
                        conn,
                        lease_token=lease_token,
                        job_id=event_job_id,
                        phase="assembling",
                        state="success",
                        message=f"Assembled {len(merged_jobs)} job(s)",
                    )
                    deploy_sha = git_rev_parse(worktree, "HEAD")
                normal_pulse()
                if persistent_workspace:
                    cache_reused = self._worktrees.activate_persistent_cache(
                        worktree=worktree,
                        log=log,
                        pulse=normal_pulse,
                    )
                    self._event(
                        conn,
                        lease_token=lease_token,
                        job_id=event_job_id,
                        phase="gating",
                        state="reused" if cache_reused else "success",
                        message=(
                            "Persistent validation cache reused"
                            if cache_reused
                            else "Persistent validation cache initialized"
                        ),
                    )
                if deploy:
                    self._assert_auto_execution_policy(merged_jobs)
                try:
                    if reuse_fallback_reason:
                        self._event(
                            conn,
                            lease_token=lease_token,
                            job_id=event_job_id,
                            phase="gating",
                            state="warning",
                            message="Validated gates were not reused; rerunning all gates",
                            detail=reuse_fallback_reason,
                        )
                    self._event(
                        conn,
                        lease_token=lease_token,
                        job_id=event_job_id,
                        phase="gating",
                        state="active",
                        message=(
                            "Reusing validated gates"
                            if reused_validation_sha
                            else "Running train gates"
                        ),
                        detail=reused_validation_sha,
                    )
                    if reused_validation_sha:
                        self._gates.run_reused_gates(
                            worktree=worktree,
                            validation_sha=reused_validation_sha,
                            base_ref=integration_base_sha,
                            log=log,
                            pulse=normal_pulse,
                            on_gate=gate_progress,
                        )
                    else:
                        self._gates.run_gates(
                            worktree=worktree,
                            log=log,
                            pulse=normal_pulse,
                            on_gate=gate_progress,
                            base_ref=integration_base_sha,
                            head_ref=deploy_sha,
                        )
                    self._pushes.assert_tree_unchanged(worktree, deploy_sha)
                    self._event(
                        conn,
                        lease_token=lease_token,
                        job_id=event_job_id,
                        phase="gating",
                        state="success",
                        message="All train gates passed",
                        detail=reused_validation_sha,
                    )
                except CommandFailed as exc:
                    if deploying_validated:
                        gate_mode = "validated reuse" if reused_validation_sha else "reassembly"
                        note = f"validated train gate failed after {gate_mode}: {exc}"
                        return [
                            finish(
                                job,
                                status="failed",
                                deploy_sha=deploy_sha,
                                log_path=str(log_path),
                                note=note,
                            )
                            for job in jobs
                        ]
                    if len(merged_jobs) == 1:
                        # The tree that failed is exactly the base plus this one
                        # job, so there is nothing to isolate: running it again
                        # would only retry a failed gate, and a pass could ship.
                        raise
                    log.write(
                        "\ntrain gate failed; probing "
                        f"{len(merged_jobs)} merged jobs for semantic conflicts\n"
                    )
                    self._event(
                        conn,
                        lease_token=lease_token,
                        job_id=event_job_id,
                        phase="gating",
                        state="warning",
                        message=(
                            "Train gate failed; probing "
                            f"{len(merged_jobs)} jobs for semantic conflicts"
                        ),
                        detail=f"exit_code={exc.returncode}",
                    )
                    results.extend(
                        self._bisect_failed_train(
                            conn,
                            merged_jobs,
                            merge_shas=merge_shas,
                            integration_base_sha=integration_base_sha,
                            worktree=worktree,
                            log=log,
                            log_path=log_path,
                            lease_token=lease_token,
                            deploy=deploy,
                            keep_worktree=keep_worktree or persistent_workspace,
                            owner=owner,
                            ttl_minutes=ttl_minutes,
                            expected_plan_sha=expected_plan_sha,
                        )
                    )
                    return results
                if deploy:
                    self._push_and_verify(
                        conn,
                        job_ids=[job.id for job in merged_jobs],
                        deploy_sha=deploy_sha,
                        lease_token=lease_token,
                        worktree=worktree,
                        log=log,
                        before_push=normal_pulse,
                        ownership_pulse=ownership_pulse,
                        state=deploy_state,
                        event_job_id=event_job_id,
                        expected_plan_sha=expected_plan_sha,
                        task_commits=[merge_shas.get(job.id, "") for job in merged_jobs],
                    )
                status = "deployed" if deploy else "validated"
                note = deploy_state.warning or (
                    f"batch ok; reused validation {reused_validation_sha}"
                    if reused_validation_sha
                    else f"batch ok; merged {len(merged_jobs)} job(s)"
                )
                train_id = uuid.uuid4().hex if not deploy else ""
                validated_at = utc_now() if not deploy else ""
                validation_identity_fields: dict[str, str] = {}
                if not deploy:
                    validation_identity_fields = self._validation.identity_fields(
                        jobs=merged_jobs,
                        train_id=train_id,
                        validated_heads=merge_shas,
                        validation_sha=deploy_sha,
                        worktree=worktree,
                        log=log,
                        pulse=normal_pulse,
                    )
                for job in merged_jobs:
                    validation_fields = {}
                    if not deploy:
                        validation_fields = {
                            "train_id": train_id,
                            "train_size": len(merged_jobs),
                            "validated_at": validated_at,
                            "validation_base_sha": integration_base_sha,
                            "validation_sha": deploy_sha,
                            "validated_head_sha": merge_shas[job.id],
                            **validation_identity_fields,
                        }
                    results.append(
                        finish(
                            job,
                            status=status,
                            deploy_sha=deploy_sha,
                            log_path=str(log_path),
                            note=note,
                            push_status=deploy_state.push_status,
                            verify_status=deploy_state.verify_status,
                            reused_validation_sha=reused_validation_sha,
                            **validation_fields,
                        )
                    )
                if deploy:
                    self._pushes.clear_pending_refs([job.id for job in merged_jobs], log=log)
                return results
            except LostLease:
                raise
            except CancellationRequested:
                if deploy_state.push_status == "succeeded":
                    return finish_active_after_error(
                        status="canceled",
                        note="canceled by user while the train was running",
                    )
                return cancel_active_jobs()
            except _AlreadyLanded as exc:
                return finish_active_jobs(status="queued", note=str(exc))
            except AmbiguousPush as exc:
                return finish_active_after_error(status="needs_reconcile", note=str(exc))
            except QueueBusy as exc:
                # This frame pushed and saw the refs land, so it can finalize
                # honestly. Anything less certain writes NOTHING: every status
                # write goes through the same contended database, and the ones
                # that succeed destroy durable evidence (mark_job clears the
                # pending-deploy marker on a requeue). push_status is also only
                # this frame's own: an isolated job's contention arrives here
                # through _process_isolated_jobs, where this state describes
                # the batch and not the job that actually pushed. Leaving the
                # rows as the last successful write left them makes contention
                # indistinguishable from a crash at the same instant, which
                # store.recover_orphans settles from the durable marker.
                if deploy_state.push_status != "succeeded":
                    raise
                return finish_active_after_error(status="deployed", note=str(exc))
            except CommandFailed as exc:
                return finish_active_after_error(status="failed", note=str(exc))
            except MergetrainError as exc:
                return finish_active_after_error(status="blocked", note=str(exc))
            except Exception as exc:  # pragma: no cover - defensive boundary
                return finish_active_after_error(status="failed", note=f"unexpected error: {exc}")
            finally:
                self._worktrees.cleanup(
                    worktree,
                    log=log,
                    keep_worktree=keep_worktree or persistent_workspace,
                )
