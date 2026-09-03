from __future__ import annotations

import asyncio
import importlib.util
import inspect
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from mergetrain.cli import main
from mergetrain.config import load_config
from mergetrain.mcp_server import (
    MergetrainTools,
    _deploy_approval,
    _replace_local_path_root,
    _stop_cli_process,
)
from mergetrain.store import connect, enqueue_job, mark_job

try:
    HAS_MCP = importlib.util.find_spec("mcp.server.mcpserver") is not None
except ModuleNotFoundError:
    HAS_MCP = False


def completed(stdout: str, returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=["mergetrain"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@contextmanager
def current_checkout_cli() -> Iterator[None]:
    """Make real MCP child-process tests execute the checkout under test."""

    source_path = str(Path(__file__).resolve().parents[1] / "src")
    inherited = os.environ.get("PYTHONPATH", "")
    child_pythonpath = os.pathsep.join(
        part for part in (source_path, inherited) if part
    )
    with patch.dict(os.environ, {"PYTHONPATH": child_pythonpath}):
        yield


DOCTOR = {
    "ok": True,
    "contract_version": 1,
    "health": True,
    "next_action": "deploy_validated_train_when_approved",
    "git": {"remote_url": "git@github.com:example/checkout.git"},
    "config": {
        "git": {
            "remote": "origin",
            "integration_ref": "origin/main",
            "push_refs": ["main"],
        },
        "gates": [{"name": "ruff"}, {"name": "tests"}],
        "deploy": {"verify": [{"name": "github-ci"}]},
    },
}

TRAIN = {
    "train_id": "abc123",
    "train_size": 2,
    "job_ids": [7, 8],
    "branches": [
        {"job_id": 7, "branch": "agent/one", "validated_head_sha": "a" * 40},
        {"job_id": 8, "branch": "agent/two", "validated_head_sha": "b" * 40},
    ],
    "validated_at": "2026-09-02T00:00:00Z",
    "validation_base_sha": "c" * 40,
    "validation_sha": "d" * 40,
    "current_integration_sha": "e" * 40,
    "integration_changed_since_validation": True,
    "deploy_eligible": True,
}

STATUS = {
    "ok": True,
    "contract_version": 1,
    "next_action": "deploy_validated_train_when_approved",
    "validated_trains": [TRAIN],
    "jobs": [
        {
            "id": 7,
            "task": "Add checkout guard",
            "branch": "agent/one",
            "status": "validated",
            "note": "",
        },
        {
            "id": 8,
            "task": "Handle payment retry",
            "branch": "agent/two",
            "status": "validated",
            "note": "",
        },
    ],
    "attention_jobs": [
        {
            "id": 7,
            "task": "Add checkout guard",
            "branch": "agent/one",
            "status": "validated",
            "note": "",
        },
        {
            "id": 8,
            "task": "Handle payment retry",
            "branch": "agent/two",
            "status": "validated",
            "note": "",
        },
        {
            "id": 9,
            "task": "Repair refund calculation",
            "branch": "agent/three",
            "status": "blocked",
            "note": "gate tests failed",
        },
    ],
}

PLAN_SHA = "f" * 64
PREVIEW = {
    "ok": True,
    "result": "confirmation_required",
    "deploy_plan_sha": PLAN_SHA,
    "push_plan": {
        "remote": "origin",
        "url": "git@github.com:example/checkout.git",
        "refs": [{"source": "HEAD", "target": "main", "spec": "HEAD:main"}],
    },
    "reuse": {"decision": {"action": "rerun"}},
    "jobs": [
        {"id": 7, "task": "Add checkout guard", "branch": "agent/one", "train_id": "abc123"},
        {"id": 8, "task": "Handle payment retry", "branch": "agent/two", "train_id": "abc123"},
    ],
}


def is_guarded_deploy_call(args: list[str]) -> bool:
    return args[:1] == ["deploy"] and "--expected-plan" in args


class FakeCapabilities:
    def __init__(self, *, elicitation: bool) -> None:
        # The real ClientCapabilities carries an ElicitationCapability or None.
        self.elicitation = object() if elicitation else None


class FakeContext:
    """Minimal context for direct deploy-plan tests."""

    def __init__(self, *, elicitation: bool = True, action: str = "accept", confirm: bool = True):
        self.client_capabilities = FakeCapabilities(elicitation=elicitation)
        self.action = action
        self.confirm = confirm


class ToolSurfaceTests(unittest.TestCase):
    """The surface is a safety boundary: absent parameters are the enforcement."""

    def setUp(self) -> None:
        self.tools = MergetrainTools(repo=Path("/repo"))

    def test_no_tool_can_reach_unattended_deploy_or_destruction(self) -> None:
        forbidden = {"auto", "apply", "delete_branches", "force", "confirm", "yes"}
        for name in ("status", "inspect", "validate", "enqueue", "deploy"):
            parameters = set(inspect.signature(getattr(self.tools, name)).parameters)
            self.assertEqual(
                parameters & forbidden,
                set(),
                f"{name} exposes a parameter that must never be model-supplied",
            )

    def test_destructive_commands_are_not_implemented_at_all(self) -> None:
        for name in ("daemon", "cancel", "unlock", "dismiss", "reconcile", "recover"):
            self.assertFalse(
                hasattr(self.tools, name),
                f"{name} must stay terminal-only, not become an MCP tool",
            )

    def test_enqueue_always_pins_the_reviewed_sha(self) -> None:
        with patch.object(MergetrainTools, "_run", return_value=completed("{}")) as run:
            asyncio.run(self.tools.enqueue(task="t", branch="agent/one"))
        args = run.call_args.args[0]
        self.assertEqual(args, ["enqueue", "--task", "t", "--branch", "agent/one", "--json"])
        self.assertNotIn("--auto", args)

    def test_enqueue_uses_bound_repo_when_server_cwd_is_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td).resolve()
            subprocess.run(
                ["git", "init", "-q", "--initial-branch=agent/one", str(repo)],
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"], cwd=repo, check=True
            )
            subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=repo, check=True)
            (repo / ".mergetrain.yaml").write_text(
                "version: 2\nproject:\n  name: mcp-cwd\ngates: []\n", encoding="utf-8"
            )
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "ready"], cwd=repo, check=True)
            subprocess.run(
                ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                cwd=repo,
                check=True,
            )

            tools = MergetrainTools(repo=repo)
            with current_checkout_cli():
                payload = asyncio.run(tools.enqueue(task="mcp cwd", branch="agent/one"))

        self.assertTrue(payload["ok"])
        self.assertEqual(Path(payload["job"]["worktree_path"]), repo)

    def test_operator_only_reads_are_not_public_methods(self) -> None:
        for name in ("doctor", "history", "stats", "agent_contract", "gc_preview"):
            self.assertFalse(hasattr(self.tools, name), name)

    def test_bounded_arguments_are_clamped(self) -> None:
        with patch.object(MergetrainTools, "_run", return_value=completed("{}")) as run:
            asyncio.run(self.tools.status(limit=10_000))
        self.assertIn("200", run.call_args.args[0])
        with patch.object(MergetrainTools, "_run", return_value=completed("")) as run:
            asyncio.run(self.tools.inspect(job_id=3, detail="logs", limit=10_000))
        self.assertIn("200", run.call_args.args[0])


class PayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = MergetrainTools(repo=Path("/repo"))

    def test_cli_payload_is_returned_verbatim(self) -> None:
        with patch.object(MergetrainTools, "_run", return_value=completed(json.dumps(STATUS))):
            payload = asyncio.run(self.tools.status())
        self.assertEqual(payload, STATUS)

    def test_real_status_keeps_persisted_note_secrets_out_of_mcp_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                "version: 2\nproject:\n  name: mcp-redaction-test\n",
                encoding="utf-8",
            )
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="secret", branch="feature/secret")
                mark_job(
                    conn,
                    job.id,
                    status="blocked",
                    note="API_TOKEN=mcp-secret --password mcp-password",
                )
            finally:
                conn.close()

            # pytest's ``pythonpath = ["src"]`` does not propagate to this
            # MCP CLI child, so make the real-process test hermetic.
            with current_checkout_cli():
                payload = asyncio.run(MergetrainTools(repo=repo).status())

        rendered = json.dumps(payload)
        self.assertNotIn("mcp-secret", rendered)
        self.assertNotIn("mcp-password", rendered)
        self.assertIn("API_TOKEN=[redacted]", rendered)
        self.assertIn("--password [redacted]", rendered)

    def test_a_failure_envelope_is_passed_through_not_rewritten(self) -> None:
        envelope = {
            "ok": False,
            "error": {"code": "config_error", "message": "bad", "retryable": False},
        }
        with patch.object(
            MergetrainTools, "_run", return_value=completed(json.dumps(envelope), returncode=2)
        ):
            payload = asyncio.run(self.tools.status())
        self.assertEqual(payload, envelope)

    def test_unreadable_output_becomes_the_one_failure_shape(self) -> None:
        with patch.object(
            MergetrainTools, "_run", return_value=completed("not json", returncode=1, stderr="boom")
        ):
            payload = asyncio.run(self.tools.status())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "cli_output_unreadable")
        self.assertIn("boom", payload["error"]["message"])

    def test_unreadable_output_redacts_stderr_and_minimizes_local_paths(self) -> None:
        stderr = (
            "API_TOKEN=super-secret --password hunter2 "
            "https://agent:credential@example.test/repo "
            "/repo/src/startup.py"
        )
        with patch.object(
            MergetrainTools,
            "_run",
            return_value=completed("not json", returncode=1, stderr=stderr),
        ):
            payload = asyncio.run(self.tools.status())
        message = payload["error"]["message"]
        for secret in ("super-secret", "hunter2", "credential"):
            self.assertNotIn(secret, message)
        self.assertIn("API_TOKEN=[redacted]", message)
        self.assertIn("--password [redacted]", message)
        self.assertIn("https://agent:[redacted]@example.test/repo", message)
        self.assertIn("[repo]/src/startup.py", message)

    def test_unreadable_output_redacts_stdout_fallback(self) -> None:
        with patch.object(
            MergetrainTools,
            "_run",
            return_value=completed("GITHUB_PAT=ghp-secret", returncode=1),
        ):
            payload = asyncio.run(self.tools.status())
        self.assertNotIn("ghp-secret", payload["error"]["message"])
        self.assertIn("GITHUB_PAT=[redacted]", payload["error"]["message"])

    def test_local_path_minimization_accepts_both_separator_styles(self) -> None:
        self.assertEqual(
            _replace_local_path_root("at /repo/src/main.py", r"\repo", "[repo]"),
            "at [repo]/src/main.py",
        )
        self.assertEqual(
            _replace_local_path_root(r"at \repo\src\main.py", "/repo", "[repo]"),
            r"at [repo]\src\main.py",
        )

    def test_valid_cli_json_is_returned_verbatim_even_if_it_looks_sensitive(self) -> None:
        envelope = {
            "ok": False,
            "error": {
                "code": "upstream_contract",
                "message": "API_TOKEN=contract-owned",
                "retryable": False,
            },
        }
        with patch.object(MergetrainTools, "_run", return_value=completed(json.dumps(envelope))):
            payload = asyncio.run(self.tools.status())
        self.assertEqual(payload, envelope)

    def test_timeout_is_reported_not_raised(self) -> None:
        with patch.object(
            MergetrainTools,
            "_run",
            side_effect=subprocess.TimeoutExpired(cmd="mergetrain", timeout=1),
        ):
            payload = asyncio.run(self.tools.status())
        self.assertEqual(payload["error"]["code"], "cli_timeout")

    def test_events_returns_the_cli_frames_unchanged(self) -> None:
        stdout = (
            '{"type": "stream_start", "contract_version": 1, "after_event_id": 0}\n'
            '{"type": "event", "id": 4, "phase": "validating"}\n'
            "not json\n"
        )
        with patch.object(MergetrainTools, "_run", return_value=completed(stdout)):
            payload = asyncio.run(self.tools.inspect(job_id=1, detail="events"))
        self.assertEqual(
            payload["frames"],
            [
                {"type": "stream_start", "contract_version": 1, "after_event_id": 0},
                {"type": "event", "id": 4, "phase": "validating"},
            ],
        )

    def test_events_failure_redacts_synthesized_stderr(self) -> None:
        with patch.object(
            MergetrainTools,
            "_run",
            return_value=completed("", returncode=1, stderr="ACCESS_TOKEN=event-secret"),
        ):
            payload = asyncio.run(self.tools.inspect(job_id=1, detail="events"))
        self.assertNotIn("event-secret", payload["error"]["message"])
        self.assertIn("ACCESS_TOKEN=[redacted]", payload["error"]["message"])

    def test_logs_failure_redacts_synthesized_detail(self) -> None:
        with patch.object(
            MergetrainTools,
            "_run",
            return_value=completed("", returncode=1, stderr="--api-key log-secret"),
        ):
            payload = asyncio.run(self.tools.inspect(job_id=4, detail="logs"))
        self.assertNotIn("log-secret", payload["error"]["message"])
        self.assertIn("--api-key [redacted]", payload["error"]["message"])

    def test_successful_raw_log_output_remains_unchanged(self) -> None:
        raw = "API_TOKEN=intentionally-raw\n"
        with patch.object(MergetrainTools, "_run", return_value=completed(raw)):
            payload = asyncio.run(self.tools.inspect(job_id=4, detail="logs"))
        self.assertEqual(payload["log"], raw)


class ProcessLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = MergetrainTools(repo=Path("/repo"))

    def test_successful_async_child_preserves_completed_process_contract(self) -> None:
        argv = [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)",
        ]
        with patch.object(MergetrainTools, "_argv", return_value=argv):
            result = asyncio.run(self.tools._run(["doctor"]))
        self.assertEqual(result.args, argv)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, f"out{os.linesep}")
        self.assertEqual(result.stderr, f"err{os.linesep}")

    def test_windows_break_failure_falls_back_to_tree_termination(self) -> None:
        process = MagicMock()
        process.pid = 123
        process.returncode = None
        process.send_signal.side_effect = OSError("no console")
        process.wait = AsyncMock(return_value=0)
        stop_tree = AsyncMock()
        with (
            patch("mergetrain.mcp_server.os.name", "nt"),
            patch("mergetrain.mcp_server._stop_windows_process_tree", stop_tree),
        ):
            self.assertTrue(asyncio.run(_stop_cli_process(process)))
        stop_tree.assert_awaited_once_with(process)
        process.wait.assert_awaited_once()

    def test_cancelling_run_stops_the_cli_process_group(self) -> None:
        heartbeat_program = (
            "from pathlib import Path\n"
            "import sys, time\n"
            "heartbeat = Path(sys.argv[1])\n"
            "counter = 0\n"
            "while True:\n"
            "    heartbeat.write_text(str(counter), encoding='utf-8')\n"
            "    counter += 1\n"
            "    time.sleep(0.02)\n"
        )
        parent_program = (
            "from pathlib import Path\n"
            "import os, signal, subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[2]], "
            "start_new_session=(os.name == 'posix'))\n"
            "if os.name == 'posix':\n"
            "    def interrupt(_signum, _frame):\n"
            "        os.killpg(child.pid, signal.SIGTERM)\n"
            "        child.wait(timeout=5)\n"
            "        raise KeyboardInterrupt\n"
            "    signal.signal(signal.SIGINT, interrupt)\n"
            "Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}', encoding='utf-8')\n"
            "time.sleep(60)\n"
        )

        async def wait_for_file(path: Path) -> None:
            for _ in range(500):
                if path.exists() and path.stat().st_size:
                    return
                await asyncio.sleep(0.01)
            self.fail(f"timed out waiting for {path.name}")

        async def wait_for_change(path: Path, previous: str) -> None:
            for _ in range(500):
                if path.read_text(encoding="utf-8") != previous:
                    return
                await asyncio.sleep(0.01)
            self.fail(f"timed out waiting for {path.name} to change")

        async def scenario(root: Path) -> None:
            pid_path = root / "pids.txt"
            heartbeat_path = root / "heartbeat.txt"
            argv = [
                sys.executable,
                "-c",
                parent_program,
                str(pid_path),
                str(heartbeat_path),
                heartbeat_program,
            ]
            task: asyncio.Task[subprocess.CompletedProcess[str]] | None = None
            parent_pid = 0
            try:
                with patch.object(MergetrainTools, "_argv", return_value=argv):
                    task = asyncio.create_task(self.tools._run(["doctor"]))
                    await wait_for_file(pid_path)
                    await wait_for_file(heartbeat_path)
                    parent_pid = int(pid_path.read_text(encoding="utf-8").split()[0])
                    before = heartbeat_path.read_text(encoding="utf-8")
                    await wait_for_change(heartbeat_path, before)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                await asyncio.sleep(0.1)
                stopped = heartbeat_path.read_text(encoding="utf-8")
                await asyncio.sleep(0.15)
                self.assertEqual(
                    heartbeat_path.read_text(encoding="utf-8"),
                    stopped,
                    "the grandchild kept running after the MCP task was cancelled",
                )
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                if parent_pid:
                    if os.name == "posix":
                        with suppress(ProcessLookupError):
                            os.killpg(parent_pid, signal.SIGKILL)
                    else:  # pragma: no cover - Windows cleanup belt
                        subprocess.run(
                            ["taskkill", "/PID", str(parent_pid), "/T", "/F"],
                            check=False,
                            capture_output=True,
                            timeout=10,
                        )

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(Path(tmp)))


