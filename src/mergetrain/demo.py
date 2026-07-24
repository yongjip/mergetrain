"""Self-contained, local-only mergetrain walkthrough.

The demo deliberately drives the public CLI in child processes instead of
calling runner internals.  That keeps the walkthrough honest: the commands and
JSON shown to a new user are the same surfaces an agent or operator uses.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DemoFailure(RuntimeError):
    """A safe, user-facing demo failure."""


@dataclass(slots=True)
class DemoSandbox:
    root: Path
    marker_token: str

    @classmethod
    def create(cls, requested: str | None) -> DemoSandbox:
        if requested:
            requested_path = Path(requested).expanduser()
            if requested_path.is_symlink():
                raise DemoFailure("--dir must not be a symbolic link")
            root = requested_path.resolve()
            if root.exists():
                if not root.is_dir():
                    raise DemoFailure(f"--dir is not a directory: {root}")
                if any(root.iterdir()):
                    raise DemoFailure(f"--dir must not exist or must be empty: {root}")
            else:
                root.mkdir(parents=True)
        else:
            root = Path(tempfile.mkdtemp(prefix="mergetrain-demo-")).resolve()

        if root == Path(root.anchor) or root == Path.home().resolve():
            raise DemoFailure(f"refusing unsafe demo directory: {root}")
        token = uuid.uuid4().hex
        (root / ".mergetrain-demo-marker").write_text(token, encoding="utf-8")
        return cls(root=root, marker_token=token)

    def cleanup(self) -> None:
        marker = self.root / ".mergetrain-demo-marker"
        try:
            verified = (
                not self.root.is_symlink()
                and marker.is_file()
                and marker.read_text(encoding="utf-8") == self.marker_token
            )
        except OSError as exc:
            raise DemoFailure(
                f"could not verify demo directory before cleanup: {self.root}"
            ) from exc
        if not verified:
            raise DemoFailure(
                f"refusing to clean unverified demo directory: {self.root}"
            )

        def clear_readonly(func, path, _exc_info):
            os.chmod(path, 0o700)
            func(path)

        shutil.rmtree(self.root, onerror=clear_readonly)


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    def json(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.stdout)
        except json.JSONDecodeError as exc:
            raise DemoFailure("demo command did not return valid JSON") from exc
        if not isinstance(payload, dict):
            raise DemoFailure("demo command returned a non-object JSON payload")
        return payload


class DemoWalkthrough:
    TOTAL_STEPS = 9

    def __init__(
        self,
        sandbox: DemoSandbox,
        *,
        pause: bool,
        delay: float,
        brief: bool = False,
    ):
        self.sandbox = sandbox
        self.pause = pause
        self.delay = delay
        self.brief = brief
        self.repo = sandbox.root / "repo"
        self.remote = sandbox.root / "remote.git"
        self.agent_root = sandbox.root / "agents"
        self.git_config = sandbox.root / "gitconfig"
        self.env = self._isolated_environment()
        self._step_number = 0

    def _isolated_environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_CONFIG_GLOBAL"] = str(self.git_config)
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"

        # Tests commonly launch from a source checkout with PYTHONPATH=src.
        # Child commands run inside the sandbox, so make those entries absolute
        # before changing cwd. Installed users do not need this adjustment.
        entries = [
            str(Path(entry).resolve())
            for entry in sys.path
            if entry and Path(entry).is_dir()
        ]
        if entries:
            env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(entries))
        return env

    def _step(self, title: str, narration: str) -> None:
        if self._step_number:
            if self.pause:
                input("\nPress Enter for the next step...")
                if self.brief:
                    print("\033[2J\033[H", end="", flush=True)
            elif self.delay:
                time.sleep(self.delay)
        self._step_number += 1
        print(f"\n=== {self._step_number}/{self.TOTAL_STEPS} {title} ===", flush=True)
        print(narration, flush=True)

    def _run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        display: Iterable[str] | None = None,
        expected: set[int] | None = None,
        show: bool = True,
    ) -> CommandResult:
        expected = expected or {0}
        shown = list(display if display is not None else argv)
        if show:
            command = self._display_command(shown)
            print(f"$ {command}", flush=True)
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        if show and completed.stdout:
            output = self._brief_output(completed.stdout) if self.brief else completed.stdout
            if output:
                print(output, end="" if output.endswith("\n") else "\n")
        if show and completed.stderr:
            print(
                completed.stderr,
                end="" if completed.stderr.endswith("\n") else "\n",
                file=sys.stderr,
            )
        result = CommandResult(completed.returncode, completed.stdout, completed.stderr)
        if completed.returncode not in expected:
            command = shlex.join(shown)
            raise DemoFailure(
                f"command exited {completed.returncode}, expected {sorted(expected)}: {command}"
            )
        return result

    def _display_command(self, shown: list[str]) -> str:
        if not self.brief:
            return shlex.join(shown)
        if shown[:2] == ["mergetrain", "--repo"]:
            shown = [shown[0], *shown[3:]]
        if shown[:2] == ["mergetrain", "enqueue"] and "--branch" in shown:
            branch = shown[shown.index("--branch") + 1]
            return f"mergetrain enqueue --branch {shlex.quote(branch)} …"
        if shown[:3] == ["mergetrain", "run-batch", "--deploy"]:
            return "mergetrain run-batch --deploy --train-id <validated-train>"
        if shown[:2] == ["git", "--git-dir"]:
            return "git log --oneline main"
        return shlex.join(shown).replace(str(self.sandbox.root), "$DEMO")

    @staticmethod
    def _brief_output(output: str) -> str:
        prefixes = (
            "#",
            "dismissed job ",
            "health:",
            "lock:",
            "next action:",
            "queued job ",
            "result:",
        )
        lines: list[str] = []
        for line in output.splitlines():
            if line.startswith("#"):
                matched = re.match(r"^(#\d+) ([^:]+): ([^ ]+)", line)
                lines.append(
                    f"{matched.group(1)} {matched.group(2)}: {matched.group(3)}"
                    if matched
                    else line
                )
            elif line.startswith(prefixes):
                lines.append(line)
            elif re.match(r"^[0-9a-f]{7,40} ", line):
                lines.append(re.sub(r"'[0-9a-f]{40}'", "'<agent-commit>'", line))
        return "\n".join(lines) + ("\n" if lines else "")

    def _git(self, *args: str, cwd: Path | None = None, show: bool = False) -> CommandResult:
        return self._run(
            ["git", *args],
            cwd=cwd or self.sandbox.root,
            display=["git", *args],
            show=show,
        )

    def _cli(self, *args: str, expected: set[int] | None = None) -> CommandResult:
        argv = [sys.executable, "-m", "mergetrain", "--repo", str(self.repo), *args]
        shown = ["mergetrain", "--repo", str(self.repo), *args]
        return self._run(argv, cwd=self.repo, display=shown, expected=expected)

    def _cli_json(self, *args: str) -> dict[str, Any]:
        argv = [sys.executable, "-m", "mergetrain", "--repo", str(self.repo), *args]
        return self._run(argv, cwd=self.repo, show=False).json()

    def _show_cli_json(self, args: tuple[str, ...], payload: dict[str, Any]) -> None:
        shown = (
            ["mergetrain", *args]
            if self.brief
            else ["mergetrain", "--repo", str(self.repo), *args]
        )
        command = self._display_command(shown)
        print(f"$ {command}", flush=True)
        if self.brief and "git" in payload:
            git_state = payload["git"]
            print(
                "ready: "
                f"health={str(payload.get('health')).lower()} "
                f"clean={str(git_state.get('worktree_clean')).lower()} "
                f"next={payload.get('next_action')}"
            )
            return
        if self.brief and "job" in payload:
            job = payload["job"]
            outcome = payload.get("outcome", {})
            print(
                f"job: #{job.get('id')} {job.get('status')} · "
                f"{job.get('branch')}"
            )
            if job.get("conflict_with"):
                print(f"conflict_with: #{job.get('conflict_with')}")
            elif outcome.get("category") == "merge_conflict":
                print("blocked_reason: Git conflict with current train")
            print(f"outcome: {outcome.get('category')}")
            return
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))

    def _require_git(self) -> None:
        if shutil.which("git") is None:
            raise DemoFailure("git is required")
        version = self._run(
            ["git", "--version"],
            cwd=self.sandbox.root,
            show=False,
        ).stdout
        match = re.search(r"(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (2, 32):
            installed = version.strip() or "unknown"
            raise DemoFailure(
                f"git >= 2.32 is required for config isolation (have {installed})"
            )

    def _bootstrap(self) -> None:
        self._require_git()
        for key, value in (
            ("init.defaultBranch", "main"),
            ("user.email", "demo@example.invalid"),
            ("user.name", "Mergetrain Demo"),
            ("commit.gpgsign", "false"),
            ("protocol.file.allow", "always"),
        ):
            self._git("config", "--file", str(self.git_config), key, value)
        self._git("init", "--bare", str(self.remote))
        self._git("clone", str(self.remote), str(self.repo))
        (self.repo / "app").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "app" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "app" / "config.py").write_text(
            "DEFAULT_TIMEOUT = 30\n", encoding="utf-8"
        )
        (self.repo / "tests" / "test_config.py").write_text(
            "import unittest\n\n"
            "from app.config import DEFAULT_TIMEOUT\n\n\n"
            "class ConfigTests(unittest.TestCase):\n"
            "    def test_default_timeout(self):\n"
            "        self.assertEqual(DEFAULT_TIMEOUT, 30)\n",
            encoding="utf-8",
        )

    def _write_demo_config(self) -> None:
        python = shlex.quote(sys.executable)
        remote = shlex.quote(str(self.remote))
        config = f"""version: 1

