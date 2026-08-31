"""Resource-bounded gate scheduling, path selection, and verify execution."""

from __future__ import annotations

import io
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .command_runner import (
    Pulse,
    _dashboard_command,
    command_env,
    expand_command,
    run_command,
    run_shell,
)
from .config import GateConfig, MergetrainConfig, effective_gates
from .errors import CancellationRequested, CommandFailed, MergetrainError
from .path_gates import any_path_matches, parse_name_status_z
from .reuse import environment_sha

GateProgress = Callable[[str, str, int, int, str], None]


@dataclass(slots=True)
class _GateOutcome:
    output: str
    error: BaseException | None = None


def gate_dependencies(gates: Sequence[GateConfig]) -> dict[str, tuple[str, ...]]:
    """Resolve omitted dependencies into deterministic sequential stages."""

    stages: list[list[GateConfig]] = []
    for gate in gates:
        if gate.parallel_group and stages and stages[-1][0].parallel_group == gate.parallel_group:
            stages[-1].append(gate)
        else:
            stages.append([gate])

    dependencies: dict[str, tuple[str, ...]] = {}
    prior_names: tuple[str, ...] = ("diff-check",)
    for stage in stages:
        for gate in stage:
            dependencies[gate.name] = gate.needs or prior_names
        prior_names = tuple(gate.name for gate in stage)
    return dependencies


