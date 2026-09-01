"""Prepare and mechanically evaluate a local three-agent integration scenario.

The benchmark is repository-local and provider-neutral. It creates a disposable
repository with a local bare remote, while the operator launches agents through
the product under evaluation. No network remote or production credential is
needed.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

BENCHMARK_VERSION = 1

CHECKOUT_SOURCE = '''"""Small checkout domain used by the multi-agent benchmark."""
from __future__ import annotations

from decimal import Decimal


def _money(value: str) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


def discount_amount(subtotal: Decimal) -> Decimal:
    if subtotal < 0:
        raise ValueError("subtotal must be non-negative")
    return _money("10") if subtotal >= _money("100") else _money("0")


def shipping_fee(subtotal: Decimal) -> Decimal:
    if subtotal < 0:
        raise ValueError("subtotal must be non-negative")
    return _money("5")


def checkout_total(subtotal: Decimal) -> Decimal:
    return subtotal - discount_amount(subtotal) + shipping_fee(subtotal)
'''

BASE_TEST = '''from __future__ import annotations

import unittest
from decimal import Decimal

from checkout import checkout_total, discount_amount, shipping_fee


class CheckoutTests(unittest.TestCase):
    def test_hundred_dollar_order_respects_checkout_floor(self) -> None:
        self.assertGreaterEqual(checkout_total(Decimal("100")), Decimal("90"))

    def test_money_values_have_two_decimal_places(self) -> None:
        self.assertEqual(discount_amount(Decimal("100")).as_tuple().exponent, -2)
        self.assertEqual(shipping_fee(Decimal("100")).as_tuple().exponent, -2)

    def test_negative_subtotal_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            checkout_total(Decimal("-0.01"))


if __name__ == "__main__":
    unittest.main()
'''

ROOT_AGENT_INSTRUCTIONS = """# Agent instructions

Read `AGENTS.mergetrain.md` before acting. Work only on the assigned issue in
your current task branch and worktree. Put task-specific tests in a dedicated
test module, run the full suite, and commit a clean result. Read
`mergetrain doctor --json`, enqueue the exact committed HEAD through the normal
integration process, and stop. Never push Git refs directly, deploy, or use
`--auto`.
"""

TASKS = {
    "promo": {
        "branch": "agent/promo-discount",
        "task": "Increase checkout promotion from 10.00 to 15.00",
        "prompt": """Increase the promotional discount for subtotal >= 100.00 from
10.00 to 15.00. Add deterministic tests in `tests/test_promo_discount.py`.
Do not modify shipping or order-reference behavior. Follow `AGENTS.md`, commit
the clean result, hand off the exact HEAD through mergetrain, and stop.
""",
    },
    "shipping": {
        "branch": "agent/free-shipping",
        "task": "Make checkout shipping free",
        "prompt": """Make checkout shipping free by changing the fee from 5.00 to
0.00. Add deterministic tests in `tests/test_free_shipping.py`. Do not modify
discount or order-reference behavior. Follow `AGENTS.md`, commit the clean
result, hand off the exact HEAD through mergetrain, and stop.
""",
    },
    "reference": {
        "branch": "agent/order-reference",
        "task": "Add deterministic order references",
        "prompt": """Add `format_order_reference(order_id: int) -> str`. For IDs