project:
  name: demo

state:
  db: .mergetrain/queue.sqlite
  logs: .mergetrain/logs
  worktree_root: .mergetrain/worktrees

git:
  remote: origin
  integration_branch: main
  push_refs:
    - main

queue:
  lock_ttl_minutes: 5
  daemon_interval_seconds: 1
  heartbeat_interval_seconds: 1
  command_timeout_seconds: 60

agent:
  require_clean_worktree_before_enqueue: true
  require_explicit_auto_approval: true
  prefer_json_status: true

gates:
  - name: tests
    run: {python} -m unittest discover -s tests

deploy:
  verify:
    - name: bare-remote-main
      run: git --git-dir={remote} log -1 --format=%H main
  reuse:
    enabled: false
    max_age_minutes: 60
    on_mismatch: rerun
    fingerprints: []
"""
        (self.repo / ".mergetrain.yaml").write_text(config, encoding="utf-8")
        (self.repo / ".gitignore").write_text(
            ".mergetrain/\n__pycache__/\n*.py[cod]\n", encoding="utf-8"
        )

    def _commit_seed(self) -> None:
        self._write_demo_config()
        self._git("add", ".", cwd=self.repo)
        self._git("commit", "-m", "seed demo project", cwd=self.repo)
        self._git("branch", "-M", "main", cwd=self.repo)
        self._git("push", "-u", "origin", "main", cwd=self.repo)

    def _make_agent_branch(self, branch: str, files: dict[str, str]) -> Path:
        name = branch.removeprefix("agent/")
        worktree = self.agent_root / name
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "-b", branch, str(worktree), "main", cwd=self.repo)
        for relative, content in files.items():
            path = worktree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self._git("add", ".", cwd=worktree)
        self._git("commit", "-m", branch, cwd=worktree)
        return worktree

    def _make_agent_branches(self) -> list[tuple[str, str, Path]]:
        branches = [
            (
                "agent/faster-timeout",
                "reduce the default timeout",
                {
                    "app/config.py": "DEFAULT_TIMEOUT = 10\n",
                    "tests/test_config.py": (
                        "import unittest\n\n"
                        "from app.config import DEFAULT_TIMEOUT\n\n\n"
                        "class ConfigTests(unittest.TestCase):\n"
                        "    def test_default_timeout(self):\n"
                        "        self.assertEqual(DEFAULT_TIMEOUT, 10)\n"
                    ),
                },
            ),
            (
                "agent/longer-timeout",
                "raise the timeout for slow jobs",
                {
                    "app/config.py": "DEFAULT_TIMEOUT = 60\n",
                    "tests/test_config.py": (
                        "import unittest\n\n"
                        "from app.config import DEFAULT_TIMEOUT\n"
                        "\n\n"
                        "class ConfigTests(unittest.TestCase):\n"
                        "    def test_default_timeout(self):\n"
                        "        self.assertEqual(DEFAULT_TIMEOUT, 60)\n"
                    ),
                },
            ),
            (
                "agent/add-retries",
                "add retry policy",
                {
                    "app/retries.py": "MAX_RETRIES = 3\n",
                    "tests/test_retries.py": (
                        "import unittest\n\n"
                        "from app.retries import MAX_RETRIES\n\n\n"
                        "class RetryTests(unittest.TestCase):\n"
                        "    def test_retry_limit(self):\n"
                        "        self.assertEqual(MAX_RETRIES, 3)\n"
                    ),
                },
            ),
            (
                "agent/request-logging",
                "add request logging",
                {
                    "app/request_logging.py": "LOG_REQUESTS = True\n",
                    "tests/test_request_logging.py": (
                        "import unittest\n\n"
                        "from app.request_logging import LOG_REQUESTS\n\n\n"
                        "class LoggingTests(unittest.TestCase):\n"
                        "    def test_logging_enabled(self):\n"
                        "        self.assertTrue(LOG_REQUESTS)\n"
                    ),
                },
            ),
        ]
        return [
            (branch, task, self._make_agent_branch(branch, files))
            for branch, task, files in branches
        ]

    @staticmethod
    def _jobs_by_branch(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise DemoFailure("runner payload is missing jobs")
        return {
            str(job.get("branch")): job
            for job in jobs
            if isinstance(job, dict) and job.get("branch")
        }

    def run(self) -> None:
        self._bootstrap()
        self._step(
            "Bootstrap a disposable repository",
            "The remote, Git identity, config, queue, and worktrees all live inside this sandbox.",
        )
        self._cli("init", "--project", "demo", "--write")
        self._commit_seed()
        branches = self._make_agent_branches()
        print(f"Sandbox: {'$DEMO' if self.brief else self.sandbox.root}")
        print("Created four clean agent worktrees; no network or user Git config was used.")

        self._step(
            "Check readiness",
            "doctor returns a machine-readable next action before any branch is enqueued.",
        )
        doctor = self._cli_json("doctor", "--json")
        if doctor.get("next_action") != "enqueue_clean_branch":
            raise DemoFailure("doctor did not report enqueue_clean_branch")
        git_state = doctor.get("git", {})
        self._show_cli_json(
            ("doctor", "--json"),
            {
                "health": doctor.get("health"),
                "git": {
                    "integration_ref": git_state.get("integration_ref"),
                    "worktree_clean": git_state.get("worktree_clean"),
                },
                "lock": doctor.get("lock"),
                "next_action": doctor.get("next_action"),
            },
        )

        self._step(
            "Enqueue four agent branches",
            "Each branch is committed, clean, and SHA-pinned at enqueue time.",
        )
        job_ids: dict[str, int] = {}
        for branch, task, worktree in branches:
            result = self._cli(
                "enqueue",
                "--task",
                task,
                "--branch",
                branch,
                "--worktree",
                str(worktree),
                "--capture-sha",
            )
            matched = re.search(r"queued job (\d+):", result.stdout)
            if not matched:
                raise DemoFailure(f"enqueue did not return a job ID for {branch}")
            job_ids[branch] = int(matched.group(1))

        self._step(
            "Read the queue",
            "The queue is FIFO and the runner lock is still free.",
        )
        self._cli("status", "--limit", "10")

        self._step(
            "Validate the combined train",
            "The runner merges requests in FIFO order. A later Git conflict is "
            "skipped without discarding requests that already merged cleanly.",
        )
        self._cli("run-batch", "--validate-only", expected={1})
        validation = self._cli_json("status", "--json", "--limit", "10")
        jobs = self._jobs_by_branch(validation)
        conflicted = jobs.get("agent/longer-timeout", {})
        if conflicted.get("status") != "blocked":
            raise DemoFailure("the second FIFO request was not blocked on its Git conflict")
        if str(conflicted.get("conflict_with") or ""):
            raise DemoFailure("a Git merge conflict must not be labeled as a semantic pair")
        if "conflict" not in str(conflicted.get("note", "")).lower():
            raise DemoFailure("the blocked request is missing its Git conflict reason")
        survivor_branches = (
            "agent/faster-timeout",
            "agent/add-retries",
            "agent/request-logging",
        )
        survivors = [jobs.get(branch, {}) for branch in survivor_branches]
        if any(job.get("status") != "validated" for job in survivors):
            raise DemoFailure("the compatible FIFO survivors were not validated")
        train_ids = {str(job.get("train_id", "")) for job in survivors}
        if len(train_ids) != 1 or not next(iter(train_ids)):
            raise DemoFailure("validated survivors do not share one train identity")
        train_id = next(iter(train_ids))
        print(
            "FIFO result: "
            f"#{job_ids['agent/faster-timeout']} merged; "
            f"#{job_ids['agent/longer-timeout']} hit a Git conflict and was skipped; "
            f"#{job_ids['agent/add-retries']} and "
            f"#{job_ids['agent/request-logging']} continued."
        )
        print(
            "result: partial — exit 1 means inspect the graded result; "
            "three compatible requests were validated together."
        )

        self._step(
            "Inspect the blocked request",
            "The second request keeps its concrete Git conflict reason and recovery "
            "guidance in structured JSON.",
        )
        inspect_args = (
            "inspect",
            str(job_ids["agent/longer-timeout"]),
            "--event-limit",
            "3",
            "--json",
        )
        inspected = self._cli_json(*inspect_args)
        inspected_job = inspected.get("job", {})
        if inspected_job.get("status") != "blocked":
            raise DemoFailure("inspect did not preserve the blocked status")
        if str(inspected_job.get("conflict_with") or ""):
            raise DemoFailure("inspect mislabeled the Git conflict as a semantic pair")
        if "conflict" not in str(inspected_job.get("note", "")).lower():
            raise DemoFailure("inspect did not preserve the Git conflict reason")
        inspected_outcome = inspected.get("outcome", {})
        self._show_cli_json(
            inspect_args,
            {
                "job": {
                    "branch": inspected_job.get("branch"),
                    "conflict_with": inspected_job.get("conflict_with"),
                    "id": inspected_job.get("id"),
                    "note": inspected_job.get("note"),
                    "status": inspected_job.get("status"),
                },
                "ok": inspected.get("ok"),
                "outcome": {
                    "category": inspected_outcome.get("category"),
                    "message": inspected_outcome.get("message"),
                },
            },
        )

        self._step(
            "Dismiss the blocked request",
            "Blocked requests never self-clear. Dismissal keeps the already "
            "validated three-request train ready for explicit approval.",
        )
        self._cli(
            "dismiss",
            "--all",
            "--note",
            "demo: Git conflict acknowledged; rebase and enqueue a fresh commit",
        )
        self._cli("doctor")
        after_dismiss = self._cli_json("doctor", "--json")
        if after_dismiss.get("next_action") != "deploy_validated_train_when_approved":
            raise DemoFailure("doctor did not reveal the validated train")

        self._step(
            "Deploy the exact validated train",
            "Explicit approval names one train ID; the runner re-gates and pushes atomically.",
        )
        self._cli(
            "run-batch",
            "--deploy",
            "--train-id",
            train_id,
        )
        deployed = self._cli_json("status", "--json", "--limit", "10")
        deployed_jobs = self._jobs_by_branch(deployed)
        if any(
            deployed_jobs.get(branch, {}).get("status") != "deployed"
            for branch in survivor_branches
        ):
            raise DemoFailure("validated survivor train did not deploy successfully")
        print("result: success — the atomic local-remote push and verify hook both completed.")

        self._step(
            "Prove what landed",
            "Final state and the bare remote show exactly the three compatible "
            "requests, in FIFO order, on main.",
        )
        self._cli("status", "--limit", "10")
        final_status = self._cli_json("status", "--json", "--limit", "10")
        if final_status.get("next_action") != "enqueue_clean_branch":
            raise DemoFailure("final queue did not return to enqueue_clean_branch")
        self._git(
            "--git-dir",
            str(self.remote),
            "log",
            "--oneline",
            "--decorate",
            "--max-count=8",
            "main",
            show=True,
        )

    def hints(self) -> list[str]:
        return [
            f"mergetrain --repo {shlex.quote(str(self.repo))} status --json",
            f"mergetrain --repo {shlex.quote(str(self.repo))} dashboard --preview",
        ]


def _step_delay() -> float:
    raw = os.environ.get("MERGETRAIN_DEMO_STEP_DELAY", "0").strip() or "0"
    try:
        delay = float(raw)
    except ValueError as exc:
        raise DemoFailure("MERGETRAIN_DEMO_STEP_DELAY must be a number") from exc
    if delay < 0 or delay > 60:
        raise DemoFailure("MERGETRAIN_DEMO_STEP_DELAY must be between 0 and 60 seconds")
    return delay


def run_demo(
    *,
    directory: str | None = None,
    keep: bool = False,
    pause: bool = False,
    brief: bool = False,
) -> int:
    """Run the walkthrough, preserving its sandbox on every failure."""

    try:
        delay = _step_delay()
        sandbox = DemoSandbox.create(directory)
    except DemoFailure as exc:
        print(f"mergetrain demo: {exc}", file=sys.stderr)
        return 1

    walkthrough = DemoWalkthrough(sandbox, pause=pause, delay=delay, brief=brief)
    try:
        walkthrough.run()
    except KeyboardInterrupt:
        print(f"\nDemo interrupted; sandbox kept at: {sandbox.root}", file=sys.stderr)
        for hint in walkthrough.hints():
            print(f"  {hint}", file=sys.stderr)
        raise
    except Exception as exc:
        print(f"\nDemo failed; sandbox kept at: {sandbox.root}", file=sys.stderr)
        print(f"Reason: {exc}", file=sys.stderr)
        for hint in walkthrough.hints():
            print(f"  {hint}", file=sys.stderr)
        return 1

    print("\nDemo complete: FIFO Git conflict skipped; survivor train deployed atomically.")
    if keep:
        print(f"Sandbox kept at: {sandbox.root}")
        for hint in walkthrough.hints():
            print(f"  {hint}")
        return 0
    try:
        sandbox.cleanup()
    except DemoFailure as exc:
        print(f"Demo succeeded, but cleanup was refused: {exc}", file=sys.stderr)
        print(f"Sandbox kept at: {sandbox.root}", file=sys.stderr)
        return 1
    print(f"Sandbox removed: {'$DEMO' if brief else sandbox.root}")
    if not brief:
        print("Run with --keep to inspect the queue or open the read-only dashboard afterward.")
    return 0
