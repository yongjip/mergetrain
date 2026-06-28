"""Git worktree runner for trainyard."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import IO, Iterable, Sequence

from .config import GateConfig, TrainyardConfig
from .errors import CommandFailed, MergeBlocked, TrainyardError
from .models import Job
from .store import mark_job, utc_now


def _render_command(command: Sequence[str] | str) -> str:
    if isinstance(command, str):
        return command
    return " ".join(str(part) for part in command)


def run_command(
    command: Sequence[str],
    *,
    cwd: str | Path,
    env: dict[str, str] | None = None,
    log: IO[str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if log:
        log.write(f"\n$ {_render_command(command)}\n")
        log.flush()
    completed = subprocess.run(
        list(command),
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if log:
        if completed.stdout:
            log.write(completed.stdout)
        if completed.stderr:
            log.write(completed.stderr)
        log.flush()
    if check and completed.returncode != 0:
        raise CommandFailed(command, completed.returncode, completed.stdout, completed.stderr, str(cwd))
    return completed


def run_shell(
    command: str,
    *,
    cwd: str | Path,
    env: dict[str, str],
    log: IO[str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if log:
        log.write(f"\n$ /bin/sh -c {command!r}\n")
        log.flush()
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        shell=True,
        executable="/bin/sh" if Path("/bin/sh").exists() else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if log:
        if completed.stdout:
            log.write(completed.stdout)
        if completed.stderr:
            log.write(completed.stderr)
        log.flush()
    if check and completed.returncode != 0:
        raise CommandFailed(command, completed.returncode, completed.stdout, completed.stderr, str(cwd))
    return completed


def git_output(args: Sequence[str], *, cwd: str | Path) -> str:
    completed = run_command(["git", *args], cwd=cwd, check=True)
    return completed.stdout.strip()


def git_output_or_empty(args: Sequence[str], *, cwd: str | Path) -> str:
    completed = run_command(["git", *args], cwd=cwd, check=False)
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def git_repo_root(path: str | Path) -> str:
    return git_output_or_empty(["rev-parse", "--show-toplevel"], cwd=path)


def git_current_branch(path: str | Path) -> str:
    return git_output_or_empty(["branch", "--show-current"], cwd=path)


def git_worktree_clean(path: str | Path) -> bool:
    return git_output_or_empty(["status", "--porcelain"], cwd=path) == ""


def git_remote_url(path: str | Path, remote: str) -> str:
    return git_output_or_empty(["remote", "get-url", remote], cwd=path)


def git_remote_exists(path: str | Path, remote: str) -> bool:
    return bool(git_remote_url(path, remote))


def git_ref_exists(path: str | Path, ref: str) -> bool:
    completed = run_command(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=path, check=False)
    return completed.returncode == 0


def git_rev_parse(path: str | Path, ref: str) -> str:
    return git_output(["rev-parse", f"{ref}^{{commit}}"], cwd=path)


def expand_command(command: str, *, config: TrainyardConfig, worktree: Path) -> str:
    replacements = {
        "${integration_ref}": config.git.integration_ref,
        "${project}": config.project.name,
        "${repo}": str(config.repo),
        "${worktree}": str(worktree),
    }
    expanded = command
    for key, value in replacements.items():
        expanded = expanded.replace(key, value)
    return expanded


def command_env(*, config: TrainyardConfig, worktree: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "TRAINYARD_PROJECT": config.project.name,
            "TRAINYARD_INTEGRATION_REF": config.git.integration_ref,
            "TRAINYARD_REPO": str(config.repo),
            "TRAINYARD_WORKTREE": str(worktree),
        }
    )
    return env


class GitRunner:
    """Executes queued branches in temporary Git worktrees."""

    def __init__(self, config: TrainyardConfig):
        self.config = config
        self.repo = config.repo

    def _ensure_state_dirs(self) -> None:
        self.config.state.logs.mkdir(parents=True, exist_ok=True)
        self.config.state.worktree_root.mkdir(parents=True, exist_ok=True)

    def _log_path(self, prefix: str, first_job_id: int) -> Path:
        stamp = utc_now().replace(":", "").replace("-", "").replace("Z", "")
        return self.config.state.logs / f"{prefix}-{first_job_id}-{stamp}.log"

    def _worktree_path(self, first_job_id: int) -> Path:
        suffix = uuid.uuid4().hex[:8]
        name = f"{self.config.project.name}-trainyard-{first_job_id}-{suffix}"
        return self.config.state.worktree_root / name

    def _cleanup_worktree(self, worktree: Path, *, log: IO[str] | None, keep_worktree: bool) -> None:
        if keep_worktree:
            if log:
                log.write(f"\nkeeping integration worktree: {worktree}\n")
            return
        if not worktree.exists():
            return
        try:
            run_command(["git", "worktree", "remove", "--force", str(worktree)], cwd=self.repo, log=log, check=True)
        except Exception:
            shutil.rmtree(worktree, ignore_errors=True)

    def _run_gate(self, gate: GateConfig, *, worktree: Path, log: IO[str]) -> None:
        command = expand_command(gate.run, config=self.config, worktree=worktree)
        env = command_env(config=self.config, worktree=worktree)
        log.write(f"\n## gate: {gate.name}\n")
        run_shell(command, cwd=worktree, env=env, log=log, check=True)

    def _run_gates(self, *, worktree: Path, log: IO[str]) -> None:
        run_command(["git", "diff", "--check", f"{self.config.git.integration_ref}..HEAD"], cwd=worktree, log=log)
        for gate in self.config.gates:
            self._run_gate(gate, worktree=worktree, log=log)

    def _run_verify_hooks(self, *, worktree: Path, log: IO[str]) -> None:
        for hook in self.config.deploy.verify:
            command = expand_command(hook.run, config=self.config, worktree=worktree)
            env = command_env(config=self.config, worktree=worktree)
            log.write(f"\n## verify: {hook.name}\n")
            run_shell(command, cwd=worktree, env=env, log=log, check=True)

    def push_verified_head(self, *, worktree: Path, log: IO[str] | None = None) -> None:
        if not self.config.git.push_refs:
            raise TrainyardError("git.push_refs must not be empty for deploy mode")
        push_args = ["git", "push", "--atomic", self.config.git.remote]
        push_args.extend(f"HEAD:{ref}" for ref in self.config.git.push_refs)
        run_command(push_args, cwd=worktree, log=log)

    def _prepare_worktree(self, *, worktree: Path, log: IO[str]) -> None:
        run_command(["git", "fetch", self.config.git.remote], cwd=self.repo, log=log)
        run_command(
            ["git", "worktree", "add", "--detach", str(worktree), self.config.git.integration_ref],
            cwd=self.repo,
            log=log,
        )

    def process_one(
        self,
        conn,
        job: Job,
        *,
        deploy: bool,
        keep_worktree: bool = False,
    ) -> Job:
        self._ensure_state_dirs()
        log_path = self._log_path("job", job.id)
        worktree = self._worktree_path(job.id)
        deploy_sha = ""
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"trainyard job {job.id}: {job.task}\n")
            log.write(f"branch: {job.branch}\nmode: {'deploy' if deploy else 'validate'}\n")
            try:
                self._prepare_worktree(worktree=worktree, log=log)
                merge = run_command(["git", "merge", "--no-edit", job.branch], cwd=worktree, log=log, check=False)
                if merge.returncode != 0:
                    raise MergeBlocked(merge.stderr.strip() or merge.stdout.strip() or f"merge failed for {job.branch}")
                if not git_worktree_clean(worktree):
                    raise MergeBlocked("integration worktree is dirty after merge")
                deploy_sha = git_rev_parse(worktree, "HEAD")
                self._run_gates(worktree=worktree, log=log)
                verify_warning = ""
                if deploy:
                    self.push_verified_head(worktree=worktree, log=log)
                    try:
                        self._run_verify_hooks(worktree=worktree, log=log)
                    except CommandFailed as exc:
                        verify_warning = f"post-push verify warning: {exc}"
                        log.write(f"\nWARNING: {verify_warning}\n")
                status = "deployed" if deploy else "validated"
                note = verify_warning or "ok"
                return mark_job(conn, job.id, status=status, deploy_sha=deploy_sha, log_path=str(log_path), note=note)
            except MergeBlocked as exc:
                return mark_job(conn, job.id, status="blocked", deploy_sha=deploy_sha, log_path=str(log_path), note=str(exc))
            except CommandFailed as exc:
                return mark_job(conn, job.id, status="failed", deploy_sha=deploy_sha, log_path=str(log_path), note=str(exc))
            except TrainyardError as exc:
                return mark_job(conn, job.id, status="blocked", deploy_sha=deploy_sha, log_path=str(log_path), note=str(exc))
            except Exception as exc:  # pragma: no cover - defensive boundary
                return mark_job(conn, job.id, status="failed", deploy_sha=deploy_sha, log_path=str(log_path), note=f"unexpected error: {exc}")
            finally:
                self._cleanup_worktree(worktree, log=log, keep_worktree=keep_worktree)

    def process_batch(
        self,
        conn,
        jobs: Iterable[Job],
        *,
        deploy: bool,
        keep_worktree: bool = False,
    ) -> list[Job]:
        jobs = list(jobs)
        if not jobs:
            return []
        self._ensure_state_dirs()
        log_path = self._log_path("batch", jobs[0].id)
        worktree = self._worktree_path(jobs[0].id)
        merged_jobs: list[Job] = []
        results: list[Job] = []
        deploy_sha = ""
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"trainyard batch starting at job {jobs[0].id}\n")
            log.write(f"jobs: {[job.id for job in jobs]}\nmode: {'deploy' if deploy else 'validate'}\n")
            try:
                self._prepare_worktree(worktree=worktree, log=log)
                for job in jobs:
                    log.write(f"\n## merge job {job.id}: {job.branch}\n")
                    merge = run_command(["git", "merge", "--no-edit", job.branch], cwd=worktree, log=log, check=False)
                    if merge.returncode != 0:
                        note = merge.stderr.strip() or merge.stdout.strip() or f"merge failed for {job.branch}"
                        results.append(mark_job(conn, job.id, status="blocked", log_path=str(log_path), note=note))
                        run_command(["git", "merge", "--abort"], cwd=worktree, log=log, check=False)
                        continue
                    if not git_worktree_clean(worktree):
                        results.append(
                            mark_job(
                                conn,
                                job.id,
                                status="blocked",
                                log_path=str(log_path),
                                note="integration worktree is dirty after merge",
                            )
                        )
                        run_command(["git", "reset", "--hard", "HEAD"], cwd=worktree, log=log, check=False)
                        continue
                    merged_jobs.append(job)
                if not merged_jobs:
                    log.write("\nno jobs were merged\n")
                    return results
                deploy_sha = git_rev_parse(worktree, "HEAD")
                try:
                    self._run_gates(worktree=worktree, log=log)
                except CommandFailed as exc:
                    log.write("\ntrain gate failed; isolating merged jobs one-by-one\n")
                    self._cleanup_worktree(worktree, log=log, keep_worktree=False)
                    for job in merged_jobs:
                        results.append(self.process_one(conn, job, deploy=deploy, keep_worktree=keep_worktree))
                    return results
                verify_warning = ""
                if deploy:
                    self.push_verified_head(worktree=worktree, log=log)
                    try:
                        self._run_verify_hooks(worktree=worktree, log=log)
                    except CommandFailed as exc:
                        verify_warning = f"post-push verify warning: {exc}"
                        log.write(f"\nWARNING: {verify_warning}\n")
                status = "deployed" if deploy else "validated"
                note = verify_warning or f"batch ok; merged {len(merged_jobs)} job(s)"
                for job in merged_jobs:
                    results.append(mark_job(conn, job.id, status=status, deploy_sha=deploy_sha, log_path=str(log_path), note=note))
                return results
            except CommandFailed as exc:
                for job in merged_jobs or jobs:
                    results.append(mark_job(conn, job.id, status="failed", deploy_sha=deploy_sha, log_path=str(log_path), note=str(exc)))
                return results
            except TrainyardError as exc:
                for job in merged_jobs or jobs:
                    results.append(mark_job(conn, job.id, status="blocked", deploy_sha=deploy_sha, log_path=str(log_path), note=str(exc)))
                return results
            except Exception as exc:  # pragma: no cover - defensive boundary
                for job in merged_jobs or jobs:
                    results.append(mark_job(conn, job.id, status="failed", deploy_sha=deploy_sha, log_path=str(log_path), note=f"unexpected error: {exc}"))
                return results
            finally:
                self._cleanup_worktree(worktree, log=log, keep_worktree=keep_worktree)


def find_worktree_gc_candidates(config: TrainyardConfig) -> list[dict[str, str]]:
    root = config.state.worktree_root
    prefix = f"{config.project.name}-trainyard-"
    if not root.exists():
        return []
    candidates = []
    for path in sorted(root.iterdir()):
        if path.is_dir() and path.name.startswith(prefix):
            candidates.append({"path": str(path), "reason": "temporary trainyard worktree"})
    return candidates


def branch_exists(repo: Path, branch: str) -> bool:
    return run_command(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo, check=False).returncode == 0


def current_branch(repo: Path) -> str:
    return git_current_branch(repo)


def apply_gc(config: TrainyardConfig, *, delete_branches: Iterable[str] = ()) -> dict[str, list[dict[str, str]]]:
    removed_worktrees: list[dict[str, str]] = []
    deleted_branches: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for candidate in find_worktree_gc_candidates(config):
        path = Path(candidate["path"])
        try:
            run_command(["git", "worktree", "remove", "--force", str(path)], cwd=config.repo, check=True)
        except Exception:
            shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed_worktrees.append(candidate)
        else:
            failed.append({"path": str(path), "reason": "could not remove worktree"})
    active_branch = current_branch(config.repo)
    for branch in delete_branches:
        if branch == active_branch:
            failed.append({"branch": branch, "reason": "currently checked out"})
            continue
        if not branch_exists(config.repo, branch):
            continue
        completed = run_command(["git", "branch", "-D", branch], cwd=config.repo, check=False)
        if completed.returncode == 0:
            deleted_branches.append({"branch": branch, "reason": "terminal queue branch"})
        else:
            failed.append({"branch": branch, "reason": completed.stderr.strip() or "delete failed"})
    return {"removed_worktrees": removed_worktrees, "deleted_branches": deleted_branches, "failed": failed}
