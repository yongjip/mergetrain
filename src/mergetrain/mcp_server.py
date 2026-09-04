"""Model Context Protocol server for mergetrain (optional ``mcp`` extra).

Every tool is a veneer over the CLI's ``--json`` output: the server shells out
to ``mergetrain`` and returns that payload verbatim, so ``contract_version``
stays the one machine interface (docs/contract.md) and there is no second
contract to keep in step.

The exposed surface is deliberately smaller than the CLI. ``daemon``,
``enqueue --auto``, ``gc --apply``, ``gc --delete-branches``, ``cancel``,
``unlock``, ``dismiss`` and the recovery mutations are absent, and no tool takes
a parameter that could reach them, so an agent connected through this server
cannot start an unattended deploy or a destructive cleanup whatever a prompt
tells it. ``mergetrain_deploy`` additionally requires a human accept that the
model cannot fabricate: confirm-then-deploy is a mechanism here, not a rule
written in prose that an agent may or may not follow.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from .errors import redact_secrets

try:
    # A real name in module globals, because MCPServer resolves the deploy tool's
    # annotations to decide what to inject. The fallback keeps the module
    # importable without the extra so the tool logic stays testable.
    from mcp.server.mcpserver import Context
except ImportError:  # pragma: no cover - depends on whether the extra is present
    Context = Any  # type: ignore[assignment, misc]

try:
    from pydantic import BaseModel, Field

    class _DeployConfirmation(BaseModel):  # type: ignore[no-redef]
        """The one thing a human has to check for a deploy to proceed."""

        confirm: bool = Field(default=False, description="Deploy this validated train now")

except ImportError:  # pragma: no cover - pydantic ships with the mcp extra
    _DeployConfirmation = None  # type: ignore[assignment, misc]

INSTALL_HINT = (
    "the MCP server needs the optional 'mcp' extra: "
    "uv tool install 'mergetrain[mcp]' (or pip install 'mergetrain[mcp]')"
)

# Long enough for a validate that runs a real gate suite, bounded so a wedged
# child cannot hold the server's event loop forever.
_CLI_TIMEOUT_SECONDS = 3600
_CLI_TERMINATE_GRACE_SECONDS = 5.0
_LOG_TAIL_MAX_LINES = 200


@dataclass(frozen=True, slots=True)
class _DeployPlan:
    """One current deploy decision, resolved before any confirmation is shown."""

    refusal: dict[str, Any] | None = None
    train_id: str = ""
    plan_sha: str = ""
    summary: str = ""
    command: str = ""
    can_elicit: bool = False


@dataclass(frozen=True, slots=True)
class _ConfirmationSkipped:
    """Resolver value used when there is no question to send to the client."""

    reason: str


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """Mirror the CLI's one failure envelope so consumers parse a single shape."""

    payload: dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code,
            "message": redact_secrets(message),
            "retryable": False,
        },
    }
    payload.update(extra)
    return payload


def _replace_local_path_root(text: str, root: str, replacement: str) -> str:
    """Shorten a filesystem path root without rewriting matching URL paths."""

    if not root:
        return text
    # Contract-external diagnostics can mix path styles: for example a Python
    # traceback may contain a POSIX-looking configured path even when the MCP
    # server itself is running on Windows. Match both separator spellings while
    # retaining the leading-boundary check that keeps URL path segments intact.
    variants = {root, root.replace("\\", "/"), root.replace("/", "\\")}
    flags = re.MULTILINE | (re.IGNORECASE if os.name == "nt" else 0)
    for variant in sorted(variants, key=len, reverse=True):
        pattern = re.compile(
            rf"(^|[\s'\"(=]){re.escape(variant)}(?=[\\/]|$|[\s'\"),:])",
            flags,
        )
        text = pattern.sub(lambda match: f"{match.group(1)}{replacement}", text)
    return text


async def _stop_windows_process_tree(process: asyncio.subprocess.Process) -> None:
    """Terminate a Windows child and its descendants without blocking the loop."""

    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(killer.wait(), timeout=10)
        except asyncio.TimeoutError:  # pragma: no cover - defensive Windows fallback
            killer.kill()
            await killer.wait()
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - Windows only
        pass
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()


