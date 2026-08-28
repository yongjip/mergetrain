"""Managed subprocess execution for Git, gates, and verify hooks."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO

from .config import MergetrainConfig
from .errors import CancellationRequested, CommandFailed, MergetrainError, redact_secrets

Pulse = Callable[[], None]


def _render_command(command: Sequence[str] | str) -> str:
    if isinstance(command, str):
        return command
    return " ".join(str(part) for part in command)


def _dashboard_command(command: Sequence[str] | str) -> str:
    """Render a bounded gate command while masking obvious inline secrets."""

    rendered = redact_secrets(_render_command(command))
    return rendered if len(rendered) <= 500 else f"{rendered[:497]}..."


def _posix_shell() -> str:
    """Return the POSIX shell used by gate and verify commands.

    Git for Windows ships ``sh.exe`` even though Windows has no ``/bin/sh``.
    Never fall back to ``cmd.exe``: command expansion and the documented gate
    contract both use POSIX shell syntax.
    """

    if Path("/bin/sh").exists():
        return "/bin/sh"
    shell = shutil.which("sh")
    if shell:
        return shell
    git = shutil.which("git")
    if git:
        git_root = Path(git).parent.parent
        for candidate in (
            git_root / "bin" / "sh.exe",
            git_root / "usr" / "bin" / "sh.exe",
        ):
            if candidate.exists():
                return str(candidate)
    raise MergetrainError("A POSIX sh executable is required to run gate and verify commands")


def _shell_command(command: str) -> list[str]:
    return [_posix_shell(), "-c", command]


def _stop_windows_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate a Windows child and every descendant it spawned."""

    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if (completed is None or completed.returncode != 0) and process.poll() is None:
        process.terminate()


def _stop_process(process: subprocess.Popen[str]) -> bool:
    if process.poll() is not None:
        return False
    stopped = False
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows compatibility
            _stop_windows_process_tree(process)
        stopped = True
        process.wait(timeout=5)
    except ProcessLookupError:
        process.wait()
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - Windows compatibility
                _stop_windows_process_tree(process)
                if process.poll() is None:
                    process.kill()
            stopped = True
            process.wait()
    return stopped


