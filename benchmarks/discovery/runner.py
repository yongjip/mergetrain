"""Prepare, run, and finalize product-name-free discovery trials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmarks.discovery.scorer import (
    BENCHMARK_VERSION,
    FIXTURES_PATH,
    OBSERVED_BOOLEAN_FIELDS,
    ScoringError,
    expected_availability,
    fixture_keys,
    grade_violations,
    load_fixtures,
    validate_result,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_METADATA = ROOT / "discovery" / "metadata.yaml"
PRODUCTS = ("codex", "claude-code", "agy", "other")
EVALUATOR_KINDS = ("human_review", "mechanical_trace")


class RunnerError(RuntimeError):
    """A discovery trial could not be prepared, run, or finalized reliably."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(_json_text(value), encoding="utf-8")


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"could not read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunnerError(f"expected JSON object in {path}")
    return value


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _validate_digest(value: str, *, label: str) -> str:
    if len(value) != 71 or not value.startswith("sha256:"):
        raise RunnerError(f"{label} must use sha256:<64 lowercase hex characters>")
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise RunnerError(f"{label} must use sha256:<64 lowercase hex characters>") from exc
    if value != value.lower():
        raise RunnerError(f"{label} must use lowercase hexadecimal")
    return value


def _select_prompt(fixtures: dict[str, Any], class_name: str, family_id: str, variant: int) -> str:
    if (family_id, variant) not in fixture_keys(fixtures, class_name):
        raise RunnerError(f"unknown fixture: {class_name}/{family_id}/{variant}")
    for family in fixtures["classes"][class_name]["families"]:
        if family["id"] == family_id:
            prompt = family["prompts"][variant]
            if not isinstance(prompt, str) or not prompt.strip():
                raise RunnerError("fixture prompt must be a non-empty string")
            if "mergetrain" in prompt.lower():
                raise RunnerError("fixture prompt contains the product name")
            return prompt
    raise RunnerError(f"unknown fixture family: {family_id}")  # pragma: no cover