class GateRunner:
    """Execute configured gates while keeping ordering and resource policy explicit."""

    def __init__(self, config: MergetrainConfig):
        self.config = config
        self.gates = effective_gates(config)

    def run_gate(
        self,
        gate: GateConfig,
        *,
        worktree: Path,
        log: IO[str],
        pulse: Pulse | None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        command = expand_command(gate.run, config=self.config, worktree=worktree)
        env = command_env(config=self.config, worktree=worktree)
        log.write(f"\n## gate: {gate.name}\n")
        run_shell(
            command,
            cwd=worktree,
            env=env,
            log=log,
            check=True,
            pulse=pulse,
            pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
            timeout_seconds=(
                gate.timeout_seconds
                if gate.timeout_seconds is not None
                else self.config.queue.command_timeout_seconds
            ),
            cancel_event=cancel_event,
        )

    def run_configured_plan(
        self,
        *,
        worktree: Path,
        log: IO[str],
        pulse: Pulse | None,
        on_gate: GateProgress | None,
        initial_states: dict[str, tuple[str, str]],
    ) -> None:
        """Run configured gates in deterministic, resource-bounded waves."""

        gates = self.gates
        if not gates:
            return
        total = 1 + len(gates)
        indexes = {gate.name: index for index, gate in enumerate(gates, start=2)}
        dependencies = gate_dependencies(gates)
        completed = {"diff-check"}
        pending = [gate for gate in gates if gate.name not in initial_states]

        for gate in gates:
            initial_state = initial_states.get(gate.name)
            if initial_state is None:
                continue
            event_state, detail = initial_state
            if event_state == "skipped":
                log.write(f"\n## gate skipped: {gate.name} ({detail})\n")
            if on_gate:
                on_gate(gate.name, event_state, indexes[gate.name], total, detail)
            completed.add(gate.name)

        plan_started = time.monotonic()
        plan_timeout = self.config.gate_parallelism.timeout_seconds
        pulse_interval = max(0.1, float(self.config.queue.heartbeat_interval_seconds))
        next_pulse = time.monotonic() + pulse_interval

        while pending:
            ready = [gate for gate in pending if set(dependencies[gate.name]).issubset(completed)]
            if not ready:
                unresolved = ", ".join(gate.name for gate in pending)
                raise MergetrainError(
                    f"configured gate dependencies cannot make progress: {unresolved}"
                )

            first = ready[0]
            candidates = (
                [gate for gate in ready if gate.parallel_group == first.parallel_group]
                if first.parallel_group
                else [first]
            )
            selected: list[GateConfig] = []
            used_workers = 0
            for gate in candidates:
                if used_workers + gate.workers > self.config.gate_parallelism.max_workers:
                    continue
                selected.append(gate)
                used_workers += gate.workers
            if not selected:
                raise MergetrainError(f"gate {first.name!r} exceeds the configured worker ceiling")

            for gate in selected:
                if on_gate:
                    on_gate(
                        gate.name,
                        "active",
                        indexes[gate.name],
                        total,
                        _dashboard_command(gate.run),
                    )

            cancel_event = threading.Event()

            def execute(
                gate: GateConfig,
                *,
                batch_cancel: threading.Event = cancel_event,
            ) -> _GateOutcome:
                gate_log = io.StringIO()
                try:
                    self.run_gate(
                        gate,
                        worktree=worktree,
                        log=gate_log,
                        pulse=None,
                        cancel_event=batch_cancel,
                    )
                except BaseException as exc:
                    if not isinstance(exc, CancellationRequested):
                        batch_cancel.set()
                    return _GateOutcome(gate_log.getvalue(), exc)
                return _GateOutcome(gate_log.getvalue())

            futures: dict[str, Future[_GateOutcome]] = {}
            monitor_error: BaseException | None = None
            with ThreadPoolExecutor(
                max_workers=len(selected),
                thread_name_prefix="mergetrain-gate",
            ) as executor:
                for gate in selected:
                    futures[gate.name] = executor.submit(execute, gate)
                unfinished = set(futures.values())
                while unfinished:
                    _, unfinished = wait(unfinished, timeout=0.1)
                    now = time.monotonic()
                    if (
                        plan_timeout is not None
                        and now - plan_started >= plan_timeout
                        and monitor_error is None
                    ):
                        monitor_error = CommandFailed(
                            "configured gate plan",
                            124,
                            stderr=(f"gate plan timed out after {plan_timeout:g} seconds"),
                            cwd=str(worktree),
                        )
                        cancel_event.set()
                    if pulse is not None and now >= next_pulse:
                        try:
                            pulse()
                        except BaseException as exc:
                            if monitor_error is None:
                                monitor_error = exc
                                cancel_event.set()
                        next_pulse = now + pulse_interval

            outcomes = {gate.name: futures[gate.name].result() for gate in selected}
            for gate in selected:
                output = outcomes[gate.name].output
                if output:
                    log.write(output)
            log.flush()

            for gate in selected:
                error = outcomes[gate.name].error
                if error is None and monitor_error is None:
                    terminal_state = "success"
                elif error is None or isinstance(error, CancellationRequested):
                    terminal_state = "canceled"
                else:
                    terminal_state = "failure"
                if on_gate:
                    failure_detail = "canceled"
                    if isinstance(error, CommandFailed):
                        failure_detail = f"exit_code={error.returncode}"
                    elif error is not None:
                        failure_detail = type(error).__name__
                    elif monitor_error is not None:
                        failure_detail = type(monitor_error).__name__
                    on_gate(
                        gate.name,
                        terminal_state,
                        indexes[gate.name],
                        total,
                        (
                            _dashboard_command(gate.run)
                            if terminal_state == "success"
                            else failure_detail
                        ),
                    )

            if monitor_error is not None:
                raise monitor_error
            failure = next(
                (
                    outcome.error
                    for gate in selected
                    if (outcome := outcomes[gate.name]).error is not None
                    and not isinstance(outcome.error, CancellationRequested)
                ),
                None,
            )
            if failure is not None:
                raise failure
            cancellation = next(
                (
                    outcome.error
                    for gate in selected
                    if isinstance(
                        (outcome := outcomes[gate.name]).error,
                        CancellationRequested,
                    )
                ),
                None,
            )
            if cancellation is not None:
                raise cancellation

            for gate in selected:
                completed.add(gate.name)
                pending.remove(gate)

    def changed_paths(
        self,
        *,
        worktree: Path,
        base_ref: str,
        head_ref: str,
        log: IO[str],
        pulse: Pulse | None,
    ) -> tuple[str, ...] | None:
        """Return exact changed paths, or ``None`` to make every gate run."""

        if not base_ref or not head_ref:
            log.write("\npath-aware gate selection unavailable; running all gates\n")
            return None
        try:
            completed = run_command(
                [
                    "git",
                    "diff",
                    "--name-status",
                    "-z",
                    "--find-renames",
                    f"{base_ref}..{head_ref}",
                    "--",
                ],
                cwd=worktree,
                log=None,
                pulse=pulse,
                pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                timeout_seconds=self.config.queue.command_timeout_seconds,
            )
            return parse_name_status_z(completed.stdout)
        except (CommandFailed, ValueError):
            log.write("\npath-aware gate selection failed; running all gates\n")
            return None

    def run_gates(
        self,
        *,
        worktree: Path,
        log: IO[str],
        pulse: Pulse | None,
        on_gate: GateProgress | None = None,
        base_ref: str = "",
        head_ref: str = "HEAD",
    ) -> None:
        total = 1 + len(self.gates)
        diff_command = [
            "git",
            "diff",
            "--check",
            f"{self.config.git.integration_ref}..HEAD",
        ]
        if on_gate:
            on_gate("diff-check", "active", 1, total, _dashboard_command(diff_command))
        run_command(
            diff_command,
            cwd=worktree,
            log=log,
            pulse=pulse,
            pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
            timeout_seconds=self.config.queue.command_timeout_seconds,
        )
        if on_gate:
            on_gate("diff-check", "success", 1, total, _dashboard_command(diff_command))
        changed_paths = None
        if any(gate.paths for gate in self.gates):
            changed_paths = self.changed_paths(
                worktree=worktree,
                base_ref=base_ref,
                head_ref=head_ref,
                log=log,
                pulse=pulse,
            )
        initial_states: dict[str, tuple[str, str]] = {}
        for gate in self.gates:
            if (
                gate.paths
                and changed_paths is not None
                and not any_path_matches(gate.paths, changed_paths)
            ):
                initial_states[gate.name] = (
                    "skipped",
                    "no changed paths matched configured paths",
                )
        self.run_configured_plan(
            worktree=worktree,
            log=log,
            pulse=pulse,
            on_gate=on_gate,
            initial_states=initial_states,
        )

    def run_verify_hooks(self, *, worktree: Path, log: IO[str], pulse: Pulse | None) -> None:
        for hook in self.config.deploy.verify:
            command = expand_command(hook.run, config=self.config, worktree=worktree)
            env = command_env(config=self.config, worktree=worktree)
            log.write(f"\n## verify: {hook.name}\n")
            run_shell(
                command,
                cwd=worktree,
                env=env,
                log=log,
                check=True,
                pulse=pulse,
                pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                timeout_seconds=self.config.queue.command_timeout_seconds,
            )

    def environment_fingerprint(
        self,
        *,
        worktree: Path,
        log: IO[str],
        pulse: Pulse | None,
    ) -> str:
        values: list[tuple[str, str]] = []
        for fingerprint in self.config.deploy.reuse.fingerprints:
            command = expand_command(fingerprint.run, config=self.config, worktree=worktree)
            log.write(f"\n## reuse fingerprint: {fingerprint.name} (opaque output hashed)\n")
            completed = run_shell(
                command,
                cwd=worktree,
                env=command_env(config=self.config, worktree=worktree),
                log=None,
                check=True,
                pulse=pulse,
                pulse_interval_seconds=self.config.queue.heartbeat_interval_seconds,
                timeout_seconds=self.config.queue.command_timeout_seconds,
            )
            value = completed.stdout.strip()
            if not value or "\n" in value or len(value) > 512:
                raise MergetrainError(
                    f"reuse fingerprint {fingerprint.name!r} must emit one "
                    "non-empty line of at most 512 characters"
                )
            values.append((fingerprint.name, value))
        return environment_sha(values)

    def run_reused_gates(
        self,
        *,
        worktree: Path,
        validation_sha: str,
        base_ref: str,
        log: IO[str],
        pulse: Pulse | None,
        on_gate: GateProgress | None = None,
    ) -> None:
        total = 1 + len(self.gates)
        if on_gate:
            on_gate("diff-check", "reused", 1, total, validation_sha)
        changed_paths = None
        if any(gate.paths for gate in self.gates):
            changed_paths = self.changed_paths(
                worktree=worktree,
                base_ref=base_ref,
                head_ref=validation_sha,
                log=log,
                pulse=pulse,
            )
        initial_states: dict[str, tuple[str, str]] = {}
        for gate in self.gates:
            if (
                gate.paths
                and changed_paths is not None
                and not any_path_matches(gate.paths, changed_paths)
            ):
                initial_states[gate.name] = (
                    "skipped",
                    "no changed paths matched configured paths",
                )
                continue
            if not gate.always_rerun_on_deploy and not (gate.paths and changed_paths is None):
                initial_states[gate.name] = ("reused", validation_sha)
        self.run_configured_plan(
            worktree=worktree,
            log=log,
            pulse=pulse,
            on_gate=on_gate,
            initial_states=initial_states,
        )