def _run_managed(
    command: Sequence[str],
    *,
    cwd: str | Path,
    env: dict[str, str] | None,
    log: IO[str] | None,
    check: bool,
    pulse: Pulse | None,
    pulse_interval_seconds: float,
    timeout_seconds: float | None,
    cancel_event: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one non-interactive process while enforcing pulse, timeout, and cancel."""

    if cancel_event is not None and cancel_event.is_set():
        raise CancellationRequested("command canceled before it started")
    if pulse is not None:
        pulse()
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        shell=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        start_new_session=os.name == "posix",
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        ),
    )
    stdout_tail: deque[str] = deque(maxlen=2000)
    stderr_tail: deque[str] = deque(maxlen=2000)
    log_lock = threading.Lock()

    def drain(stream: IO[str] | None, tail: deque[str]) -> None:
        if stream is None:
            return
        for line in iter(stream.readline, ""):
            tail.append(line)
            if log is not None:
                with log_lock:
                    log.write(line)
                    log.flush()
        stream.close()

    readers = [
        threading.Thread(target=drain, args=(process.stdout, stdout_tail), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr_tail), daemon=True),
    ]
    for reader in readers:
        reader.start()

    started = time.monotonic()
    next_pulse = started + max(0.1, pulse_interval_seconds)
    timed_out = False
    canceled = False
    try:
        while process.poll() is None:
            now = time.monotonic()
            if cancel_event is not None and cancel_event.is_set():
                if _stop_process(process):
                    canceled = True
                    stderr_tail.append("command canceled by gate scheduler\n")
                    break
                continue
            if timeout_seconds is not None and now - started >= timeout_seconds:
                if _stop_process(process):
                    timed_out = True
                    stderr_tail.append(f"command timed out after {timeout_seconds:g} seconds\n")
                    break
                continue
            if pulse is not None and now >= next_pulse:
                pulse()
                next_pulse = now + max(0.1, pulse_interval_seconds)
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
    except BaseException:
        _stop_process(process)
        raise
    finally:
        join_deadline = time.monotonic() + 2.0
        for reader in readers:
            reader.join(timeout=max(0.0, join_deadline - time.monotonic()))

    stdout = "".join(stdout_tail)
    stderr = "".join(stderr_tail)
    returncode = process.returncode if process.returncode is not None else 124
    if timed_out:
        returncode = 124
    completed = subprocess.CompletedProcess(command, returncode, stdout, stderr)
    if canceled:
        raise CancellationRequested("command canceled by gate scheduler")
    if check and completed.returncode != 0:
        raise CommandFailed(command, completed.returncode, stdout, stderr, str(cwd))
    return completed


# Local Git operations normally finish in seconds. The ceiling only prevents a
# pathological child from holding the sole runner indefinitely.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 600.0


def _git_safe_env(env: dict[str, str] | None) -> dict[str, str]:
    base = dict(os.environ) if env is None else dict(env)
    base.setdefault("GIT_TERMINAL_PROMPT", "0")
    return base


def run_command(
    command: Sequence[str],
    *,
    cwd: str | Path,
    env: dict[str, str] | None = None,
    log: IO[str] | None = None,
    check: bool = True,
    pulse: Pulse | None = None,
    pulse_interval_seconds: float = 10,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    if log:
        log.write(f"\n$ {_render_command(command)}\n")
        log.flush()
    env = _git_safe_env(env)
    if timeout_seconds is None:
        timeout_seconds = DEFAULT_COMMAND_TIMEOUT_SECONDS
    return _run_managed(
        list(command),
        cwd=cwd,
        env=env,
        log=log,
        check=check,
        pulse=pulse,
        pulse_interval_seconds=pulse_interval_seconds,
        timeout_seconds=timeout_seconds,
    )


def run_shell(
    command: str,
    *,
    cwd: str | Path,
    env: dict[str, str],
    log: IO[str] | None = None,
    check: bool = True,
    pulse: Pulse | None = None,
    pulse_interval_seconds: float = 10,
    timeout_seconds: float | None = None,
    cancel_event: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    if log:
        log.write(f"\n$ /bin/sh -c {redact_secrets(command)!r}\n")
        log.flush()
    env = _git_safe_env(env)
    if timeout_seconds is None:
        timeout_seconds = DEFAULT_COMMAND_TIMEOUT_SECONDS
    return _run_managed(
        _shell_command(command),
        cwd=cwd,
        env=env,
        log=log,
        check=check,
        pulse=pulse,
        pulse_interval_seconds=pulse_interval_seconds,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )


def expand_command(command: str, *, config: MergetrainConfig, worktree: Path) -> str:
    """Expand documented placeholders using POSIX-shell-safe quoting."""

    expanded = command

    def replace_path(placeholder: str, value: str) -> None:
        nonlocal expanded
        rendered: list[str] = []
        quote: str | None = None
        escaped = False
        index = 0
        while index < len(expanded):
            if not escaped and expanded.startswith(placeholder, index):
                if quote == "'":
                    replacement = value.replace("'", "'\"'\"'")
                elif quote == '"':
                    replacement = (
                        value.replace("\\", "\\\\")
                        .replace('"', '\\"')
                        .replace("$", "\\$")
                        .replace("`", "\\`")
                    )
                else:
                    replacement = shlex.quote(value)
                rendered.append(replacement)
                index += len(placeholder)
                continue

            char = expanded[index]
            rendered.append(char)
            if escaped:
                escaped = False
            elif char == "\\" and quote != "'":
                escaped = True
            elif char in {"'", '"'}:
                if quote is None:
                    quote = char
                elif quote == char:
                    quote = None
            index += 1
        expanded = "".join(rendered)

    replacements = {
        "${integration_ref}": config.git.integration_ref,
        "${project}": config.project.name,
    }
    for key, value in replacements.items():
        expanded = expanded.replace(key, value)
    replace_path("${repo}", str(config.repo))
    replace_path("${worktree}", str(worktree))
    return expanded


def command_env(*, config: MergetrainConfig, worktree: Path) -> dict[str, str]:
    """Build the non-interactive environment shared by gates and verify hooks."""

    env = os.environ.copy()
    inherited_path = env.get("PATH", "")
    runner_python = ""
    command_path = inherited_path
    if sys.executable:
        runner_python = os.path.abspath(os.path.expanduser(sys.executable))
        runner_bin = str(Path(runner_python).parent)
        runner_bin_key = os.path.normcase(os.path.abspath(runner_bin))
        path_entries = [
            entry
            for entry in inherited_path.split(os.pathsep)
            if entry and os.path.normcase(os.path.abspath(entry)) != runner_bin_key
        ]
        command_path = os.pathsep.join((runner_bin, *path_entries))
    env.update(
        {
            "PATH": command_path,
            "MERGETRAIN_PROJECT": config.project.name,
            "MERGETRAIN_INTEGRATION_REF": config.git.integration_ref,
            "MERGETRAIN_REPO": str(config.repo),
            "MERGETRAIN_RUNNER_PYTHON": runner_python,
            "MERGETRAIN_WORKTREE": str(worktree),
        }
    )
    return env
