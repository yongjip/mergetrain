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
from contextlib import redirect_stderr, suppress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from mergetrain.cli import main
from mergetrain.mcp_server import (
    MergetrainTools,
    _elicit_deploy_accept,
    _replace_local_path_root,
    _stop_cli_process,
)

HAS_MCP = importlib.util.find_spec("mcp") is not None


def completed(stdout: str, returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=["mergetrain"], returncode=returncode, stdout=stdout, stderr=stderr
    )


DOCTOR = {
    "ok": True,
    "contract_version": 1,
    "health": True,
    "next_action": "deploy_validated_train_when_approved",
    "config": {
        "git": {"integration_ref": "origin/main", "push_refs": ["main"]},
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
    "deploy_eligible": True,
}

STATUS = {
    "ok": True,
    "contract_version": 1,
    "next_action": "deploy_validated_train_when_approved",
    "validated_trains": [TRAIN],
    "jobs": [
        {"id": 7, "branch": "agent/one", "status": "validated", "note": ""},
        {"id": 9, "branch": "agent/three", "status": "blocked", "note": "gate tests failed"},
    ],
}


class FakeCapabilities:
    def __init__(self, *, elicitation: bool) -> None:
        # The real ClientCapabilities carries an ElicitationCapability or None.
        self.elicitation = object() if elicitation else None


class FakeClientParams:
    def __init__(self, *, elicitation: bool) -> None:
        self.capabilities = FakeCapabilities(elicitation=elicitation)


class FakeSession:
    def __init__(self, *, elicitation: bool) -> None:
        self.client_params = FakeClientParams(elicitation=elicitation)


class FakeContext:
    """A client stand-in: records what it was shown and answers as told."""

    def __init__(self, *, elicitation: bool = True, action: str = "accept", confirm: bool = True):
        self.session = FakeSession(elicitation=elicitation)
        self.action = action
        self.confirm = confirm
        self.messages: list[str] = []

    async def elicit(self, message: str, schema: Any) -> Any:
        self.messages.append(message)

        class Result:
            pass

        result = Result()
        result.action = self.action  # type: ignore[attr-defined]
        result.data = schema(confirm=self.confirm) if self.action == "accept" else None  # type: ignore[attr-defined]
        return result


class ExplodingContext(FakeContext):
    async def elicit(self, message: str, schema: Any) -> Any:
        raise RuntimeError("transport closed")


class ToolSurfaceTests(unittest.TestCase):
    """The surface is a safety boundary: absent parameters are the enforcement."""

    def setUp(self) -> None:
        self.tools = MergetrainTools(repo=Path("/repo"))

    def test_no_tool_can_reach_unattended_deploy_or_destruction(self) -> None:
        forbidden = {"auto", "apply", "delete_branches", "force", "confirm", "yes"}
        for name in (
            "status",
            "doctor",
            "inspect_job",
            "history",
            "stats",
            "agent_contract",
            "gc_preview",
            "events",
            "logs",
            "validate",
            "enqueue",
            "deploy",
        ):
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
        self.assertIn("--capture-sha", args)
        self.assertNotIn("--auto", args)

    def test_gc_preview_never_passes_apply(self) -> None:
        with patch.object(MergetrainTools, "_run", return_value=completed("{}")) as run:
            asyncio.run(self.tools.gc_preview())
        args = run.call_args.args[0]
        self.assertEqual(args, ["gc", "--json"])

    def test_bounded_arguments_are_clamped(self) -> None:
        with patch.object(MergetrainTools, "_run", return_value=completed("{}")) as run:
            asyncio.run(self.tools.status(limit=10_000))
        self.assertIn("200", run.call_args.args[0])
        with patch.object(MergetrainTools, "_run", return_value=completed("")) as run:
            asyncio.run(self.tools.logs(job_id=3, tail=10_000))
        self.assertIn("500", run.call_args.args[0])


class PayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = MergetrainTools(repo=Path("/repo"))

    def test_cli_payload_is_returned_verbatim(self) -> None:
        with patch.object(MergetrainTools, "_run", return_value=completed(json.dumps(STATUS))):
            payload = asyncio.run(self.tools.status())
        self.assertEqual(payload, STATUS)

    def test_a_failure_envelope_is_passed_through_not_rewritten(self) -> None:
        envelope = {
            "ok": False,
            "error": {"code": "config_error", "message": "bad", "retryable": False},
        }
        with patch.object(
            MergetrainTools, "_run", return_value=completed(json.dumps(envelope), returncode=2)
        ):
            payload = asyncio.run(self.tools.doctor())
        self.assertEqual(payload, envelope)

    def test_unreadable_output_becomes_the_one_failure_shape(self) -> None:
        with patch.object(
            MergetrainTools, "_run", return_value=completed("not json", returncode=1, stderr="boom")
        ):
            payload = asyncio.run(self.tools.doctor())
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
            payload = asyncio.run(self.tools.doctor())
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
            payload = asyncio.run(self.tools.doctor())
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
        with patch.object(
            MergetrainTools, "_run", return_value=completed(json.dumps(envelope))
        ):
            payload = asyncio.run(self.tools.doctor())
        self.assertEqual(payload, envelope)

    def test_timeout_is_reported_not_raised(self) -> None:
        with patch.object(
            MergetrainTools,
            "_run",
            side_effect=subprocess.TimeoutExpired(cmd="mergetrain", timeout=1),
        ):
            payload = asyncio.run(self.tools.doctor())
        self.assertEqual(payload["error"]["code"], "cli_timeout")

    def test_events_returns_the_cli_frames_unchanged(self) -> None:
        stdout = (
            '{"type": "stream_start", "contract_version": 1, "after_event_id": 0}\n'
            '{"type": "event", "id": 4, "phase": "validating"}\n'
            "not json\n"
        )
        with patch.object(MergetrainTools, "_run", return_value=completed(stdout)):
            payload = asyncio.run(self.tools.events())
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
            payload = asyncio.run(self.tools.events())
        self.assertNotIn("event-secret", payload["error"]["message"])
        self.assertIn("ACCESS_TOKEN=[redacted]", payload["error"]["message"])

    def test_logs_failure_redacts_synthesized_detail(self) -> None:
        with patch.object(
            MergetrainTools,
            "_run",
            return_value=completed("", returncode=1, stderr="--api-key log-secret"),
        ):
            payload = asyncio.run(self.tools.logs(job_id=4))
        self.assertNotIn("log-secret", payload["error"]["message"])
        self.assertIn("--api-key [redacted]", payload["error"]["message"])

    def test_successful_raw_log_output_remains_unchanged(self) -> None:
        raw = "API_TOKEN=intentionally-raw\n"
        with patch.object(
            MergetrainTools, "_run", return_value=completed(raw)
        ):
            payload = asyncio.run(self.tools.logs(job_id=4))
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

    def _deploy(self, ctx: Any, *, train_id: str = "", status: dict[str, Any] | None = None) -> Any:
        payloads = [DOCTOR, status if status is not None else STATUS]
        calls: list[list[str]] = []

        async def fake_run(_self: Any, args: list[str]) -> Any:
            calls.append(args)
            if args[0] == "doctor":
                return completed(json.dumps(payloads[0]))
            if args[0] == "status":
                return completed(json.dumps(payloads[1]))
            return completed(json.dumps({"ok": True, "result": "success"}))

        with patch.object(MergetrainTools, "_run", new=fake_run):
            payload = asyncio.run(self.tools.deploy(ctx, train_id=train_id))
        return payload, calls

    @unittest.skipUnless(HAS_MCP, "the confirmation schema needs the mcp extra")
    def test_accepted_confirmation_deploys_the_named_train(self) -> None:
        ctx = FakeContext(action="accept", confirm=True)
        payload, calls = self._deploy(ctx)
        self.assertEqual(payload, {"ok": True, "result": "success"})
        self.assertIn(["run-batch", "--deploy", "--train-id", "abc123", "--json"], calls)

    @unittest.skipUnless(HAS_MCP, "the confirmation schema needs the mcp extra")
    def test_the_human_sees_the_operating_contract_summary(self) -> None:
        ctx = FakeContext()
        self._deploy(ctx)
        shown = ctx.messages[0]
        for expected in (
            "abc123",
            "#7 agent/one",
            "#8 agent/two",
            "origin/main",
            "deploy_validated_train_when_approved",
            "agent/three blocked",
        ):
            self.assertIn(expected, shown)

    def test_a_client_without_elicitation_is_refused_with_instructions(self) -> None:
        ctx = FakeContext(elicitation=False)
        payload, calls = self._deploy(ctx)
        self.assertEqual(payload["error"]["code"], "confirmation_required")
        self.assertIn("run-batch --deploy --train-id abc123", payload["command"])
        self.assertNotIn(
            ["run-batch", "--deploy", "--train-id", "abc123", "--json"],
            calls,
            "a client that cannot confirm must not deploy",
        )

    def test_a_declined_dialog_does_not_deploy(self) -> None:
        payload, calls = self._deploy(FakeContext(action="decline"))
        self.assertEqual(payload["error"]["code"], "deploy_not_confirmed")
        self.assertTrue(all(args[:2] != ["run-batch", "--deploy"] for args in calls))

    def test_a_cancelled_dialog_does_not_deploy(self) -> None:
        payload, calls = self._deploy(FakeContext(action="cancel"))
        self.assertEqual(payload["error"]["code"], "deploy_not_confirmed")
        self.assertTrue(all(args[:2] != ["run-batch", "--deploy"] for args in calls))

    @unittest.skipUnless(HAS_MCP, "the confirmation schema needs the mcp extra")
    def test_an_accept_with_the_box_unchecked_does_not_deploy(self) -> None:
        payload, calls = self._deploy(FakeContext(action="accept", confirm=False))
        self.assertEqual(payload["error"]["code"], "deploy_not_confirmed")
        self.assertIn("unchecked", payload["error"]["message"])
        self.assertTrue(all(args[:2] != ["run-batch", "--deploy"] for args in calls))

    @unittest.skipUnless(HAS_MCP, "the confirmation schema needs the mcp extra")
    def test_a_failed_dialog_is_not_consent(self) -> None:
        payload, calls = self._deploy(ExplodingContext())
        self.assertEqual(payload["error"]["code"], "deploy_not_confirmed")
        self.assertIn("transport closed", payload["error"]["message"])
        self.assertTrue(all(args[:2] != ["run-batch", "--deploy"] for args in calls))

    def test_several_pending_trains_require_an_explicit_choice(self) -> None:
        second = dict(TRAIN, train_id="def456", job_ids=[10], branches=[])
        status = dict(STATUS, validated_trains=[TRAIN, second])
        ctx = FakeContext()
        payload, calls = self._deploy(ctx, status=status)
        self.assertEqual(payload["error"]["code"], "train_id_required")
        self.assertEqual(payload["pending_train_ids"], ["abc123", "def456"])
        self.assertEqual(ctx.messages, [], "no dialog before a train is chosen")
        self.assertTrue(all(args[:2] != ["run-batch", "--deploy"] for args in calls))

    def test_an_unknown_train_id_is_refused(self) -> None:
        payload, _ = self._deploy(FakeContext(), train_id="nope")
        self.assertEqual(payload["error"]["code"], "train_not_found")

    def test_an_incomplete_train_identity_is_not_deployable(self) -> None:
        status = dict(STATUS, validated_trains=[dict(TRAIN, deploy_eligible=False)])
        payload, calls = self._deploy(FakeContext(), status=status)
        self.assertEqual(payload["error"]["code"], "no_validated_train")
        self.assertTrue(all(args[:2] != ["run-batch", "--deploy"] for args in calls))

    def test_a_broken_read_stops_the_deploy(self) -> None:
        envelope = {
            "ok": False,
            "error": {"code": "config_error", "message": "too new", "retryable": False},
        }
        payload, calls = self._deploy(FakeContext(), status=envelope)
        self.assertEqual(payload, envelope)
        self.assertTrue(all(args[:2] != ["run-batch", "--deploy"] for args in calls))

    def test_confirmation_requires_a_pydantic_accept_shape(self) -> None:
        # Guards the helper itself: anything but action=accept plus confirm=True
        # is a refusal, whatever the client sends.
        if not HAS_MCP:
            self.skipTest("pydantic ships with the mcp extra")
        for action, confirm, expected in (
            ("accept", True, True),
            ("accept", False, False),
            ("decline", True, False),
            ("cancel", True, False),
        ):
            ctx = FakeContext(action=action, confirm=confirm)
            accepted, _ = asyncio.run(_elicit_deploy_accept(ctx, "summary", "abc123"))
            self.assertEqual(accepted, expected, f"{action}/{confirm}")


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
                "mergetrain_doctor",
                "mergetrain_inspect",
                "mergetrain_history",
                "mergetrain_stats",
                "mergetrain_agent_contract",
                "mergetrain_gc_preview",
                "mergetrain_events",
                "mergetrain_logs",
                "mergetrain_validate",
                "mergetrain_enqueue",
                "mergetrain_deploy",
            },
        )
        for name in ("mergetrain_status", "mergetrain_doctor", "mergetrain_gc_preview"):
            self.assertTrue(tools[name].annotations.readOnlyHint, name)
        # validate runs gate commands and moves job status, so claiming
        # read-only would misinform the client about its side effects.
        self.assertFalse(tools["mergetrain_validate"].annotations.readOnlyHint)
        self.assertTrue(tools["mergetrain_deploy"].annotations.destructiveHint)

    def test_the_deploy_context_is_injected_not_a_model_argument(self) -> None:
        from mergetrain.mcp_server import build_server

        server = build_server(Path("/repo"))
        deploy = next(
            tool for tool in asyncio.run(server.list_tools()) if tool.name == "mergetrain_deploy"
        )
        self.assertNotIn("ctx", deploy.inputSchema.get("properties", {}))
        self.assertEqual(set(deploy.inputSchema.get("properties", {})), {"train_id"})


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