class DeployGateTests(unittest.TestCase):
    """A transcript must never show a deploy no human explicitly accepted."""

    def setUp(self) -> None:
        self.tools = MergetrainTools(repo=Path("/repo"))

    def _deploy(self, ctx: Any, *, preview: dict[str, Any] | None = None) -> Any:
        calls: list[list[str]] = []

        async def fake_run(_self: Any, args: list[str]) -> Any:
            calls.append(args)
            if args == ["deploy", "--json"]:
                return completed(json.dumps(preview or PREVIEW))
            return completed(json.dumps({"ok": True, "result": "success"}))

        with patch.object(MergetrainTools, "_run", new=fake_run):
            plan = asyncio.run(self.tools.prepare_deploy(ctx))
            data = SimpleNamespace(confirm=ctx.confirm) if ctx.action == "accept" else None
            approval = SimpleNamespace(action=ctx.action, data=data)
            payload = asyncio.run(self.tools.deploy(plan, approval))
        return payload, calls

    def test_accepted_confirmation_deploys_the_exact_plan(self) -> None:
        ctx = FakeContext(action="accept", confirm=True)
        payload, calls = self._deploy(ctx)
        self.assertEqual(payload, {"ok": True, "result": "success"})
        self.assertIn(
            ["deploy", "--expected-plan", PLAN_SHA, "--json"],
            calls,
        )
        self.assertEqual(sum(is_guarded_deploy_call(args) for args in calls), 1)

    def test_the_human_sees_the_operating_contract_summary(self) -> None:
        ctx = FakeContext()
        with patch.object(
            MergetrainTools,
            "_run",
            return_value=completed(json.dumps(PREVIEW)),
        ):
            plan = asyncio.run(self.tools.prepare_deploy(ctx))
        shown = plan.summary
        self.assertIn("Add checkout guard", shown)
        self.assertIn("Destination: git@github.com:example/checkout.git (main)", shown)
        self.assertIn("Gate plan: rerun", shown)
        self.assertIn("checked again before push", shown)
        self.assertNotIn("abc123", shown)

    def test_the_human_sees_a_missing_project_gate_warning(self) -> None:
        preview = {
            **PREVIEW,
            "warnings": [
                {
                    "code": "no_configured_gates",
                    "severity": "warning",
                    "summary": "No project gates are configured.",
                }
            ],
        }
        with patch.object(
            MergetrainTools,
            "_run",
            return_value=completed(json.dumps(preview)),
        ):
            plan = asyncio.run(self.tools.prepare_deploy(FakeContext()))
        self.assertIn("Warning: No project gates are configured.", plan.summary)

    def test_a_client_without_elicitation_is_refused_with_instructions(self) -> None:
        ctx = FakeContext(elicitation=False)
        payload, calls = self._deploy(ctx)
        self.assertEqual(payload["error"]["code"], "confirmation_required")
        self.assertTrue(payload["command"].endswith(" deploy"))
        self.assertNotIn("expected-plan", payload["command"])
        self.assertFalse(any(is_guarded_deploy_call(args) for args in calls))

    def test_a_declined_dialog_does_not_deploy(self) -> None:
        payload, calls = self._deploy(FakeContext(action="decline"))
        self.assertEqual(payload["error"]["code"], "deploy_not_confirmed")
        self.assertFalse(any(is_guarded_deploy_call(args) for args in calls))

    def test_a_cancelled_dialog_does_not_deploy(self) -> None:
        payload, calls = self._deploy(FakeContext(action="cancel"))
        self.assertEqual(payload["error"]["code"], "deploy_not_confirmed")
        self.assertFalse(any(is_guarded_deploy_call(args) for args in calls))

    def test_an_accept_with_the_box_unchecked_does_not_deploy(self) -> None:
        payload, calls = self._deploy(FakeContext(action="accept", confirm=False))
        self.assertEqual(payload["error"]["code"], "deploy_not_confirmed")
        self.assertIn("unchecked", payload["error"]["message"])
        self.assertFalse(any(is_guarded_deploy_call(args) for args in calls))

    def test_a_broken_preview_stops_the_deploy(self) -> None:
        envelope = {
            "ok": False,
            "error": {"code": "config_error", "message": "too new", "retryable": False},
        }
        payload, calls = self._deploy(FakeContext(), preview=envelope)
        self.assertEqual(payload, envelope)
        self.assertFalse(any(is_guarded_deploy_call(args) for args in calls))

    def test_a_non_plan_result_is_refused(self) -> None:
        payload, calls = self._deploy(
            FakeContext(),
            preview={"ok": True, "result": "success", "note": "no work"},
        )
        self.assertEqual(payload["error"]["code"], "deploy_plan_unavailable")
        self.assertFalse(any(is_guarded_deploy_call(args) for args in calls))

    def test_confirmation_requires_a_pydantic_accept_shape(self) -> None:
        # Guards the helper itself: anything but action=accept plus confirm=True
        # is a refusal, whatever the resolver returns.
        for action, confirm, expected in (
            ("accept", True, True),
            ("accept", False, False),
            ("decline", True, False),
            ("cancel", True, False),
        ):
            data = SimpleNamespace(confirm=confirm) if action == "accept" else None
            accepted, _ = _deploy_approval(SimpleNamespace(action=action, data=data))
            self.assertEqual(accepted, expected, f"{action}/{confirm}")