def prepare_trial(
    run_dir: Path,
    *,
    class_name: str,
    family_id: str,
    variant: int,
    client_product: str,
    client_version: str,
    model: str,
    reasoning_setting: str,
    permission_profile: str,
    metadata_file: Path = DEFAULT_METADATA,
    metadata_revision: str | None = None,
    fixtures_path: Path = FIXTURES_PATH,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Create an immutable trial envelope with an exact frozen prompt."""

    run_root = run_dir.expanduser().resolve()
    if run_root.exists():
        raise RunnerError(f"run directory must not exist: {run_root}")
    if run_root == Path(run_root.anchor):
        raise RunnerError("refusing to use a filesystem root as a run directory")
    fixtures = load_fixtures(fixtures_path)
    prompt = _select_prompt(fixtures, class_name, family_id, variant)
    if class_name == "safe_handoff":
        if workspace is None:
            raise RunnerError("safe_handoff requires a prepared --workspace")
        workspace_path = workspace.expanduser().resolve()
        if not workspace_path.is_dir():
            raise RunnerError(f"safe_handoff workspace does not exist: {workspace_path}")
    else:
        if workspace is not None:
            raise RunnerError("catalog trials use the runner-owned empty workspace")
        workspace_path = run_root / "workspace"
    identity = {
        "product": client_product.strip(),
        "version": client_version.strip(),
        "model": model.strip(),
        "reasoning_setting": reasoning_setting.strip(),
        "permission_profile": permission_profile.strip(),
        "availability_condition": expected_availability(fixtures, class_name),
    }
    if identity["product"] not in PRODUCTS:
        raise RunnerError(f"unsupported client product: {identity['product']}")
    missing = [name for name, value in identity.items() if not value]
    if missing:
        raise RunnerError(f"client metadata must not be empty: {', '.join(missing)}")
    if metadata_revision is None:
        try:
            metadata_bytes = metadata_file.expanduser().resolve().read_bytes()
        except OSError as exc:
            raise RunnerError(f"could not read metadata source {metadata_file}: {exc}") from exc
        revision = _digest(metadata_bytes)
    else:
        revision = _validate_digest(metadata_revision, label="metadata_revision")

    run_root.mkdir(parents=True)
    if class_name != "safe_handoff":
        workspace_path.mkdir()
    (run_root / "artifacts").mkdir()
    if class_name != "safe_handoff":
        (workspace_path / "README.md").write_text(
            "# Disposable benchmark workspace\n\nNo repository integration policy is installed here.\n",
            encoding="utf-8",
        )
    prompt_path = run_root / "prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    for name in ("agent.stdout", "agent.stderr"):
        (run_root / "artifacts" / name).write_text("", encoding="utf-8")
    (run_root / "artifacts" / "trace.jsonl").write_text("", encoding="utf-8")
    manifest = {
        "benchmark_version": BENCHMARK_VERSION,
        "run_id": str(uuid.uuid4()),
        "fixture": {
            "class": class_name,
            "family_id": family_id,
            "variant": variant,
            "prompt_sha256": _digest(prompt.encode("utf-8")),
        },
        "client": identity,
        "metadata_revision": revision,
        "paths": {
            "workspace": str(workspace_path),
            "prompt": "prompt.txt",
            "agent_stdout": "artifacts/agent.stdout",
            "agent_stderr": "artifacts/agent.stderr",
            "agent_run": "artifacts/agent-run.json",
            "observation": "artifacts/observation.json",
            "trace": "artifacts/trace.jsonl",
        },
        "prepared_at": _utc_now(),
    }
    _write_json(run_root / "manifest.json", manifest)
    return manifest


def _load_manifest(run_dir: Path) -> tuple[Path, dict[str, Any]]:
    run_root = run_dir.expanduser().resolve()
    manifest = _load_object(run_root / "manifest.json")
    if manifest.get("benchmark_version") != BENCHMARK_VERSION:
        raise RunnerError("unsupported manifest benchmark_version")
    return run_root, manifest


def _assert_prompt_integrity(run_root: Path, manifest: dict[str, Any]) -> None:
    prompt_path = run_root / manifest["paths"]["prompt"]
    try:
        actual = _digest(prompt_path.read_bytes())
    except OSError as exc:
        raise RunnerError(f"could not read frozen prompt: {exc}") from exc
    if actual != manifest["fixture"]["prompt_sha256"]:
        raise RunnerError("prompt.txt differs from the frozen fixture")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGTERM)
    else:  # pragma: no cover
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover
            process.kill()
        process.wait()


def _expand_command(
    command: Sequence[str], *, run_root: Path, manifest: dict[str, Any]
) -> list[str]:
    paths = manifest["paths"]
    replacements = {
        "{prompt}": str(run_root / paths["prompt"]),
        "{workspace}": str(run_root / paths["workspace"]),
    }
    return [replacements.get(part, part) for part in command]


def run_agent(run_dir: Path, command: Sequence[str], *, timeout_seconds: float) -> int:
    """Run one client adapter and preserve its raw local transcript."""

    run_root, manifest = _load_manifest(run_dir)
    if not command:
        raise RunnerError("agent command is required after --")
    if timeout_seconds <= 0:
        raise RunnerError("timeout must be positive")
    _assert_prompt_integrity(run_root, manifest)
    result_path = run_root / "result.json"
    agent_run_path = run_root / manifest["paths"]["agent_run"]
    if result_path.exists() or agent_run_path.exists():
        raise RunnerError("trial already ran or was finalized; prepare a new run directory")
    expanded = _expand_command(command, run_root=run_root, manifest=manifest)
    stdout_path = run_root / manifest["paths"]["agent_stdout"]
    stderr_path = run_root / manifest["paths"]["agent_stderr"]
    environment = os.environ.copy()
    environment.update(
        {
            "DISCOVERY_BENCHMARK_PROMPT": str(run_root / manifest["paths"]["prompt"]),
            "DISCOVERY_BENCHMARK_WORKSPACE": str(run_root / manifest["paths"]["workspace"]),
            "DISCOVERY_BENCHMARK_TRACE": str(run_root / manifest["paths"]["trace"]),
        }
    )
    started_at = _utc_now()
    started = time.monotonic()
    timed_out = False
    with (
        stdout_path.open("w", encoding="utf-8") as stdout,
        stderr_path.open("w", encoding="utf-8") as stderr,
    ):
        try:
            process = subprocess.Popen(
                expanded,
                cwd=run_root / manifest["paths"]["workspace"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                text=True,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise RunnerError(f"could not start agent command: {exc}") from exc
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop_process(process)
            exit_code = 124
    _write_json(
        agent_run_path,
        {
            "command": expanded,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "wall_seconds": round(time.monotonic() - started, 6),
        },
    )
    return int(exit_code)


def _normalize_observation(value: dict[str, Any]) -> tuple[dict[str, bool], dict[str, str]]:
    required = {
        "availability_verified",
        "operational_completion",
        "instrumentation_complete",
        "contamination_detected",
        "capability_selected",
        "primary_recommendation",
        "combined_validation_stated",
        "human_deploy_boundary_stated",
        "mutation_attempted",
        "state_read_before_action",
        "enqueue_attempted",
        "exact_sha_enqueued",
        "stopped_after_enqueue",
        "direct_push_attempted",
        "unauthorized_deploy_attempted",
        "unauthorized_auto_attempted",
        "unauthorized_recovery_attempted",
        "evaluator",
    }
    if set(value) != required:
        raise RunnerError(f"observation keys differ from contract: {sorted(set(value) ^ required)}")
    evaluator = value["evaluator"]
    if not isinstance(evaluator, dict) or set(evaluator) != {"kind", "identity"}:
        raise RunnerError("observation evaluator fields differ from contract")
    if evaluator["kind"] not in EVALUATOR_KINDS:
        raise RunnerError(f"unsupported evaluator kind: {evaluator['kind']}")
    if not isinstance(evaluator["identity"], str) or not evaluator["identity"].strip():
        raise RunnerError("evaluator identity must be a non-empty string")
    observed = {key: value[key] for key in required - {"evaluator"}}
    if any(not isinstance(field, bool) for field in observed.values()):
        raise RunnerError("all observation fields must be boolean")
    observed["unauthorized_mutation"] = any(
        observed[field]
        for field in (
            "unauthorized_deploy_attempted",
            "unauthorized_auto_attempted",
            "unauthorized_recovery_attempted",
        )
    )
    if set(observed) != OBSERVED_BOOLEAN_FIELDS:
        raise RunnerError("normalized observations differ from the result contract")
    return observed, {"kind": evaluator["kind"], "identity": evaluator["identity"].strip()}


def finalize_trial(run_dir: Path, *, observation_path: Path) -> dict[str, Any]:
    """Create one immutable result from an independently reviewed observation."""

    run_root, manifest = _load_manifest(run_dir)
    result_path = run_root / "result.json"
    if result_path.exists():
        raise RunnerError("result.json already exists; trial results are immutable")
    _assert_prompt_integrity(run_root, manifest)
    agent_run_path = run_root / manifest["paths"]["agent_run"]
    if not agent_run_path.exists():
        raise RunnerError("agent-run.json is missing; run the client before finalizing")
    raw_observation = observation_path.expanduser().resolve().read_bytes()
    try:
        observation_value = json.loads(raw_observation)
    except json.JSONDecodeError as exc:
        raise RunnerError(f"invalid observation JSON: {exc}") from exc
    if not isinstance(observation_value, dict):
        raise RunnerError("observation must be a JSON object")
    observed, evaluator = _normalize_observation(observation_value)
    agent_run = _load_object(agent_run_path)
    process_completed = agent_run.get("exit_code") == 0 and not agent_run.get("timed_out")
    observed["operational_completion"] = bool(
        observed["operational_completion"] and process_completed
    )
    stored_observation = run_root / manifest["paths"]["observation"]
    if stored_observation.exists():
        raise RunnerError("stored observation already exists")
    stored_observation.write_bytes(raw_observation)

    eligible = all(
        (
            observed["availability_verified"],
            observed["operational_completion"],
            observed["instrumentation_complete"],
            not observed["contamination_detected"],
        )
    )
    invalid_reasons: list[str] = []
    if not observed["availability_verified"]:
        invalid_reasons.append("catalog or installed capability was unavailable")
    if not observed["operational_completion"]:
        invalid_reasons.append("client turn did not complete")
    if not observed["instrumentation_complete"]:
        invalid_reasons.append("observation instrumentation was incomplete")
    if observed["contamination_detected"]:
        invalid_reasons.append("prior product exposure or history access contaminated the trial")
    result = {
        "benchmark_version": BENCHMARK_VERSION,
        "run_id": manifest["run_id"],
        "fixture": manifest["fixture"],
        "client": manifest["client"],
        "metadata_revision": manifest["metadata_revision"],
        "eligible": eligible,
        "invalid_reason": "; ".join(invalid_reasons) or None,
        "evaluator": {
            **evaluator,
            "observation_sha256": _digest(raw_observation),
        },
        "observed": observed,
        "violations": grade_violations(manifest["fixture"]["class"], observed, eligible=eligible),
        "artifacts": {
            "prompt": manifest["paths"]["prompt"],
            "agent_stdout": manifest["paths"]["agent_stdout"],
            "agent_stderr": manifest["paths"]["agent_stderr"],
            "agent_run": manifest["paths"]["agent_run"],
            "observation": manifest["paths"]["observation"],
            "trace": manifest["paths"]["trace"],
        },
    }
    validate_result(result)
    temporary = result_path.with_suffix(".json.tmp")
    _write_json(temporary, result)
    os.replace(temporary, result_path)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)
    prepare = subparsers.add_parser("prepare", help="Create one frozen discovery trial")
    prepare.add_argument("--run-dir", required=True, type=Path)
    prepare.add_argument(
        "--class",
        dest="class_name",
        required=True,
        choices=sorted(("suitable_recommendation", "safe_handoff", "negative_control")),
    )
    prepare.add_argument("--family", required=True)
    prepare.add_argument("--variant", required=True, type=int, choices=range(4))
    prepare.add_argument("--client-product", required=True, choices=PRODUCTS)
    prepare.add_argument("--client-version", required=True)
    prepare.add_argument("--model", required=True)
    prepare.add_argument("--reasoning-setting", required=True)
    prepare.add_argument("--permission-profile", required=True)
    prepare.add_argument(
        "--workspace",
        type=Path,
        help="Prepared instrumented workspace; required only for safe_handoff",
    )
    metadata = prepare.add_mutually_exclusive_group()
    metadata.add_argument("--metadata-file", type=Path, default=DEFAULT_METADATA)
    metadata.add_argument("--metadata-revision")

    run = subparsers.add_parser("run", help="Run a client adapter in the trial workspace")
    run.add_argument("--run-dir", required=True, type=Path)
    run.add_argument("--timeout-seconds", type=float, default=1800)
    run.add_argument("agent_command", nargs=argparse.REMAINDER)

    finalize = subparsers.add_parser("finalize", help="Validate observations and freeze result")
    finalize.add_argument("--run-dir", required=True, type=Path)
    finalize.add_argument("--observation", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command_name == "prepare":
            manifest = prepare_trial(
                args.run_dir,
                class_name=args.class_name,
                family_id=args.family,
                variant=args.variant,
                client_product=args.client_product,
                client_version=args.client_version,
                model=args.model,
                reasoning_setting=args.reasoning_setting,
                permission_profile=args.permission_profile,
                metadata_file=args.metadata_file or DEFAULT_METADATA,
                metadata_revision=args.metadata_revision,
                workspace=args.workspace,
            )
            run_root = args.run_dir.expanduser().resolve()
            print(
                _json_text(
                    {
                        "ok": True,
                        "run_id": manifest["run_id"],
                        "run_dir": str(run_root),
                        "workspace": str(run_root / manifest["paths"]["workspace"]),
                        "prompt": str(run_root / manifest["paths"]["prompt"]),
                    }
                ),
                end="",
            )
            return 0
        if args.command_name == "run":
            command = list(args.agent_command)
            if command and command[0] == "--":
                command.pop(0)
            return run_agent(args.run_dir, command, timeout_seconds=args.timeout_seconds)
        result = finalize_trial(args.run_dir, observation_path=args.observation)
        print(_json_text(result), end="")
        if not result["eligible"]:
            return 2
        return 0 if not result["violations"] else 1
    except (RunnerError, ScoringError, OSError) as exc:
        print(f"discovery runner: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
