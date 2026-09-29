"""Git primitives and repository cleanup used by runner and recovery flows."""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Callable, Iterable, Mapping, Sequence
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


def git_common_dir(path: str | Path) -> Path | None:
    """Return the repository's resolved common Git directory, if provable."""

    completed = run_command(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=path,
        check=False,
    )
    if completed.returncode != 0:
        return None
    raw = completed.stdout.rstrip("\r\n")
    if not raw:
        return None
    common = Path(raw)
    if not common.is_absolute():
        common = Path(path) / common
    return common.resolve()


def _worktree_records(output: str) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain -z`` without path injection."""

    records: list[dict[str, str]] = []
    record: dict[str, str] = {}
    for field in output.split("\0"):
        if field == "":
            if record:
                records.append(record)
                record = {}
            continue
        key, separator, value = field.partition(" ")
        record[key] = value if separator else ""
    if record:
        records.append(record)
    return records


def _parse_worktree_porcelain(output: str, branch: str) -> tuple[Path, ...]:
    expected_ref = f"refs/heads/{branch}"
    # Porcelain -z makes the complete path one NUL-delimited value. Do not
    # strip it: spaces and newlines are valid path characters.
    return tuple(
        Path(record["worktree"])
        for record in _worktree_records(output)
        if record.get("branch") == expected_ref
        and "worktree" in record
        and "prunable" not in record
    )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


# gc decides what is its own from the files Git itself reads, with no Git
# command: a checkout's `.git` gitfile names its worktree admin directory, and
# the admin directory's `commondir` names the repository it belongs to.


def _read_marker(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def _gitfile_target(checkout: Path) -> Path | None:
    """The git directory that ``checkout/.git`` names, when it is a gitfile."""

    pointer = _read_marker(checkout / ".git")
    if not pointer.startswith("gitdir:"):
        return None
    target = Path(pointer[len("gitdir:") :].strip())
    return target if target.is_absolute() else checkout / target


def _admin_common_dir(admin: Path) -> Path:
    """The repository an admin directory belongs to; its own when unshared."""

    relative = _read_marker(admin / "commondir")
    if not relative:
        return admin
    common = Path(relative)
    return common if common.is_absolute() else admin / common


def _checkout_common_dir(checkout: Path) -> Path | None:
    dot_git = checkout / ".git"
    if dot_git.is_dir():
        return dot_git
    admin = _gitfile_target(checkout)
    return None if admin is None else _admin_common_dir(admin)


def _repository_common_dir(repo: Path) -> Path | None:
    # Only a repo path below the top of its checkout needs Git to find it.
    return _checkout_common_dir(repo) or git_common_dir(repo)


def worktree_keep_reason(repo: Path, worktree: Path) -> str:
    """Why cleanup must leave ``worktree`` alone, or "" when it may go.

    Cleanup removes only this repository's own unlocked worktree, or a
    directory that no repository uses any more. Git refuses to remove a
    locked worktree, and another clone's live worktree can sit in a shared
    worktree_root under a name gc recognizes.
    """

    if (worktree / ".git").is_dir():
        return "it is a repository of its own"
    admin = _gitfile_target(worktree)
    if admin is None or not admin.is_dir():
        return ""
    ours = _repository_common_dir(repo)
    if ours is None or not _same_path(_admin_common_dir(admin), ours):
        return "it is a worktree of another repository"
    if (admin / "locked").exists():
        return "it is locked with `git worktree lock`"
    return ""


def _admin_worktree_path(admin: Path, common: Path) -> str:
    if _same_path(admin, common):
        return str(common.parent if common.name == ".git" else common)
    gitfile = _read_marker(admin / "gitdir")
    return str(Path(gitfile).parent) if gitfile else str(admin)


def branch_worktree_use(repo: Path, branch: str) -> str:
    """Why a worktree still needs ``branch``, or "" when none does.

    These are the uses for which `git branch -d` refuses to delete a branch.
    `git worktree list` shows a worktree that is rebasing or bisecting the
    branch as detached, and one whose directory is missing only as prunable,
    so its porcelain output misses them. Each worktree's HEAD, rebase
    head-name, and BISECT_START live in its admin directory, which stays
    while the directory is away.
    """

    common = _repository_common_dir(repo)
    if common is None:
        return "its repository's worktrees could not be read"
    ref = f"refs/heads/{branch}"
    admins = [common]
    linked = common / "worktrees"
    if linked.is_dir():
        admins.extend(sorted(path for path in linked.iterdir() if path.is_dir()))
    for admin in admins:
        where = _admin_worktree_path(admin, common)
        if _read_marker(admin / "HEAD") == f"ref: {ref}":
            return f"checked out in {where}"
        if {
            _read_marker(admin / "rebase-merge" / "head-name"),
            _read_marker(admin / "rebase-apply" / "head-name"),
        } & {ref, branch}:
            return f"being rebased in {where}"
        if _read_marker(admin / "BISECT_START") in {ref, branch}:
            return f"being bisected in {where}"
    return ""


def is_linked_worktree(repo: str | Path, path: Path) -> bool:
    """Whether ``path`` is itself a registered linked worktree of ``repo``.

    Git walks up from a directory without a ``.git`` of its own, so a leftover
    directory inside the control checkout still answers every Git command, but
    for the control checkout (#221). Require both that Git places the top of
    the working tree at ``path`` and that ``repo`` lists ``path`` as a live
    linked worktree.
    """

    toplevel = git_repo_root(path)
    if not toplevel or not _same_path(Path(toplevel), path):
        return False
    completed = run_command(
        ["git", "worktree", "list", "--porcelain", "-z"],
        cwd=repo,
        check=True,
    )
    # The first record is the main worktree, which is never a linked one.
    return any(
        "worktree" in record
        and "prunable" not in record
        and _same_path(Path(record["worktree"]), path)
        for record in _worktree_records(completed.stdout)[1:]
    )


def git_worktrees_for_branch(path: str | Path, branch: str) -> tuple[Path, ...]:
    """Return live registered worktrees with exactly ``branch`` checked out."""

    completed = run_command(
        ["git", "worktree", "list", "--porcelain", "-z"],
        cwd=path,
        check=True,
    )
    return _parse_worktree_porcelain(completed.stdout, branch)


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


def git_remote_push_urls(path: str | Path, remote: str) -> tuple[str, ...]:
    """Return every effective push URL after Git's normal rewrite rules."""

    output = git_output_or_empty(
        ["remote", "get-url", "--push", "--all", remote], cwd=path
    )
    return tuple(line for line in output.splitlines() if line)


def git_remote_exists(path: str | Path, remote: str) -> bool:
    return bool(git_remote_url(path, remote))


def git_remote_ref_sha(
    path: str | Path,
    remote: str,
    ref: str,
    *,
    env: dict[str, str] | None = None,
    log: IO[str] | None = None,
    pulse: Pulse | None = None,
    pulse_interval_seconds: float = 10,
    timeout_seconds: float | None = None,
) -> tuple[bool, str]:
    """Resolve one exact remote ref without accepting a suffix match."""

    completed = run_command(
        ["git", "ls-remote", "--refs", remote, ref],
        cwd=path,
        env=env,
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
        keep = worktree_keep_reason(config.repo, path)
        if keep:
            candidates.append(
                {"path": str(path), "reason": f"{keep}, skipped", "protected": True}
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


# IsReparseTagNameSurrogate(): the reparse point names another file or
# directory, as NTFS junctions and symbolic links do.
_NAME_SURROGATE_REPARSE_TAG = 0x20000000


def _is_link_stat(st: os.stat_result) -> bool:
    # Windows reports an NTFS junction as an ordinary directory; only its
    # reparse tag shows that it names a directory somewhere else.
    tag = getattr(st, "st_reparse_tag", 0)
    return stat.S_ISLNK(st.st_mode) or bool(tag & _NAME_SURROGATE_REPARSE_TAG)


def _is_link(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    return entry.is_dir(follow_symlinks=False) and _is_link_stat(
        entry.stat(follow_symlinks=False)
    )


def _remove_links_below(root: Path) -> None:
    """Remove every link below ``root`` as a link, never what it points to.

    Git for Windows deletes a worktree by recursing into NTFS junctions, which
    empties any external directory a gate linked into it (#214, #216). Once the
    links are gone, a recursive delete can reach only the worktree's own files.
    """

    if _is_link_stat(os.lstat(root)):
        raise OSError(f"{root} is itself a link")
    pending = [os.fspath(root)]
    while pending:
        with os.scandir(pending.pop()) as scan:
            entries = list(scan)
        for entry in entries:
            if _is_link(entry):
                attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
                if attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
                    os.rmdir(entry.path)  # a Windows directory link; the target is untouched
                else:
                    os.unlink(entry.path)
            elif entry.is_dir(follow_symlinks=False):
                pending.append(entry.path)


def remove_worktree(repo: Path, worktree: Path, *, log: IO[str] | None = None) -> None:
    """Delete a linked worktree without deleting anything outside it.

    The worktree is kept when cleanup does not own it (see
    ``worktree_keep_reason``), when its path is itself a link, or when a link
    inside it cannot be removed on its own, because the recursive delete could
    then reach through that link.
    """

    keep = worktree_keep_reason(repo, worktree)
    if keep:
        if log:
            log.write(f"\nkeeping worktree: {worktree} ({keep})\n")
        return
    try:
        _remove_links_below(worktree)
    except OSError as exc:
        if log:
            log.write(
                f"\nkeeping integration worktree: {worktree} "
                f"(could not remove it without following a link: {exc})\n"
            )
        return
    try:
        run_command(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repo,
            log=log,
            check=True,
        )
    except Exception:
        # No repository uses the directory any more, so Git has nothing left
        # to remove it through.
        shutil.rmtree(worktree, ignore_errors=True)


def _is_ancestor(repo: Path, commit: str, descendant: str) -> bool:
    completed = run_command(
        ["git", "merge-base", "--is-ancestor", commit, descendant],
        cwd=repo,
        check=False,
    )
    return completed.returncode == 0


def branch_deletion_blocker(
    config: MergetrainConfig,
    branch: str,
    recorded_head: str,
    *,
    landed_sha: str = "",
) -> str:
    """Why gc must keep ``branch``, or "" when deleting it loses no commit.

    The branch must still point at the head its job recorded, so nothing
    committed afterwards goes with it (#223). That head must also have landed:
    it is merged into the integration ref, or into ``landed_sha``, the commit
    its job pushed. A canceled branch that never landed is kept.
    """

    integration_ref = config.git.integration_ref
    if branch in config.git.push_refs or branch == config.git.integration_branch:
        return "protected integration branch"
    if not recorded_head:
        return "its job recorded no head to compare against"
    current = git_output_or_empty(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"],
        cwd=config.repo,
    )
    recorded = git_output_or_empty(
        ["rev-parse", "--verify", "--quiet", f"{recorded_head}^{{commit}}"],
        cwd=config.repo,
    )
    if not current:
        return "branch does not exist"
    if not recorded or current != recorded:
        return f"moved since its job recorded {recorded_head[:12]}"
    in_use = branch_worktree_use(config.repo, branch)
    if in_use:
        return in_use
    if _is_ancestor(config.repo, recorded, config.git.integration_tracking_ref):
        return ""
    if landed_sha and _is_ancestor(config.repo, recorded, landed_sha):
        return ""
    return f"not merged into {integration_ref}"


def apply_gc(
    config: MergetrainConfig,
    *,
    delete_branches: Mapping[str, str] | None = None,
    protect: Iterable[str] = (),
    live_worktree_now: Callable[[], str | None] | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Remove terminal worktrees and, given ``{branch: recorded head}``, branches."""

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
        remove_worktree(config.repo, path)
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
    for branch, recorded_head in dict(delete_branches or {}).items():
        if not branch_exists(config.repo, branch):
            continue
        in_use = branch_worktree_use(config.repo, branch)
        if in_use:
            failed.append({"branch": branch, "reason": in_use})
            continue
        # Compare-and-delete: a commit that lands on the branch after the caller
        # checked it makes the update fail instead of being thrown away (#223).
        completed = run_command(
            ["git", "update-ref", "-d", f"refs/heads/{branch}", recorded_head],
            cwd=config.repo,
            check=False,
        )
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