async def _stop_cli_process(process: asyncio.subprocess.Process) -> bool:
    """Stop the CLI process group, escalating when graceful shutdown wedges."""

    if process.returncode is not None:
        return False
    stopped = False
    try:
        if os.name == "posix":
            # SIGINT lets the Python CLI unwind its own managed command
            # runners, which may lead separate process groups for gates/Git.
            # Their BaseException cleanup stops those descendants before the
            # outer CLI releases its runner lock.
            os.killpg(process.pid, signal.SIGINT)
        else:  # pragma: no cover - exercised by Windows CI
            ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
            if ctrl_break is None:
                await _stop_windows_process_tree(process)
            else:
                try:
                    process.send_signal(ctrl_break)
                except OSError:
                    # GUI/stdio hosts may not own a console that can receive a
                    # CTRL_BREAK event. taskkill remains the tree-safe fallback.
                    await _stop_windows_process_tree(process)
        stopped = True
        await asyncio.wait_for(process.wait(), timeout=_CLI_TERMINATE_GRACE_SECONDS)
    except ProcessLookupError:
        await process.wait()
    except asyncio.TimeoutError:
        if process.returncode is None:
            if os.name == "posix":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - exercised by Windows CI
                await _stop_windows_process_tree(process)
                if process.returncode is None:
                    process.kill()
            stopped = True
            await process.wait()
    return stopped


async def _stop_and_drain(
    process: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
) -> tuple[bytes, bytes]:
    """Complete process-tree cleanup even when the caller is being cancelled."""

    await _stop_cli_process(process)
    with suppress(BaseException):
        return await communicate_task
    return b"", b""