@unittest.skipUnless(HAS_MCP, "the mcp extra is not installed")
class MCPV2DeployProtocolTests(unittest.TestCase):
    """Exercise the real v2 client, including its InputRequiredResult retry."""

    def _call(
        self,
        *,
        action: str | None,
        confirm: bool = True,
        mode: str = "auto",
        execute_payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[list[str]], list[str]]:
        from mcp import Client
        from mcp.types import ElicitResult

        calls: list[list[str]] = []
        messages: list[str] = []

        async def fake_json(_self: Any, args: list[str]) -> dict[str, Any]:
            calls.append(args)
            if args == ["deploy", "--json"]:
                return PREVIEW
            return execute_payload or {"ok": True, "result": "success"}

        async def confirm_deploy(_ctx: Any, params: Any) -> Any:
            messages.append(params.message)
            content = {"confirm": confirm} if action == "accept" else None
            return ElicitResult(action=action, content=content)

        async def scenario() -> dict[str, Any]:
            from mergetrain.mcp_server import build_server

            kwargs = {"mode": mode}
            if action is not None:
                kwargs["elicitation_callback"] = confirm_deploy
            async with Client(build_server(Path("/repo")), **kwargs) as client:
                result = await client.call_tool("mergetrain_deploy", {})
            return result.model_dump(by_alias=True, exclude_none=True)["structuredContent"]

        with patch.object(MergetrainTools, "_json", new=fake_json):
            payload = asyncio.run(scenario())
        return payload, calls, messages

    def test_modern_client_accepts_and_deploys_exactly_once(self) -> None:
        payload, calls, messages = self._call(action="accept")
        self.assertEqual(payload, {"ok": True, "result": "success"})
        self.assertEqual(
            sum(is_guarded_deploy_call(args) for args in calls),
            1,
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("atomically push the change set below", messages[0])
        self.assertNotIn("abc123", messages[0])

    def test_legacy_protocol_client_still_uses_the_v2_resolver(self) -> None:
        payload, calls, messages = self._call(action="accept", mode="legacy")
        self.assertEqual(payload, {"ok": True, "result": "success"})
        self.assertEqual(len(messages), 1)
        self.assertTrue(any(is_guarded_deploy_call(args) for args in calls))

    def test_decline_cancel_and_unchecked_accept_never_deploy(self) -> None:
        for action, confirm in (("decline", True), ("cancel", True), ("accept", False)):
            with self.subTest(action=action, confirm=confirm):
                payload, calls, _ = self._call(action=action, confirm=confirm)
                self.assertEqual(payload["error"]["code"], "deploy_not_confirmed")
                self.assertTrue(not any(is_guarded_deploy_call(args) for args in calls))

    def test_client_without_form_elicitation_gets_terminal_fallback(self) -> None:
        payload, calls, messages = self._call(action=None)
        self.assertEqual(payload["error"]["code"], "confirmation_required")
        self.assertTrue(payload["command"].endswith(" deploy"))
        self.assertNotIn("expected-plan", payload["command"])
        self.assertEqual(messages, [])
        self.assertFalse(any(is_guarded_deploy_call(args) for args in calls))

    def test_state_change_after_confirmation_is_rechecked_and_refused(self) -> None:
        changed = {
            "ok": False,
            "error": {
                "code": "deploy_plan_changed",
                "message": "the exact plan changed; nothing was pushed",
                "retryable": False,
            },
        }
        payload, calls, messages = self._call(action="accept", execute_payload=changed)
        self.assertEqual(payload["error"]["code"], "deploy_plan_changed")
        self.assertEqual(len(messages), 1)
        # The adapter invokes exactly one hash-bound execution. The returned
        # refusal proves the CLI rechecked the plan before its Git push path.
        self.assertEqual(sum(is_guarded_deploy_call(args) for args in calls), 1)

    def test_client_callback_error_is_fail_closed(self) -> None:
        from mcp import Client
        from mcp.shared.exceptions import MCPError
        from mcp.types import ErrorData

        from mergetrain.mcp_server import build_server

        calls: list[list[str]] = []

        async def fake_json(_self: Any, args: list[str]) -> dict[str, Any]:
            calls.append(args)
            if args == ["deploy", "--json"]:
                return PREVIEW
            return {"ok": True, "result": "must not happen"}

        async def broken_callback(_ctx: Any, _params: Any) -> ErrorData:
            return ErrorData(code=-32603, message="dialog host failed")

        async def scenario() -> None:
            async with Client(
                build_server(Path("/repo")), elicitation_callback=broken_callback
            ) as client:
                with self.assertRaises(MCPError):
                    await client.call_tool("mergetrain_deploy", {})

        with patch.object(MergetrainTools, "_json", new=fake_json):
            asyncio.run(scenario())
        self.assertFalse(any(is_guarded_deploy_call(args) for args in calls))


@unittest.skipUnless(HAS_MCP, "the mcp extra is not installed")
class ServerRegistrationTests(unittest.TestCase):
    def test_every_tool_is_registered_with_truthful_annotations(self) -> None:
        from mergetrain.mcp_server import build_server

        server = build_server(Path("/repo"))
        tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
        self.assertEqual(
            set(tools),
            {
                "mergetrain_status",
                "mergetrain_inspect",
                "mergetrain_validate",
                "mergetrain_enqueue",
                "mergetrain_deploy",
            },
        )
        for name in ("mergetrain_status", "mergetrain_inspect"):
            self.assertTrue(tools[name].annotations.read_only_hint, name)
        # validate runs gate commands and moves job status, so claiming
        # read-only would misinform the client about its side effects.
        self.assertFalse(tools["mergetrain_validate"].annotations.read_only_hint)
        self.assertTrue(tools["mergetrain_deploy"].annotations.destructive_hint)

    def test_the_deploy_context_is_injected_not_a_model_argument(self) -> None:
        from mergetrain.mcp_server import build_server

        server = build_server(Path("/repo"))
        deploy = next(
            tool for tool in asyncio.run(server.list_tools()) if tool.name == "mergetrain_deploy"
        )
        self.assertNotIn("ctx", deploy.input_schema.get("properties", {}))
        self.assertEqual(set(deploy.input_schema.get("properties", {})), set())


class SubcommandTests(unittest.TestCase):
    def test_the_subcommand_starts_the_stdio_server(self) -> None:
        with patch("mergetrain.mcp_server.run_server", return_value=0) as run_server:
            self.assertEqual(main(["mcp"]), 0)
        run_server.assert_called_once()

    def test_a_missing_extra_prints_the_install_hint(self) -> None:
        err = io.StringIO()
        with (
            patch("mergetrain.mcp_server.build_server", side_effect=ImportError("no mcp")),
            redirect_stderr(err),
        ):
            self.assertEqual(main(["mcp"]), 1)
        self.assertIn("mergetrain[mcp]", err.getvalue())


if __name__ == "__main__":
    unittest.main()
