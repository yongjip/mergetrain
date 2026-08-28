"""Git primitives and repository cleanup used by runner and recovery flows."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import IO, Any

from .command_runner import Pulse, run_command
from .config import MergetrainConfig
from .errors import MergetrainError


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
    """Return cleanliness, failing closed when Git cannot establish it."""

    return git_output(["status", "--porcelain"], cwd=path) == ""


_REF_REJECTION = re.compile(
    r"^\s*!\s+\[(?:remote\s+)?rejected\]\s+.+\s+\(.+\)\s*$",
    re.IGNORECASE,
)
_FORGE_POLICY_REJECTION = re.compile(
    r"^\s*remote:\s+(?:error:\s+)?GH(?:006|013)\b",
    re.IGNORECASE,
)
_PERMISSION_REJECTION = re.compile(
    r"^\s*(?:remote:\s+)?(?:error:\s+|fatal:\s+)?permission to .+ denied",
    re.IGNORECASE,
)


def is_push_rejection(stderr: str) -> bool:
    """Return whether stderr proves the remote refused the ref update."""

    return any(
        _REF_REJECTION.match(line)
        or _FORGE_POLICY_REJECTION.match(line)
        or _PERMISSION_REJECTION.match(line)
        for line in (stderr or "").splitlines()
    )


def git_dirty_paths(path: str | Path, *, limit: int = 5) -> list[str]:
    lines = git_output_or_empty(["status", "--porcelain"], cwd=path).splitlines()
    paths = [line[3:].strip() for line in lines if len(line) > 3]
    return paths[:limit]


def git_remote_url(path: str | Path, remote: str) -> str:
    return git_output_or_empty(["remote", "get-url", remote], cwd=path)


def git_remote_exists(path: str | Path, remote: str) -> bool:
    return bool(git_remote_url(path, remote))


def git_remote_ref_sha(
    path: str | Path,
    remote: str,
    ref: str,
    *,
    log: IO[str] | None = None,
    pulse: Pulse | None = None,
    pulse_interval_seconds: float = 10,
    timeout_seconds: float | None = None,
) -> tuple[bool, str]:
    """Resolve one exact remote ref without accepting a suffix match."""

    completed = run_command(
        ["git", "ls-remote", "--refs", remote, ref],
        cwd=path,
        log=log,
        check=False,
        pulse=pulse,
        pulse_interval_seconds=pulse_interval_seconds,
        timeout_seconds=timeout_seconds,
    )
    if completed.returncode != 0:
        return False, ""
    target = ref if ref.startswith("refs/") else f"refs/heads/{ref}"
    for line in completed.stdout.strip().splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) >= 2 and parts[1].strip() == target:
            return True, parts[0].strip()
    return True, ""


def git_ref_exists(path: str | Path, ref: str) -> bool:
    completed = run_command(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=path,
        check=False,
    )
    return completed.returncode == 0


def git_rev_parse(path: str | Path, ref: str) -> str:
    return git_output(["rev-parse", f"{ref}^{{commit}}"], cwd=path)


def git_tree_sha(path: str | Path, ref: str) -> str:
    return git_output(["rev-parse", f"{ref}^{{tree}}"], cwd=path)


PENDING_REF_PREFIX = "refs/mergetrain/pending/"
DEPLOY_AUDIT_REF_PREFIX = "refs/mergetrain/deploys/"


def deploy_audit_ref_name(deploy_sha: str) -> str:
    """Return the immutable content-addressed remote deploy evidence ref."""

    normalized = deploy_sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", normalized):
        raise MergetrainError(
            f"cannot build deploy audit ref from invalid commit id {deploy_sha!r}"
        )
    return f"{DEPLOY_AUDIT_REF_PREFIX}{normalized}"


def pending_ref_name(job_id: int) -> str:
    """Return the local ref that pins a pending deployment for recovery."""

    return f"{PENDING_REF_PREFIX}{job_id}"


def resolve_pending_ref(path: str | Path, job_id: int) -> str:
    return git_output_or_empty(["rev-parse", f"{pending_ref_name(job_id)}^{{commit}}"], cwd=path)


def delete_pending_ref(path: str | Path, job_id: int, *, log: IO[str] | None = None) -> None:
    run_command(
        ["git", "update-ref", "-d", pending_ref_name(job_id)],
        cwd=path,
        log=log,
        check=False,
    )


def find_worktree_gc_candidates(
    config: MergetrainConfig, *, protect: Iterable[str] = ()
) -> list[dict[str, Any]]:
    root = config.state.worktree_root
    prefix = f"{config.project.name}-mergetrain-"
    if not root.exists():
        return []
    protected = {str(Path(path)) for path in protect if path}
    candidates: list[dict[str, Any]] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        if path == config.validation_worktree_path:
            if str(path) in protected:
                candidates.append(
                    {
                        "path": str(path),
                        "reason": "active runner worktree, skipped",
                        "protected": True,
                    }
                )
            elif config.state.validation_workspace.mode == "persistent":
                candidates.append(
                    {
                        "path": str(path),
                        "reason": "configured persistent validation workspace, skipped",
                        "protected": True,
                    }
                )
            else:
                candidates.append(
                    {
                        "path": str(path),
                        "reason": "disabled persistent validation workspace",
                    }
                )
            continue
        if not path.name.startswith(prefix):
            continue
        if str(path) in protected:
            candidates.append(
                {
                    "path": str(path),
                    "reason": "active runner worktree, skipped",
                    "protected": True,
                }
            )
            continue
        candidates.append({"path": str(path), "reason": "temporary mergetrain worktree"})
    return candidates


def branch_exists(repo: Path, branch: str) -> bool:
    return (
        run_command(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo,
            check=False,
        ).returncode
        == 0
    )


def current_branch(repo: Path) -> str:
    return git_current_branch(repo)


def apply_gc(
    config: MergetrainConfig,
    *,
    delete_branches: Iterable[str] = (),
    protect: Iterable[str] = (),
    live_worktree_now: Callable[[], str | None] | None = None,
) -> dict[str, list[dict[str, str]]]:
    removed_worktrees: list[dict[str, str]] = []
    deleted_branches: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for candidate in find_worktree_gc_candidates(config, protect=protect):
        if candidate.get("protected"):
            continue
        path = Path(candidate["path"])
        if live_worktree_now is not None:
            active = live_worktree_now()
            if active and Path(active) == path:
                continue
        try:
            run_command(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=config.repo,
                check=True,
            )
        except Exception:
            shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed_worktrees.append(
                {"path": str(candidate["path"]), "reason": str(candidate["reason"])}
            )
            if path == config.validation_worktree_path:
                config_marker = (
                    config.state.worktree_root / f".{config.project.name}-validation-workspace.json"
                )
                config_marker.unlink(missing_ok=True)
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
            failed.append(
                {
                    "branch": branch,
                    "reason": completed.stderr.strip() or "delete failed",
                }
            )
    return {
        "removed_worktrees": removed_worktrees,
        "deleted_branches": deleted_branches,
        "failed": failed,
    }
