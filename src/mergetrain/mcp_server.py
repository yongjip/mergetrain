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
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import redact_secrets

try:
    # A real name in module globals, because FastMCP resolves the deploy tool's
    # annotations to decide what to inject. The fallback keeps the module
    # importable without the extra so the tool logic stays testable.
    from mcp.server.fastmcp import Context
except ImportError:  # pragma: no cover - depends on whether the extra is present
    Context = Any  # type: ignore[assignment, misc]

try:
    from pydantic import BaseModel, Field

    class _DeployConfirmation(BaseModel):  # type: ignore[no-redef]
        """The one thing a human has to check for a deploy to proceed."""

        confirm: bool = Field(
            default=False, description="Deploy this validated train now"
        )

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
_LOG_TAIL_MAX_LINES = 500


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
        await asyncio.wait_for(
            process.wait(), timeout=_CLI_TERMINATE_GRACE_SECONDS
        )
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
            popen_options["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        process = await asyncio.create_subprocess_exec(*argv, **popen_options)
        communicate_task = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communicate_task), timeout=_CLI_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            await _stop_and_drain(process, communicate_task)
            raise subprocess.TimeoutExpired(
                cmd=argv, timeout=_CLI_TIMEOUT_SECONDS
            ) from exc
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
        """Queue, runner lock, validated trains, and the advisory next action."""

        return await self._json(["status", "--json", "--limit", str(max(1, min(limit, 200)))])

    async def doctor(self) -> dict[str, Any]:
        """Repository health, effective config, and the advisory next action."""

        return await self._json(["doctor", "--json"])

    async def inspect_job(self, job_id: int) -> dict[str, Any]:
        """One job with its latest run, failure category, and train outcome."""

        return await self._json(["inspect", str(job_id), "--json"])

    async def history(self, limit: int = 50, since: str = "") -> dict[str, Any]:
        """Retained train and job history with gate outcomes."""

        args = ["history", "--json", "--limit", str(max(1, min(limit, 500)))]
        if since:
            args += ["--since", since]
        return await self._json(args)

    async def stats(self, since: str = "") -> dict[str, Any]:
        """Land rate, latency, queue time, and per-gate timing."""

        args = ["stats", "--json"]
        if since:
            args += ["--since", since]
        return await self._json(args)

    async def agent_contract(self) -> dict[str, Any]:
        """The operating contract: free commands, gated ones, and next actions."""

        return await self._json(["agent-contract", "--json"])

    async def gc_preview(self) -> dict[str, Any]:
        """Preview cleanup candidates. Dry run only -- apply is not reachable.

        ``--apply`` and ``--delete-branches`` are not parameters of this tool by
        design, so nothing an agent sends can turn the preview into a deletion.
        """

        return await self._json(["gc", "--json"])

    async def events(self, limit: int = 50, job_id: int = 0) -> dict[str, Any]:
        """Recent runner event frames, bounded and never following.

        Returns the CLI's own JSONL frames under ``frames``; the frames
        themselves are unchanged, including their ``stream_start`` header.
        """

        args = ["events", "--jsonl", "--limit", str(max(1, min(limit, 200)))]
        if job_id:
            args += ["--job", str(job_id)]
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

    async def logs(self, job_id: int, tail: int = 200) -> dict[str, Any]:
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

    # --- mutating, but never shipping ------------------------------------

    async def validate(self) -> dict[str, Any]:
        """Validate the queued train without pushing anything.

        Free to run per the operating contract, but not read-only: it creates a
        worktree, runs the configured gate commands, and moves jobs to
        ``validated`` or ``blocked``. The annotations say so rather than
        claiming otherwise to the client.
        """

        return await self._json(["run-batch", "--validate-only", "--json"])

    async def enqueue(self, task: str, branch: str) -> dict[str, Any]:
        """Queue a committed branch, pinning the SHA that was reviewed.

        Always passes ``--capture-sha``: a queued job records the exact commit,
        so later work on the branch cannot ride along silently. ``--auto`` is
        not a parameter -- unattended deploy is not reachable from here.
        """

        return await self._json(
            ["enqueue", "--task", task, "--branch", branch, "--capture-sha", "--json"]
        )

    # --- ships code, human-gated -----------------------------------------

    def deploy_summary(
        self,
        doctor: dict[str, Any],
        status: dict[str, Any],
        train: dict[str, Any],
    ) -> str:
        """Build the deploy summary the operating contract requires.

        Describe only the train selected for this deploy. Exact train identity
        remains internal while the human sees task intent, destinations, gate
        evidence, reassembly risk, and every action-required job.
        """

        config = doctor.get("config") or {}
        git = config.get("git") or {}
        remote = git.get("remote") or "origin"
        remote_url = (doctor.get("git") or {}).get("remote_url")
        remote_label = f"{remote} ({remote_url})" if remote_url else str(remote)
        push_refs = [str(ref) for ref in git.get("push_refs") or []]
        gates = [
            str(gate.get("name"))
            for gate in config.get("gates") or []
            if gate.get("name")
        ]
        verify_hooks = [
            str(hook.get("name"))
            for hook in (config.get("deploy") or {}).get("verify") or []
            if hook.get("name")
        ]
        jobs_by_id: dict[str, dict[str, Any]] = {}
        for key in ("jobs", "attention_jobs"):
            for job in status.get(key) or []:
                jobs_by_id[str(job.get("id"))] = job
        lines = [
            f"Repository: {self.repo}",
            f"Destination: {remote_label} atomically updates "
            f"{', '.join(push_refs) or 'unknown refs'} and records "
            "refs/mergetrain/deploys/<deploy-sha>",
            f"Integration source: {git.get('integration_ref', 'unknown')}",
            "Pre-push gate policy evaluated: "
            + ", ".join(dict.fromkeys(["diff-check", *gates])),
            "Post-push verification: " + (", ".join(verify_hooks) or "none configured"),
            f"doctor next_action: {doctor.get('next_action', 'unknown')}",
        ]
        lines.append(f"Selected change set ({train.get('train_size')} job(s)):")
        for member in train.get("branches") or []:
            job = jobs_by_id.get(str(member.get("job_id"))) or {}
            task = " ".join(str(job.get("task") or "task not recorded").split())
            lines.append(
                f"- #{member.get('job_id')} {task}: {member.get('branch')} "
                f"@{str(member.get('validated_head_sha') or '')[:12]}"
            )
        lines.append(
            "Recorded validation: "
            f"{train.get('validated_at') or 'time unavailable'}; integration base "
            f"{str(train.get('validation_base_sha') or 'unavailable')[:12]}; "
            f"validated commit {str(train.get('validation_sha') or 'unavailable')[:12]}"
        )
        current_sha = str(train.get("current_integration_sha") or "")
        if train.get("integration_changed_since_validation") is True:
            lines.append(
                "Reassembly risk: the local integration ref advanced since "
                "validation to "
                f"{current_sha[:12] or 'an unknown commit'}; deploy will reassemble "
                "the selected change set and evaluate the configured pre-push "
                "gate policy before push"
            )
        else:
            lines.append(
                "Reassembly: deploy will rebuild the selected change set and "
                "evaluate the configured pre-push gate policy before push"
            )
        attention_source = status.get("attention_jobs")
        if attention_source is None:
            attention_source = status.get("jobs") or []
        attention = []
        for job in attention_source:
            status_label = str(job.get("status") or "unknown")
            verification_unknown = (
                status_label == "deployed" and job.get("verify_status") == "unknown"
            )
            if status_label not in {"blocked", "failed", "needs_reconcile"} and not (
                verification_unknown
            ):
                continue
            if verification_unknown:
                status_label += " (verification unknown)"
            attention.append(
                f"#{job.get('id')} "
                f"{' '.join(str(job.get('task') or 'task not recorded').split())} "
                f"— {job.get('branch')} {status_label}: "
                f"{' '.join(str(job.get('note', '')).split())[:160]}"
            )
        if attention:
            lines.append("Needs attention: " + "; ".join(attention))
        return "\n".join(lines)

    def select_train(
        self, status: dict[str, Any], train_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Pick the train to ship, or return the refusal that explains why not.

        Never guesses between candidates: with several pending trains the caller
        has to name one, because picking for them would ship code nobody chose.
        """

        trains = [
            train
            for train in status.get("validated_trains") or []
            if train.get("deploy_eligible")
        ]
        if not trains:
            return None, _error(
                "no_validated_train",
                "no deploy-eligible validated train is pending; "
                "run mergetrain_validate first",
                next_action=status.get("next_action"),
            )
        if train_id:
            for train in trains:
                if str(train.get("train_id")) == train_id:
                    return train, None
            return None, _error(
                "train_not_found",
                f"no deploy-eligible validated train {train_id} is pending",
                pending_train_ids=[str(t.get("train_id")) for t in trains],
            )
        if len(trains) > 1:
            return None, _error(
                "train_id_required",
                "several validated trains are pending; name the one to deploy",
                pending_train_ids=[str(t.get("train_id")) for t in trains],
            )
        return trains[0], None

    async def deploy(self, ctx: Context, train_id: str = "") -> dict[str, Any]:
        """Deploy one validated train after a human accepts it.

        There is no ``confirm`` parameter, on purpose: a model-supplied argument
        would be the model confirming its own deploy. The accept has to come
        through the client's elicitation dialog, and when the client cannot show
        one this refuses and hands the human a terminal command instead of
        quietly shipping.
        """

        doctor = await self.doctor()
        status = await self.status(limit=50)
        for payload in (doctor, status):
            if payload.get("ok") is False:
                return payload
        train, refusal = self.select_train(status, train_id)
        if refusal is not None:
            return refusal
        assert train is not None
        chosen = str(train.get("train_id"))
        summary = self.deploy_summary(doctor, status, train)
        command = f"mergetrain --repo {self.repo} run-batch --deploy --train-id {chosen}"

        if not _client_can_elicit(ctx):
            return _error(
                "confirmation_required",
                "this client cannot show a confirmation dialog, so the deploy "
                "was not started; run it in a terminal after reviewing the "
                f"summary:\n{summary}\n\n{command}",
                train_id=chosen,
                command=command,
            )
        accepted, reason = await _elicit_deploy_accept(ctx, summary)
        if not accepted:
            return _error(
                "deploy_not_confirmed",
                f"the deploy was not confirmed ({reason}); nothing was pushed",
                train_id=chosen,
                command=command,
            )
        return await self._json(["run-batch", "--deploy", "--train-id", chosen, "--json"])


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
        params = ctx.session.client_params
        return params is not None and params.capabilities.elicitation is not None
    except Exception:
        return False


async def _elicit_deploy_accept(ctx: Any, summary: str) -> tuple[bool, str]:
    """Show the summary and require an explicit accept plus an explicit yes."""

    if _DeployConfirmation is None:  # pragma: no cover - needs the extra missing
        return False, "the confirmation schema is unavailable"

    message = (
        "mergetrain will atomically push the change set below. "
        "This ships code.\n\n" + summary
    )
    try:
        result = await ctx.elicit(message=message, schema=_DeployConfirmation)
    except Exception as exc:  # a transport or client-side failure is not consent
        return False, f"the confirmation dialog failed: {exc}"
    action = getattr(result, "action", "")
    if action != "accept":
        outcome = {"decline": "declined", "cancel": "cancelled"}.get(
            action, action or "did not respond"
        )
        return False, f"the human {outcome}"
    data = getattr(result, "data", None)
    if data is None or not getattr(data, "confirm", False):
        return False, "the confirmation was left unchecked"
    return True, "accepted"


def build_server(repo: Path) -> Any:
    """Register the tool surface, annotated with what each tool actually does."""

    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    tools = MergetrainTools(repo=repo)
    server = FastMCP(
        name="mergetrain",
        instructions=(
            "mergetrain is a local merge-and-push queue for coding-agent "
            "branches. Read state first (mergetrain_doctor, mergetrain_status) "
            "and act on what the JSON says, never on assumption: 'ok' means "
            "only that the command ran, a run's outcome is in 'result', and "
            "repo health is in 'health'. 'next_action' is advisory. "
            "mergetrain_deploy ships code and needs a human accept; unattended "
            "deploy, destructive cleanup and cancellation are not available "
            "here by design -- ask the human to run those in a terminal."
        ),
    )

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    for fn, name, hints in (
        (tools.status, "mergetrain_status", read_only),
        (tools.doctor, "mergetrain_doctor", read_only),
        (tools.inspect_job, "mergetrain_inspect", read_only),
        (tools.history, "mergetrain_history", read_only),
        (tools.stats, "mergetrain_stats", read_only),
        (tools.agent_contract, "mergetrain_agent_contract", read_only),
        (tools.gc_preview, "mergetrain_gc_preview", read_only),
        (tools.events, "mergetrain_events", read_only),
        (tools.logs, "mergetrain_logs", read_only),
        # Not read-only: validate runs gate commands and moves job status.
        (
            tools.validate,
            "mergetrain_validate",
            ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
        ),
        (
            tools.enqueue,
            "mergetrain_enqueue",
            ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
        ),
        (
            tools.deploy,
            "mergetrain_deploy",
            ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
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
