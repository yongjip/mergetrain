"""Integration worktree lifecycle and persistent validation cache policy."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import IO

from .command_runner import Pulse, run_command
from .config import MergetrainConfig
from .errors import MergetrainError
from .gate_runner import GateRunner
from .git_ops import git_common_dir, git_worktree_clean
from .reuse import gate_policy_sha


class WorktreeManager:
    """Create, restore, clean, and remove integration worktrees."""

    def __init__(self, config: MergetrainConfig, gates: GateRunner):
        self.config = config
        self.repo = config.repo
        self.gates = gates

    def ensure_state_dirs(self) -> None:
        self.config.state.logs.mkdir(parents=True, exist_ok=True)
        self.config.state.worktree_root.mkdir(parents=True, exist_ok=True)

    def worktree_path(self, first_job_id: int) -> Path:
        suffix = uuid.uuid4().hex[:8]
        name = f"{self.config.project.name}-mergetrain-{first_job_id}-{suffix}"
        return self.config.state.worktree_root / name

    def primary_path(self, first_job_id: int, *, deploy: bool) -> tuple[Path, bool]:
        persistent = not deploy and self.config.state.validation_workspace.mode == "persistent"
        if persistent:
            return self.config.validation_worktree_path, True
        return self.worktree_path(first_job_id), False

    def persistent_workspace_marker(self) -> Path:
        return (
            self.config.state.worktree_root
            / f".{self.config.project.name}-validation-workspace.json"
        )

    def cleanup(
        self,
        worktree: Path,
        *,
        log: IO[str] | None,
        keep_worktree: bool,
    ) -> None:
        if keep_worktree:
            if log:
                log.write(f"\nkeeping integration worktree: {worktree}\n")
            return
        if not worktree.exists():
            return
        try:
            run_command(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=self.repo,
                log=log,
                check=True,
            )
        except Exception:
            shutil.rmtree(worktree, ignore_errors=True)

    @staticmethod
    def git_common_dir(path: Path) -> Path | None:
        return git_common_dir(path)

    def persistent_cache_directories(self, worktree: Path) -> list[tuple[str, Path]]:
        directories: list[tuple[str, Path]] = []
        root = worktree.resolve()
        for relative in self.config.state.validation_workspace.cache_paths:
            path = worktree.joinpath(*relative.split("/"))
            if path.is_symlink():
                raise MergetrainError(f"persistent validation cache path is a symlink: {relative}")
            try:
                path.resolve().relative_to(root)
            except ValueError as exc:
                raise MergetrainError(
                    f"persistent validation cache escapes the worktree: {relative}"
                ) from exc
            tracked = run_command(
                ["git", "ls-files", "--", relative],
                cwd=worktree,
                check=True,
            )
            if tracked.stdout.strip():
                raise MergetrainError(
                    f"persistent validation cache path contains tracked files: {relative}"
                )
            path.mkdir(parents=True, exist_ok=True)
            ignored = run_command(
                ["git", "check-ignore", "--no-index", "--quiet", "--", relative],
                cwd=worktree,
                check=False,
            )
            if ignored.returncode != 0:
                raise MergetrainError(
                    f"persistent validation cache path must be ignored by Git: {relative}"
                )
            directories.append((relative, path))
        return directories

    def clean_untracked_except_validation_cache(
        self,
        *,
        worktree: Path,
        log: IO[str],
    ) -> list[tuple[str, Path]]:
        directories = self.persistent_cache_directories(worktree)
        command = ["git", "clean", "-ffdx"]
        for relative, _ in directories:
            command.append(f"--exclude=/{relative}/")
        run_command(command, cwd=worktree, log=log, check=True)
        return directories

    def prepare_persistent(
        self,
        *,
        worktree: Path,
        log: IO[str],
        pulse: Pulse | None,
    ) -> bool:
        reused = worktree.exists()
        if reused:
            repo_common = self.git_common_dir(self.repo)
            worktree_common = self.git_common_dir(worktree)
            if repo_common is None or worktree_common != repo_common:
                raise MergetrainError(
                    "persistent validation workspace exists but is not a worktree "
                    "owned by this repository; move it aside or run gc after "
                    "switching validation_workspace.mode to ephemeral"
                )
            run_command(
                ["git", "reset", "--hard", self.config.git.integration_ref],
                cwd=worktree,
                log=log,
                pulse=pulse,
                pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                timeout_seconds=self.config.queue.command_timeout_seconds,
            )
        else:
            self.persistent_workspace_marker().unlink(missing_ok=True)
            run_command(
                ["git", "worktree", "prune"],
                cwd=self.repo,
                log=log,
                check=True,
            )
            run_command(
                [
                    "git",
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree),
                    self.config.git.integration_ref,
                ],
                cwd=self.repo,
                log=log,
                pulse=pulse,
                pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                timeout_seconds=self.config.queue.command_timeout_seconds,
            )
        self.clean_untracked_except_validation_cache(worktree=worktree, log=log)
        if not git_worktree_clean(worktree):
            raise MergetrainError("persistent validation workspace could not be restored cleanly")
        return reused

    def activate_persistent_cache(
        self,
        *,
        worktree: Path,
        log: IO[str],
        pulse: Pulse | None,
    ) -> bool:
        workspace = self.config.state.validation_workspace
        identity = {
            "version": 1,
            "cache_key": workspace.cache_key,
            "gate_policy_sha": gate_policy_sha(self.config),
            "environment_sha": self.gates.environment_fingerprint(
                worktree=worktree,
                log=log,
                pulse=pulse,
            ),
        }
        marker = self.persistent_workspace_marker()
        previous: object = None
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        retained = previous == identity
        directories = self.persistent_cache_directories(worktree)
        if not retained:
            for _, directory in directories:
                if directory.is_symlink():
                    raise MergetrainError(
                        f"persistent validation cache path became a symlink: {directory}"
                    )
                shutil.rmtree(directory)
                directory.mkdir(parents=True)
        self.clean_untracked_except_validation_cache(worktree=worktree, log=log)
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_name(f"{marker.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, marker)
        return retained

    def prepare(
        self,
        *,
        worktree: Path,
        log: IO[str],
        pulse: Pulse | None,
        persistent: bool = False,
    ) -> bool:
        run_command(
            ["git", "fetch", self.config.git.remote],
            cwd=self.repo,
            log=log,
            pulse=pulse,
            pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )
        if persistent:
            return self.prepare_persistent(
                worktree=worktree,
                log=log,
                pulse=pulse,
            )
        run_command(
            [
                "git",
                "worktree",
                "add",
                "--detach",
                str(worktree),
                self.config.git.integration_ref,
            ],
            cwd=self.repo,
            log=log,
            pulse=pulse,
            pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )
        return False