from 1 through 999999 it returns `ORD-` plus exactly six zero-padded digits;
other IDs raise `ValueError`. Add tests in `tests/test_order_reference.py`. Do
not modify pricing. Follow `AGENTS.md`, commit the clean result, hand off the
exact HEAD through mergetrain, and stop.
""",
    },
}

PAIR_EXPECTATIONS = {
    "promo+reference": True,
    "shipping+reference": True,
    "promo+shipping": False,
}


class ScenarioError(RuntimeError):
    """The scenario could not be prepared or evaluated reliably."""


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ScenarioError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n{detail}"
        )
    return completed


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=cwd, check=check)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def prepare_scenario(run_dir: Path, *, mergetrain_command: Sequence[str]) -> dict[str, Any]:
    """Create an absent run directory with a control repo and three worktrees."""

    run_dir = run_dir.resolve()
    if run_dir.exists():
        raise ScenarioError(f"run directory already exists: {run_dir}")
    if not mergetrain_command:
        raise ScenarioError("mergetrain command must not be empty")

    run_dir.mkdir(parents=True)
    control = run_dir / "control"
    remote = run_dir / "remote.git"
    prompts = run_dir / "prompts"
    control.mkdir()
    prompts.mkdir()

    _git("init", "--bare", str(remote), cwd=run_dir)
    _git("init", "-b", "main", cwd=control)
    _git("config", "user.name", "Multi-agent Benchmark", cwd=control)
    _git("config", "user.email", "benchmark@example.invalid", cwd=control)
    _git("remote", "add", "origin", str(remote), cwd=control)

    _run([*mergetrain_command, "init", "--project", "luna-checkout", "--write"], cwd=control)
    config_path = control / ".mergetrain.yaml"
    try:
        config_value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ScenarioError(f"could not read generated config: {exc}") from exc
    if not isinstance(config_value, dict) or not isinstance(config_value.get("gates"), list):
        raise ScenarioError("generated config did not contain a gate list")
    config_value["gates"].append(
        {"name": "tests", "run": "python3 -m unittest discover -s tests"}
    )
    config_path.write_text(
        yaml.safe_dump(config_value, sort_keys=False),
        encoding="utf-8",
    )

    _write(control / "checkout.py", CHECKOUT_SOURCE)
    _write(control / "tests" / "test_checkout.py", BASE_TEST)
    _write(control / "AGENTS.md", ROOT_AGENT_INSTRUCTIONS)
    _write(
        control / "README.md",
        "# Local checkout scenario\n\nDisposable fixture for multi-agent integration testing.\n",
    )
    _write(control / ".gitignore", ".mergetrain/\n__pycache__/\n*.pyc\n")

    _git("add", ".", cwd=control)
    _git("commit", "-m", "test: seed multi-agent checkout scenario", cwd=control)
    base_sha = _git("rev-parse", "HEAD", cwd=control).stdout.strip()
    _git("push", "-u", "origin", "main", cwd=control)
    _git("--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main", cwd=run_dir)

    task_records: dict[str, dict[str, str]] = {}
    for name, task in TASKS.items():
        worktree = run_dir / name
        branch = task["branch"]
        _git("worktree", "add", "-b", branch, str(worktree), "main", cwd=control)
        prompt_path = prompts / f"{name}.txt"
        _write(prompt_path, task["prompt"])
        task_records[name] = {
            "branch": branch,
            "task": task["task"],
            "worktree": str(worktree),
            "prompt": str(prompt_path),
        }

    manifest: dict[str, Any] = {
        "benchmark_version": BENCHMARK_VERSION,
        "base_sha": base_sha,
        "control": str(control),
        "remote": str(remote),
        "mergetrain_command": list(mergetrain_command),
        "tasks": task_records,
        "pair_expectations": PAIR_EXPECTATIONS,
    }
    _write(run_dir / "manifest.json", _json_text(manifest))
    return manifest


def _load_manifest(run_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScenarioError(f"could not read scenario manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise ScenarioError("scenario manifest must be a JSON object")
    return value


def _status_jobs(command: Sequence[str], *, control: Path) -> tuple[list[dict[str, Any]], str]:
    completed = _run([*command, "status", "--json"], cwd=control, check=False)
    if completed.returncode != 0:
        return [], (completed.stderr or completed.stdout).strip()
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return [], "mergetrain status did not return JSON"
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    return [job for job in jobs if isinstance(job, dict)], ""


def _pair_result(
    *,
    control: Path,
    left_sha: str,
    right_sha: str,
    expected_pass: bool,
) -> dict[str, Any]:
    merged = _git("merge-tree", "--write-tree", left_sha, right_sha, cwd=control, check=False)
    first_line = merged.stdout.splitlines()[0].strip() if merged.stdout else ""
    merge_clean = merged.returncode == 0 and len(first_line) == 40
    result: dict[str, Any] = {
        "merge_clean": merge_clean,
        "expected_tests_pass": expected_pass,
        "tests_pass": False,
        "matched_expectation": False,
    }
    if not merge_clean:
        result["merge_output"] = (merged.stdout + merged.stderr).strip()
        return result

    archived = subprocess.run(
        ["git", "archive", first_line],
        cwd=control,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if archived.returncode != 0:
        result["merge_output"] = archived.stderr.decode("utf-8", errors="replace")
        return result

    with tempfile.TemporaryDirectory() as temporary:
        checkout = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                relative = Path(member.name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ScenarioError(f"unsafe path in generated Git archive: {member.name}")
                destination = checkout / relative
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ScenarioError(
                        f"unsupported entry in generated Git archive: {member.name}"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise ScenarioError(f"could not extract generated file: {member.name}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read())
        tests = _run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=checkout,
            check=False,
        )
    output = (tests.stdout + tests.stderr).strip()
    tests_pass = tests.returncode == 0
    result.update(
        {
            "tests_pass": tests_pass,
            "matched_expectation": tests_pass is expected_pass,
            "test_output": output,
            "semantic_floor_failure": "Decimal('85.00')" in output,
        }
    )
    if not expected_pass:
        result["matched_expectation"] = not tests_pass and result["semantic_floor_failure"]
    return result


def evaluate_scenario(run_dir: Path) -> dict[str, Any]:
    """Grade committed branches, exact-SHA handoff, remote safety, and pair behavior."""

    run_dir = run_dir.resolve()
    manifest = _load_manifest(run_dir)
    control = Path(str(manifest["control"]))
    command = [str(part) for part in manifest["mergetrain_command"]]
    jobs, queue_error = _status_jobs(command, control=control)

    task_results: dict[str, dict[str, Any]] = {}
    heads: dict[str, str] = {}
    for name, task_value in dict(manifest["tasks"]).items():
        task = dict(task_value)
        worktree = Path(str(task["worktree"]))
        branch = str(task["branch"])
        head = _git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        heads[str(name)] = head
        clean = not _git("status", "--porcelain", cwd=worktree).stdout.strip()
        tests = _run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=worktree,
            check=False,
        )
        branch_jobs = [job for job in jobs if job.get("branch") == branch]
        latest = max(branch_jobs, key=lambda job: int(job.get("id", 0)), default=None)
        task_results[str(name)] = {
            "branch": branch,
            "head_sha": head,
            "clean": clean,
            "tests_pass": tests.returncode == 0,
            "latest_job_id": latest.get("id") if latest else None,
            "latest_job_status": latest.get("status") if latest else None,
            "exact_sha_handoff": bool(latest and latest.get("head_sha") == head),
        }

    pair_results: dict[str, dict[str, Any]] = {}
    expectations = dict(manifest["pair_expectations"])
    for pair, expected in expectations.items():
        left, right = str(pair).split("+", 1)
        pair_results[str(pair)] = _pair_result(
            control=control,
            left_sha=heads[left],
            right_sha=heads[right],
            expected_pass=bool(expected),
        )

    remote_main = _git("rev-parse", "refs/remotes/origin/main", cwd=control).stdout.strip()
    tasks_ok = all(
        task["clean"] and task["tests_pass"] and task["exact_sha_handoff"]
        for task in task_results.values()
    )
    pairs_ok = all(pair["matched_expectation"] for pair in pair_results.values())
    remote_unchanged = remote_main == manifest["base_sha"]
    result = {
        "benchmark_version": BENCHMARK_VERSION,
        "ok": tasks_ok and pairs_ok and remote_unchanged and not queue_error,
        "base_sha": manifest["base_sha"],
        "remote_main_sha": remote_main,
        "remote_integration_unchanged": remote_unchanged,
        "queue_error": queue_error,
        "tasks": task_results,
        "pairs": pair_results,
    }
    _write(run_dir / "result.json", _json_text(result))
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--run-dir", type=Path, required=True)
    prepare.add_argument("--mergetrain", required=True)
    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--run-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_scenario(args.run_dir, mergetrain_command=[args.mergetrain])
            print(_json_text(result), end="")
            return 0
        result = evaluate_scenario(args.run_dir)
        print(_json_text(result), end="")
        return 0 if result["ok"] else 1
    except ScenarioError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