@dataclass(slots=True)
class MergetrainTools:
    """The tool implementations, bound to one repository.

    Kept separate from server registration so the safety-relevant paths -- the
    deploy gate above all -- are directly testable without a live MCP client.
    """

    repo: Path

    def _argv(self, args: list[str]) -> list[str]:
        # sys.executable -m keeps the child on this interpreter even when the
        # server was started from a venv that is not first on PATH.
        return [sys.executable, "-m", "mergetrain", "--repo", str(self.repo), *args]

    async def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        # Use an async child handle rather than a worker-thread subprocess.run:
        # MCP request cancellation and server shutdown must terminate the CLI
        # process tree instead of returning while validate/deploy keeps running.
        argv = self._argv(args)
        popen_options: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name == "posix":
            popen_options["start_new_session"] = True
        else:  # pragma: no cover - exercised by Windows CI
            popen_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = await asyncio.create_subprocess_exec(*argv, **popen_options)
        communicate_task = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communicate_task), timeout=_CLI_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            await _stop_and_drain(process, communicate_task)
            raise subprocess.TimeoutExpired(cmd=argv, timeout=_CLI_TIMEOUT_SECONDS) from exc
        except BaseException:
            # Shield cleanup so the cancellation already delivered to this task
            # cannot strand the child. Re-raise the original CancelledError (or
            # transport failure) so the MCP SDK keeps its existing semantics.
            cleanup = asyncio.create_task(_stop_and_drain(process, communicate_task))
            await asyncio.shield(cleanup)
            raise
        return subprocess.CompletedProcess(
            argv,
            process.returncode if process.returncode is not None else 1,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    def _safe_detail(self, *candidates: str) -> str:
        """Bound and mask contract-external CLI diagnostics for MCP output."""

        raw = next((item for item in candidates if item), "").strip()
        detail = redact_secrets(raw)
        repo = str(self.repo)
        detail = _replace_local_path_root(detail, repo, "[repo]")
        home = str(Path.home())
        detail = _replace_local_path_root(detail, home, "~")
        return detail[:2000]

    async def _json(self, args: list[str]) -> dict[str, Any]:
        """Run a --json command and return its payload untouched.

        A failing command still emits the contract's failure envelope, so a
        non-zero exit is not an error here -- the payload is the answer. Only
        output that is not a JSON object needs a synthesized envelope.
        """

        try:
            completed = await self._run(args)
        except subprocess.TimeoutExpired:
            return _error(
                "cli_timeout",
                f"'mergetrain {' '.join(args)}' exceeded {_CLI_TIMEOUT_SECONDS}s",
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            detail = self._safe_detail(completed.stderr, completed.stdout)
            return _error(
                "cli_output_unreadable",
                f"'mergetrain {' '.join(args)}' did not return JSON: {detail}",
                exit_code=completed.returncode,
            )
        if not isinstance(payload, dict):
            return _error(
                "cli_output_unreadable",
                f"'mergetrain {' '.join(args)}' returned a non-object payload",
                exit_code=completed.returncode,
            )
        return payload

    # --- read-only -------------------------------------------------------

    async def status(self, limit: int = 20) -> dict[str, Any]:
        """Current queue state, action-required work, and the exact next action."""

        return await self._json(["status", "--json", "--limit", str(max(1, min(limit, 200)))])

    async def _inspect_job(self, job_id: int) -> dict[str, Any]:
        """One job with its latest run, failure category, and train outcome."""

        return await self._json(["inspect", str(job_id), "--json"])

    async def _events(self, limit: int = 50, job_id: int = 0, after: int = 0) -> dict[str, Any]:
        """Recent runner event frames, bounded and never following.

        Returns the CLI's own JSONL frames under ``frames``; the frames
        themselves are unchanged, including their ``stream_start`` header.
        """

        args = ["events", "--jsonl", "--limit", str(max(1, min(limit, 200)))]
        if job_id:
            args += ["--job", str(job_id)]
        if after:
            args += ["--after", str(after)]
        try:
            completed = await self._run(args)
        except subprocess.TimeoutExpired:
            return _error("cli_timeout", f"'mergetrain {' '.join(args)}' timed out")
        frames: list[dict[str, Any]] = []
        for line in completed.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict):
                frames.append(frame)
        if not frames and completed.returncode != 0:
            detail = self._safe_detail(completed.stderr)
            return _error("cli_output_unreadable", f"events failed: {detail}")
        return {"frames": frames}

    async def _logs(self, job_id: int, tail: int = 200) -> dict[str, Any]:
        """A capped tail of one job's runner log. Never follows.

        The log is plain text, so this returns text rather than a queue payload;
        read it only when ``inspect_job``'s note and category are not enough.
        """

        lines = max(1, min(tail, _LOG_TAIL_MAX_LINES))
        args = ["logs", str(job_id), "--tail", str(lines)]
        try:
            completed = await self._run(args)
        except subprocess.TimeoutExpired:
            return _error("cli_timeout", f"'mergetrain {' '.join(args)}' timed out")
        if completed.returncode != 0:
            detail = self._safe_detail(completed.stderr, completed.stdout)
            return _error(
                "log_unavailable",
                f"could not read the log for job {job_id}: {detail}",
                exit_code=completed.returncode,
            )
        return {"job_id": job_id, "tail_lines": lines, "log": completed.stdout}

    async def inspect(
        self,
        job_id: int,
        detail: Literal["summary", "events", "logs"] = "summary",
        limit: int = 100,
        after_event_id: int = 0,
    ) -> dict[str, Any]:
        """Inspect one job; request bounded events or raw log text explicitly."""

        if detail == "summary":
            return await self._inspect_job(job_id)
        if detail == "events":
            return await self._events(
                limit=limit,
                job_id=job_id,
                after=after_event_id,
            )
        if detail == "logs":
            return await self._logs(job_id, tail=limit)
        return _error(
            "invalid_inspect_detail",
            "detail must be one of: summary, events, logs",
        )

    # --- mutating, but never shipping ------------------------------------

    async def validate(self) -> dict[str, Any]:
        """Validate the queued train without pushing anything.

        Free to run per the operating contract, but not read-only: it creates a
        worktree, runs the configured gate commands, and moves jobs to
        ``validated`` or ``blocked``. The annotations say so rather than
        claiming otherwise to the client.
        """

        return await self._json(["validate", "--json"])

    async def enqueue(self, task: str, branch: str) -> dict[str, Any]:
        """Queue a committed branch, pinning the SHA that was reviewed.

        The CLI always captures the exact clean commit, so later work on the
        branch cannot ride along silently. ``--auto`` is not a parameter --
        unattended deploy is not reachable from here.
        """

        return await self._json(["enqueue", "--task", task, "--branch", branch, "--json"])

    # --- ships code, human-gated -----------------------------------------

    async def prepare_deploy(self, ctx: Context) -> _DeployPlan:
        """Validate if needed and resolve the exact plan shown to the human.

        There is no model-supplied confirmation or train selector. The CLI owns
        the single-Ready invariant, and the accept comes only through MCP
        elicitation.
        """

        preview = await self._json(["deploy", "--json"])
        if preview.get("ok") is False:
            return _DeployPlan(refusal=preview)
        plan_sha = str(preview.get("deploy_plan_sha") or "")
        if preview.get("result") != "confirmation_required" or not plan_sha:
            note = str(preview.get("note") or "no deployable work is ready")
            return _DeployPlan(
                refusal=_error(
                    "deploy_plan_unavailable",
                    f"{note}; nothing was pushed",
                )
            )
        jobs = preview.get("jobs") or []
        push_plan = preview.get("push_plan") or {}
        tasks = ", ".join(
            " ".join(str(job.get("task") or job.get("branch") or "task").split()) for job in jobs
        )
        refs = ", ".join(
            str(item.get("target")) for item in push_plan.get("refs") or [] if item.get("target")
        )
        destination = push_plan.get("url") or push_plan.get("remote") or "unknown"
        reuse = preview.get("reuse") or {}
        decision = reuse.get("decision") or {}
        gate_action = decision.get("action") or "run configured gates"
        warning_lines = [
            f"Warning: {warning.get('summary')}"
            for warning in preview.get("warnings") or []
            if warning.get("summary")
        ]
        summary = "\n".join(
            [
                f"Changes: {tasks or 'task details unavailable'}",
                f"Destination: {destination} ({refs or 'configured refs'})",
                f"Gate plan: {gate_action}",
                *warning_lines,
                "Safety: the exact train, destination, gate/reuse policy, and "
                "verify hooks will be checked again before push",
            ]
        )
        train_id = str(jobs[0].get("train_id") or "") if jobs else ""
        command = shlex.join(["mergetrain", "--repo", str(self.repo), "deploy"])
        return _DeployPlan(
            train_id=train_id,
            plan_sha=plan_sha,
            summary=summary,
            command=command,
            can_elicit=_client_can_elicit(ctx),
        )

    async def deploy(self, plan: _DeployPlan, approval: Any) -> dict[str, Any]:
        """Deploy a prepared train only after the resolver returns explicit consent."""

        if plan.refusal is not None:
            return plan.refusal

        if not plan.can_elicit:
            return _error(
                "confirmation_required",
                "this client cannot show a confirmation dialog, so the deploy "
                "was not started; run it in a terminal after reviewing the "
                f"summary:\n{plan.summary}\n\n{plan.command}",
                train_id=plan.train_id,
                command=plan.command,
            )
        accepted, reason = _deploy_approval(approval)
        if not accepted:
            return _error(
                "deploy_not_confirmed",
                f"the deploy was not confirmed ({reason}); nothing was pushed",
                train_id=plan.train_id,
                command=plan.command,
            )
        return await self._json(
            [
                "deploy",
                "--expected-plan",
                plan.plan_sha,
                "--json",
            ]
        )


def _client_can_elicit(ctx: Any) -> bool:
    """Report whether the client declared elicitation support at initialize.

    Reads the declared capability directly, which is what
    ``ServerSession.check_client_capability`` does for this capability, and
    keeps the refusal path free of SDK imports so it stays testable. Anything
    unreadable counts as unsupported: refusing costs the human one terminal
    command, while assuming a dialog that never renders would ship code nobody
    saw.
    """

    try:
        capabilities = ctx.client_capabilities
    except Exception:
        return False
    elicitation = getattr(capabilities, "elicitation", None)
    if elicitation is None:
        return False
    # Before elicitation modes existed, an empty capability meant form support.
    # In v2, form support is explicit and url-only clients cannot render this gate.
    return (
        getattr(elicitation, "form", None) is not None or getattr(elicitation, "url", None) is None
    )


def _deploy_approval(result: Any) -> tuple[bool, str]:
    """Require an accepted resolver outcome whose checkbox is explicitly true."""

    action = getattr(result, "action", "")
    if action != "accept":
        outcome = {"decline": "declined", "cancel": "cancelled"}.get(
            action, action or "did not respond"
        )
        return False, f"the human {outcome}"
    data = getattr(result, "data", None)
    if isinstance(data, _ConfirmationSkipped):
        return False, data.reason
    if data is None or not getattr(data, "confirm", False):
        return False, "the confirmation was left unchecked"
    return True, "accepted"


def build_server(repo: Path) -> Any:
    """Register the tool surface, annotated with what each tool actually does."""

    from mcp.server import MCPServer
    from mcp.server.elicitation import ElicitationResult
    from mcp.server.mcpserver import Elicit, Resolve
    from mcp.types import ToolAnnotations

    tools = MergetrainTools(repo=repo)
    server = MCPServer(
        name="mergetrain",
        instructions=(
            "mergetrain is a local merge-and-push queue for coding-agent "
            "branches. Work in the assigned worktree, commit a clean HEAD, read "
            "mergetrain_status before changing queue state, and enqueue with task "
            "and branch only. Stop after enqueue unless end-to-end deployment was "
            "explicitly authorized. Never push integration refs directly. "
            "mergetrain_deploy ships code and requires a client-rendered human "
            "accept; unattended deploy, recovery, cancellation, and destructive "
            "cleanup are not available through this server."
        ),
    )

    async def resolve_deploy_plan(ctx: Context) -> _DeployPlan:
        return await tools.prepare_deploy(ctx)

    async def resolve_deploy_confirmation(plan: _DeployPlan) -> Any:
        if plan.refusal is not None:
            return _ConfirmationSkipped("the deploy request was refused before confirmation")
        if not plan.can_elicit:
            return _ConfirmationSkipped("the client cannot show a confirmation dialog")
        assert _DeployConfirmation is not None
        message = (
            "mergetrain will atomically push the change set below. "
            "This ships code.\n\n" + plan.summary
        )
        return Elicit(message=message, schema=_DeployConfirmation)

    # Resolver parameters are deliberately absent from the tool schema. Setting
    # concrete annotations here keeps the optional SDK import out of module load
    # while still giving MCPServer the complete dependency graph at registration.
    plan_dependency = Annotated[_DeployPlan, Resolve(resolve_deploy_plan)]
    resolve_deploy_confirmation.__annotations__["plan"] = plan_dependency
    resolve_deploy_confirmation.__annotations__["return"] = (
        Elicit[_DeployConfirmation] | _ConfirmationSkipped
    )

    async def deploy_tool(plan: Any, approval: Any) -> dict[str, Any]:
        """Deploy one exact validated plan after client-rendered human confirmation."""

        return await tools.deploy(plan, approval)

    deploy_tool.__annotations__["plan"] = plan_dependency
    deploy_tool.__annotations__["approval"] = Annotated[
        ElicitationResult[Any], Resolve(resolve_deploy_confirmation)
    ]
    deploy_tool.__annotations__["return"] = dict[str, Any]

    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False)
    for fn, name, hints in (
        (tools.status, "mergetrain_status", read_only),
        (tools.inspect, "mergetrain_inspect", read_only),
        # Not read-only: validate runs gate commands and moves job status.
        (
            tools.validate,
            "mergetrain_validate",
            ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
            ),
        ),
        (
            tools.enqueue,
            "mergetrain_enqueue",
            ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
            ),
        ),
        (
            deploy_tool,
            "mergetrain_deploy",
            ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=False,
            ),
        ),
    ):
        server.add_tool(fn, name=name, annotations=hints)

    return server


def run_server(repo: Path) -> int:
    """Serve over stdio, or explain how to install the extra."""

    try:
        server = build_server(repo)
    except ImportError:
        print(f"mergetrain mcp: {INSTALL_HINT}", file=sys.stderr)
        return 1
    server.run(transport="stdio")
    return 0
