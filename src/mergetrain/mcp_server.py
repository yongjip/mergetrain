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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
_LOG_TAIL_MAX_LINES = 500


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """Mirror the CLI's one failure envelope so consumers parse a single shape."""

    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": code, "message": message, "retryable": False},
    }
    payload.update(extra)
    return payload


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
        # Off the event loop: a gate suite can run for minutes, and a blocking
        # call here would stall the whole stdio session, including cancellation.
        return await asyncio.to_thread(
            subprocess.run,
            self._argv(args),
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
            check=False,
        )

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
            detail = (completed.stderr or completed.stdout).strip()[:2000]
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
            detail = (completed.stderr or "").strip()[:2000]
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
            detail = (completed.stderr or completed.stdout).strip()[:2000]
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

    def deploy_summary(self, doctor: dict[str, Any], status: dict[str, Any]) -> str:
        """Build the deploy summary the operating contract requires.

        Same content CLAUDE.md asks an agent to post before deploying: the
        train, its jobs and recorded HEADs, the integration ref, the advisory
        next action, and anything blocked or failed.
        """

        config = doctor.get("config") or {}
        git = config.get("git") or {}
        lines = [
            f"Repository: {self.repo}",
            f"Integration ref: {git.get('integration_ref', 'unknown')} "
            f"(pushes {', '.join(git.get('push_refs') or []) or 'unknown'})",
            f"doctor next_action: {doctor.get('next_action', 'unknown')}",
        ]
        trains = status.get("validated_trains") or []
        for train in trains:
            members = ", ".join(
                f"#{member.get('job_id')} {member.get('branch')} "
                f"@{str(member.get('validated_head_sha') or '')[:12]}"
                for member in train.get("branches") or []
            )
            lines.append(
                f"Train {train.get('train_id')} "
                f"(size {train.get('train_size')}, "
                f"deploy_eligible={train.get('deploy_eligible')}): {members}"
            )
        attention = [
            f"#{job.get('id')} {job.get('branch')} {job.get('status')}: "
            f"{' '.join(str(job.get('note', '')).split())[:160]}"
            for job in status.get("jobs") or []
            if job.get("status") in {"blocked", "failed", "needs_reconcile"}
        ]
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
        summary = self.deploy_summary(doctor, status)
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
        accepted, reason = await _elicit_deploy_accept(ctx, summary, chosen)
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


async def _elicit_deploy_accept(
    ctx: Any, summary: str, train_id: str
) -> tuple[bool, str]:
    """Show the summary and require an explicit accept plus an explicit yes."""

    if _DeployConfirmation is None:  # pragma: no cover - needs the extra missing
        return False, "the confirmation schema is unavailable"

    message = (
        f"mergetrain will push validated train {train_id}. "
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
