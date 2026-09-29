"""Managed subprocess execution for Git, gates, and verify hooks."""

from __future__ import annotations

import io
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, cast

from .config import MergetrainConfig
from .errors import CancellationRequested, CommandFailed, MergetrainError, redact_secrets
from .shell_quoting import expand_path_placeholders
from .windows_job import CREATE_SUSPENDED, WindowsJob

Pulse = Callable[[], None]


class _RedactingLog(io.TextIOBase):
    """Mask secrets in subprocess output without closing the caller's log."""

    def __init__(self, wrapped: IO[str]):
        self._wrapped = wrapped

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        return self._wrapped.write(redact_secrets(text))

    def flush(self) -> None:
        # Garbage collection can finalize this wrapper after its caller closed
        # the log, and finalizing flushes.
        if not getattr(self._wrapped, "closed", False):
            self._wrapped.flush()


def redacting_log(log: IO[str] | None) -> IO[str] | None:
    """Return a non-owning writer that masks endpoint credentials."""

    return cast(IO[str], _RedactingLog(log)) if log is not None else None


def _render_command(command: Sequence[str] | str) -> str:
    if isinstance(command, str):
        return command
    return " ".join(str(part) for part in command)


def _display_command(command: Sequence[str] | str) -> str:
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


def _join_job(  # pragma: no cover - Windows compatibility
    job: WindowsJob, process: subprocess.Popen[str]
) -> WindowsJob | None:
    """Move a suspended command into its job and let it run.

    Returns None, with the job closed, when Windows refuses the move;
    ``taskkill /T`` then remains the way to stop the process tree.
    """

    try:
        joined = job.adopt(process.pid)
    except OSError as exc:
        process.kill()
        process.wait()
        job.close()
        raise MergetrainError(f"could not start command process {process.pid}: {exc}") from exc
    if not joined:
        job.close()
        return None
    return job


@contextmanager
def _job_handle_for_command(job: WindowsJob | None) -> Iterator[Any]:
    """Yield the ``startupinfo`` that gives a new process a handle to the named job.

    Windows forgets an object's name once the last handle to it closes, even
    while processes still run in it. The command and its keeper each hold one,
    so the job stays findable by name after a runner killed mid-push lost its
    handle.
    """

    handle = job.handle if job is not None else None
    if sys.platform != "win32" or handle is None:
        yield None
        return
    os.set_handle_inheritable(handle, True)  # pragma: no cover - Windows compatibility
    try:  # pragma: no cover - Windows compatibility
        yield subprocess.STARTUPINFO(lpAttributeList={"handle_list": [handle]})
    finally:  # pragma: no cover - Windows compatibility
        os.set_handle_inheritable(handle, False)


# Runs outside a named job and holds an inherited handle to it until no process
# of the job runs, so the job stays findable after its command and runner are
# gone: a local receive-pack can outlive a git push that dies on its own, and
# Git for Windows gives it no handle to the job.
_JOB_KEEPER = (
    "import sys\n"
    "from mergetrain.windows_job import hold_until_idle\n"
    "hold_until_idle(int(sys.argv[1]))\n"
)
_KEEPERS: list[subprocess.Popen[bytes]] = []


def _start_job_keeper(  # pragma: no cover - Windows compatibility
    job: WindowsJob,
) -> subprocess.Popen[bytes] | None:
    """Start the process that keeps a named job findable; None if it cannot start."""

    _KEEPERS[:] = [keeper for keeper in _KEEPERS if keeper.poll() is None]
    package_root = str(Path(__file__).resolve().parents[1])
    search_path = os.pathsep.join(
        path for path in (package_root, os.environ.get("PYTHONPATH", "")) if path
    )
    try:
        with _job_handle_for_command(job) as startupinfo:
            keeper = subprocess.Popen(
                [sys.executable, "-c", _JOB_KEEPER, str(job.handle)],
                cwd=package_root,
                env={**os.environ, "PYTHONPATH": search_path},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=startupinfo,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except OSError:
        return None
    _KEEPERS.append(keeper)
    return keeper


def _stop_windows_process_tree(
    process: subprocess.Popen[str], job: WindowsJob | None = None
) -> None:
    """Terminate a Windows child and every descendant it spawned.

    The command's job reaches descendants whose parent already exited;
    ``taskkill /T`` walks parent links and is the fallback without a job.
    """

    if job is not None and job.terminate():
        return
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


# How long a stopped command's process group gets to exit after SIGTERM, and
# then after SIGKILL, before the stop gives up waiting.
_STOP_GRACE_SECONDS = 5.0


def _group_alive(process: subprocess.Popen[str]) -> bool:
    """Whether any process is left in the command's POSIX process group."""

    process.poll()  # reap an exited leader, which would otherwise count as a member
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_group(process: subprocess.Popen[str], seconds: float) -> bool:
    """Wait until the process group is gone; return whether it is."""

    deadline = time.monotonic() + seconds
    while _group_alive(process):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _stop_posix_group(process: subprocess.Popen[str]) -> bool:
    """SIGTERM the command's process group, then SIGKILL whatever is left.

    A descendant that traps or ignores SIGTERM keeps running after the group
    leader exits, so escalation follows the group, not the leader (#228).
    """

    running = process.poll() is None
    if not running and not _group_alive(process):
        return False
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return False
    except PermissionError:
        # Only members this user may not signal are left, such as a gate's
        # sudo child; waiting cannot make them stoppable.
        process.wait()
        return running
    if not _wait_for_group(process, _STOP_GRACE_SECONDS):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        _wait_for_group(process, _STOP_GRACE_SECONDS)
    process.wait()
    return running


def _stop_windows_process(  # pragma: no cover - Windows compatibility
    process: subprocess.Popen[str], job: WindowsJob | None
) -> bool:
    if process.poll() is not None:
        return False
    stopped = False
    try:
        _stop_windows_process_tree(process, job)
        stopped = True
        process.wait(timeout=5)
    except ProcessLookupError:
        process.wait()
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            _stop_windows_process_tree(process, job)
            if process.poll() is None:
                process.kill()
            stopped = True
            process.wait()
    return stopped


def _stop_process(process: subprocess.Popen[str], job: WindowsJob | None = None) -> bool:
    """Stop a managed command and everything it started; report if it was running."""

    if os.name == "posix":
        return _stop_posix_group(process)
    return _stop_windows_process(process, job)  # pragma: no cover - Windows compatibility


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
    pass_fds: Sequence[int] = (),
    job_name: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run one non-interactive process while enforcing pulse, timeout, and cancel.

    ``pass_fds`` stay open in the process and everything it starts (POSIX);
    ``job_name`` names its Windows job so that other processes can find it for
    as long as any process of the job runs, or until the command succeeds.
    """

    if cancel_event is not None and cancel_event.is_set():
        raise CancellationRequested("command canceled before it started")
    if pulse is not None:
        pulse()
    job = WindowsJob.create(job_name) if os.name == "nt" else None
    keeper: subprocess.Popen[bytes] | None = None
    try:
        with _job_handle_for_command(job if job_name else None) as startupinfo:
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
                pass_fds=tuple(pass_fds) if os.name == "posix" else (),
                startupinfo=startupinfo,
                # A job adopts the process before it runs, so no descendant escapes.
                creationflags=(
                    (
                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                        | (CREATE_SUSPENDED if job is not None else 0)
                    )
                    if os.name == "nt"
                    else 0
                ),
            )
        if job is not None:  # pragma: no cover - Windows compatibility
            job = _join_job(job, process)
            if job is not None and job_name:
                keeper = _start_job_keeper(job)
    except BaseException:
        if job is not None:  # pragma: no cover - Windows compatibility
            job.close()
        raise
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
                if _stop_process(process, job):
                    canceled = True
                    stderr_tail.append("command canceled by gate scheduler\n")
                    break
                continue
            if timeout_seconds is not None and now - started >= timeout_seconds:
                if _stop_process(process, job):
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
        _stop_process(process, job)
        raise
    finally:
        join_deadline = time.monotonic() + 2.0
        for reader in readers:
            reader.join(timeout=max(0.0, join_deadline - time.monotonic()))
        if job is not None:  # pragma: no cover - Windows compatibility
            # A command that succeeded has done its work, so what it left
            # running must not keep it findable. Any other exit keeps the
            # keeper until the job is empty: Windows reports a killed process
            # with an ordinary exit status.
            if keeper is not None and process.returncode == 0:
                keeper.kill()
                keeper.wait()
            job.close()

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
    pass_fds: Sequence[int] = (),
    job_name: str = "",
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
        pass_fds=pass_fds,
        job_name=job_name,
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
    """Expand documented placeholders, escaping paths for their shell context.

    Raises ``ConfigError`` rather than guess when a path that needs quoting
    lands where the shell's quoting cannot be proven.
    """

    expanded = command
    replacements = {
        "${integration_ref}": config.git.integration_ref,
        "${project}": config.project.name,
    }
    for key, value in replacements.items():
        expanded = expanded.replace(key, value)
    return expand_path_placeholders(
        expanded,
        {"${repo}": str(config.repo), "${worktree}": str(worktree)},
    )


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
