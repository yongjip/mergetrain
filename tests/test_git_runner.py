from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import mergetrain.atomic_push as atomic_push_module
import mergetrain.command_runner as command_runner_module

SHELL_PYTHON = sys.executable.replace("\\", "/")


def py_path(path: Path | str) -> str:
    """A filesystem path safe to embed inside a Python string literal.

    Windows paths contain backslashes, which a ``python -c "... '{path}' ..."``
    gate would read as escape sequences (``C:\\Users`` -> ``\\U...``). Forward
    slashes are valid in the literal and pathlib accepts them on every OS.
    """

    return str(path).replace("\\", "/")


def _clear_readonly(func, path, _exc):
    # Git marks loose objects and pack files read-only; Windows refuses to
    # delete a read-only file, so rmtree of a repo raises WinError 5. Clear the
    # bit and retry — the POSIX default already tolerates this.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree(path: Path | str) -> None:
    """``shutil.rmtree`` that also removes read-only files (Windows git repos)."""

    kwargs = {"onexc": _clear_readonly}
    if sys.version_info < (3, 12):
        kwargs = {"onerror": lambda f, p, _e: _clear_readonly(f, p, None)}
    shutil.rmtree(path, **kwargs)

from mergetrain.cli import main
from mergetrain.command_runner import (
    _dashboard_command,
    _shell_command,
    command_env,
    expand_command,
    run_shell,
)
from mergetrain.config import load_config
from mergetrain.deploy_plan import deploy_destination_sha
from mergetrain.errors import (
    AmbiguousPush,
    CommandFailed,
    DeployPlanChanged,
    MergeBlocked,
    PushRejected,
    redact_secrets,
)
from mergetrain.git_ops import deploy_audit_ref_name
from mergetrain.git_runner import GitRunner
from mergetrain.snapshot import next_action
from mergetrain.store import (
    cancel_job,
    claim_all_queued,
    claim_deploy_batch,
    connect,
    counts,
    enqueue_job,
    get_job,
    get_lock,
    list_run_events,
    release_runner_lock,
)


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed\n{completed.stdout}\n{completed.stderr}")
    return completed.stdout.strip()


def make_demo_repo(
    root: Path,
    *,
    gate_command: str = "",
    verify_command: str | None = None,
    reuse_enabled: bool = False,
    reuse_max_age_minutes: int = 60,
    reuse_on_mismatch: str = "rerun",
    always_rerun_on_deploy: bool = False,
    fingerprint_command: str | None = None,
    gate_paths: tuple[str, ...] = (),
) -> tuple[Path, Path]:
    """Create a remote+clone with a ``feature/a`` branch and return (repo, marker).

    The gate appends to ``marker`` once per gate run so tests can assert the train
    gate executed exactly once over the assembled batch.
    """
    repo = root / "repo"
    remote = root / "remote.git"
    git(root, "init", "--bare", str(remote))
    git(root, "clone", str(remote), str(repo))
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test User")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "app.txt")
    git(repo, "commit", "-m", "base")
    git(repo, "branch", "-M", "main")
    git(repo, "push", "-u", "origin", "main")
    git(repo, "switch", "-c", "feature/a")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-m", "a")
    git(repo, "switch", "main")
    marker = root / "gate-count.txt"
    gate_command = gate_command or (
        f"{SHELL_PYTHON} -c \"from pathlib import Path; p=Path('{py_path(marker)}'); "
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\""
    )
    verify_config = "  verify: []"
    if verify_command is not None:
        verify_config = f"""  verify:
    - name: live-check
      run: {verify_command}"""
    fingerprint_config = "    fingerprints: []"
    if fingerprint_command is not None:
        fingerprint_config = f"""    fingerprints:
      - name: toolchain
        run: {fingerprint_command}"""
    always_rerun_config = (
        "\n    always_rerun_on_deploy: true" if always_rerun_on_deploy else ""
    )
    paths_config = ""
    if gate_paths:
        rendered_paths = "".join(
            f"\n      - {json.dumps(pattern)}" for pattern in gate_paths
        )
        paths_config = f"\n    paths:{rendered_paths}"
    config_text = f"""project:
  name: demo
state:
  db: {root / 'queue.sqlite'}
  logs: {root / 'logs'}
  worktree_root: {root / 'worktrees'}
git:
  remote: origin
  integration_branch: main
  push_refs:
    - main
queue:
  lock_ttl_minutes: 1
  daemon_interval_seconds: 1
  heartbeat_interval_seconds: 1
  command_timeout_seconds: 30
gates:
  - name: marker
    run: {gate_command}{always_rerun_config}{paths_config}
deploy:
{verify_config}
  reuse:
    enabled: {str(reuse_enabled).lower()}
    max_age_minutes: {reuse_max_age_minutes}
    on_mismatch: {reuse_on_mismatch}
{fingerprint_config}
"""
    (repo / ".mergetrain.yaml").write_text(config_text, encoding="utf-8")
    return repo, marker


def enable_persistent_validation_workspace(
    repo: Path,
    *,
    cache_key: str = "test-cache-v1",
    cache_paths: tuple[str, ...] = (".cache",),
) -> None:
    config_path = repo / ".mergetrain.yaml"
    rendered_paths = "".join(f"\n      - {path}" for path in cache_paths)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "  worktree_root:",
            (
                "  validation_workspace:\n"
                "    mode: persistent\n"
                f"    cache_key: {cache_key}\n"
                f"    cache_paths:{rendered_paths}\n"
                "  worktree_root:"
            ),
            1,
        ),
        encoding="utf-8",
    )


class GitRunnerTests(unittest.TestCase):
    def test_invalid_manual_destination_is_typed_before_push(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="manual", branch="manual")
                runner = GitRunner(config)
                common = {
                    "conn": conn,
                    "job_ids": [job.id],
                    "deploy_sha": "a" * 40,
                    "lease_token": "",
                    "worktree": repo,
                    "log": io.StringIO(),
                    "before_push": lambda: self.fail("push preflight ran"),
                    "ownership_pulse": lambda: None,
                    "state": atomic_push_module.PushVerifyState(),
                }
                with self.assertRaisesRegex(
                    MergeBlocked, "deploy_destination_invalid"
                ):
                    runner._push_and_verify(**common)
                with self.assertRaisesRegex(
                    DeployPlanChanged, "confirmed destination"
                ):
                    runner._push_and_verify(
                        **common,
                        expected_plan_sha="b" * 64,
                    )
            finally:
                conn.close()

    def test_expected_plan_is_rechecked_with_the_pinned_destination(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, _marker = make_demo_repo(Path(td))
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="manual", branch="feature/a")
                with self.assertRaisesRegex(
                    DeployPlanChanged, "confirmed train, destination"
                ):
                    GitRunner(config)._push_and_verify(
                        conn,
                        job_ids=[job.id],
                        deploy_sha="a" * 40,
                        lease_token="",
                        worktree=repo,
                        log=io.StringIO(),
                        before_push=lambda: self.fail("push preflight ran"),
                        ownership_pulse=lambda: None,
                        state=atomic_push_module.PushVerifyState(),
                        expected_plan_sha="b" * 64,
                    )
            finally:
                conn.close()

    def test_auto_destination_change_during_gates_blocks_before_push(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            other_remote = root / "other.git"
            git(root, "init", "--bare", str(other_remote))
            config = load_config(repo=repo)
            approved_destination = deploy_destination_sha(config)
            conn = connect(config.state.db)
            owner = f"runner:{os.getpid()}"
            try:
                enqueue_job(
                    conn,
                    task="a",
                    branch="feature/a",
                    base_sha=git(repo, "rev-parse", "origin/main"),
                    head_sha=git(repo, "rev-parse", "feature/a"),
                    auto_deploy=True,
                    approval_destination_sha=approved_destination,
                )
                claimed = claim_all_queued(
                    conn,
                    owner=owner,
                    auto_only=True,
                    deploy=True,
                    approval_destination_sha=approved_destination,
                )
                runner = GitRunner(config)
                real_run_gates = runner._run_gates

                def gates_then_change_destination(*args, **kwargs):  # type: ignore[no-untyped-def]
                    result = real_run_gates(*args, **kwargs)
                    git(
                        repo,
                        "remote",
                        "set-url",
                        "--push",
                        "origin",
                        str(other_remote),
                    )
                    return result

                with patch.object(
                    runner,
                    "_run_gates",
                    side_effect=gates_then_change_destination,
                ):
                    results = runner.process_batch(
                        conn,
                        claimed,
                        deploy=True,
                        owner=owner,
                        ttl_minutes=1,
                    )
            finally:
                lock = get_lock(conn)
                if lock is not None:
                    release_runner_lock(conn, owner=owner, token=lock.token)
                conn.close()

            self.assertEqual([job.status for job in results], ["blocked"])
            self.assertIn("approval_destination_changed", results[0].note)
            self.assertEqual(results[0].pending_deploy_sha, "")
            self.assertNotIn(
                "a.txt",
                git(root / "remote.git", "ls-tree", "-r", "--name-only", "main"),
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "show-ref"],
                    cwd=other_remote,
                    text=True,
                    capture_output=True,
                    check=False,
                ).stdout,
                "",
            )

    def test_multiple_push_urls_added_during_gates_block_before_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            remote_b = root / "remote-b.git"
            remote_c = root / "remote-c.git"
            git(root, "init", "--bare", str(remote_b))
            git(root, "init", "--bare", str(remote_c))
            config = load_config(repo=repo)
            approved_destination = deploy_destination_sha(config)
            conn = connect(config.state.db)
            owner = f"runner:{os.getpid()}"
            try:
                job = enqueue_job(
                    conn,
                    task="a",
                    branch="feature/a",
                    base_sha=git(repo, "rev-parse", "origin/main"),
                    head_sha=git(repo, "rev-parse", "feature/a"),
                    auto_deploy=True,
                    approval_destination_sha=approved_destination,
                )
                claimed = claim_all_queued(
                    conn,
                    owner=owner,
                    auto_only=True,
                    deploy=True,
                    approval_destination_sha=approved_destination,
                )
                runner = GitRunner(config)
                real_run_gates = runner._run_gates

                def gates_then_add_push_urls(*args, **kwargs):  # type: ignore[no-untyped-def]
                    result = real_run_gates(*args, **kwargs)
                    git(
                        repo,
                        "config",
                        "--add",
                        "remote.origin.pushurl",
                        str(remote_b),
                    )
                    git(
                        repo,
                        "config",
                        "--add",
                        "remote.origin.pushurl",
                        str(remote_c),
                    )
                    return result

                with patch.object(
                    runner,
                    "_run_gates",
                    side_effect=gates_then_add_push_urls,
                ):
                    results = runner.process_batch(
                        conn,
                        claimed,
                        deploy=True,
                        owner=owner,
                        ttl_minutes=1,
                    )
                stored = get_job(conn, job.id)
            finally:
                lock = get_lock(conn)
                if lock is not None:
                    release_runner_lock(conn, owner=owner, token=lock.token)
                conn.close()

            self.assertEqual([result.status for result in results], ["blocked"])
            self.assertIn("approval_destination_changed", stored.note)
            self.assertEqual(stored.pending_deploy_sha, "")
            for remote in (remote_b, remote_c):
                self.assertEqual(
                    subprocess.run(
                        ["git", "show-ref"],
                        cwd=remote,
                        text=True,
                        capture_output=True,
                        check=False,
                    ).stdout,
                    "",
                )

    def test_parallel_gate_group_is_bounded_and_events_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                """gate_parallelism:
  max_workers: 2
gates:
  - name: slow
    run: '"$MERGETRAIN_RUNNER_PYTHON" -c "import time; time.sleep(0.3); print(1)"'
    parallel_group: quality
  - name: fast
    run: '"$MERGETRAIN_RUNNER_PYTHON" -c "print(2)"'
    parallel_group: quality
  - name: later
    run: '"$MERGETRAIN_RUNNER_PYTHON" -c "print(3)"'
    parallel_group: quality
""",
                encoding="utf-8",
            )
            runner = GitRunner(load_config(repo=repo))
            log = io.StringIO()
            events: list[tuple[str, str]] = []

            runner._run_configured_gate_plan(
                worktree=repo,
                log=log,
                pulse=None,
                on_gate=lambda name, state, _index, _total, _detail: events.append(
                    (name, state)
                ),
                initial_states={},
            )

            self.assertEqual(
                events,
                [
                    ("slow", "active"),
                    ("fast", "active"),
                    ("slow", "success"),
                    ("fast", "success"),
                    ("later", "active"),
                    ("later", "success"),
                ],
            )
            rendered = log.getvalue()
            self.assertLess(rendered.index("\n1\n"), rendered.index("\n2\n"))

    def test_parallel_gate_failure_cancels_peer_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                """gate_parallelism:
  max_workers: 2
gates:
  - name: fail
    run: '"$MERGETRAIN_RUNNER_PYTHON" -c "import sys; sys.exit(3)"'
    parallel_group: quality
  - name: peer
    run: '"$MERGETRAIN_RUNNER_PYTHON" -c "import time; time.sleep(10)"'
    parallel_group: quality
""",
                encoding="utf-8",
            )
            runner = GitRunner(load_config(repo=repo))
            events: list[tuple[str, str]] = []
            started = time.monotonic()

            with self.assertRaises(CommandFailed):
                runner._run_configured_gate_plan(
                    worktree=repo,
                    log=io.StringIO(),
                    pulse=None,
                    on_gate=lambda name, state, _index, _total, _detail: events.append(
                        (name, state)
                    ),
                    initial_states={},
                )

            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(
                events[-2:],
                [("fail", "failure"), ("peer", "canceled")],
            )

    def test_parallel_gate_honors_per_gate_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                """gates:
  - name: bounded
    run: '"$MERGETRAIN_RUNNER_PYTHON" -c "import time; time.sleep(10)"'
    timeout_seconds: 1
""",
                encoding="utf-8",
            )
            runner = GitRunner(load_config(repo=repo))
            started = time.monotonic()

            with self.assertRaises(CommandFailed) as raised:
                runner._run_configured_gate_plan(
                    worktree=repo,
                    log=io.StringIO(),
                    pulse=None,
                    on_gate=None,
                    initial_states={},
                )

            self.assertEqual(raised.exception.returncode, 124)
            self.assertLess(time.monotonic() - started, 5)

    def test_parallel_gate_honors_total_plan_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".mergetrain.yaml").write_text(
                """gate_parallelism:
  max_workers: 2
  timeout_seconds: 1
gates:
  - name: first
    run: '"$MERGETRAIN_RUNNER_PYTHON" -c "import time; time.sleep(10)"'
    parallel_group: bounded
  - name: second
    run: '"$MERGETRAIN_RUNNER_PYTHON" -c "import time; time.sleep(10)"'
    parallel_group: bounded
""",
                encoding="utf-8",
            )
            runner = GitRunner(load_config(repo=repo))
            events: list[tuple[str, str]] = []
            started = time.monotonic()

            with self.assertRaises(CommandFailed) as raised:
                runner._run_configured_gate_plan(
                    worktree=repo,
                    log=io.StringIO(),
                    pulse=None,
                    on_gate=lambda name, state, _index, _total, _detail: events.append(
                        (name, state)
                    ),
                    initial_states={},
                )

            self.assertEqual(raised.exception.returncode, 124)
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(
                events[-2:],
                [("first", "canceled"), ("second", "canceled")],
            )

    def test_builtin_integrity_gate_finishes_before_parallel_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config_path = repo / ".mergetrain.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    """gates:
  - name: marker
    run:""",
                    """gate_parallelism:
  max_workers: 2
gates:
  - name: first
    parallel_group: quality
    run:""",
                    1,
                ).replace(
                    "deploy:\n",
                    """  - name: second
    parallel_group: quality
    run: '"$MERGETRAIN_RUNNER_PYTHON" -c "print(2)"'
deploy:
""",
                    1,
                ),
                encoding="utf-8",
            )
            runner = GitRunner(load_config(repo=repo))
            events: list[tuple[str, str]] = []

            runner._run_gates(
                worktree=repo,
                log=io.StringIO(),
                pulse=None,
                on_gate=lambda name, state, _index, _total, _detail: events.append(
                    (name, state)
                ),
            )

            self.assertEqual(
                events[:4],
                [
                    ("diff-check", "active"),
                    ("diff-check", "success"),
                    ("first", "active"),
                    ("second", "active"),
                ],
            )

    def test_exact_configured_diff_check_duplicate_runs_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config_path = repo / ".mergetrain.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "gates:\n",
                    "gates:\n"
                    "  - name: diff-check\n"
                    "    run: git diff --check ${integration_ref}..HEAD\n",
                    1,
                ),
                encoding="utf-8",
            )
            runner = GitRunner(load_config(repo=repo))
            events: list[tuple[str, str, int]] = []

            runner._run_gates(
                worktree=repo,
                log=io.StringIO(),
                pulse=None,
                on_gate=lambda name, state, _index, total, _detail: events.append(
                    (name, state, total)
                ),
            )

            self.assertEqual(
                [event for event in events if event[0] == "diff-check"],
                [
                    ("diff-check", "active", 2),
                    ("diff-check", "success", 2),
                ],
            )

    def test_command_env_prioritizes_the_runner_python_tool_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            runner_bin = root / "runner env" / "bin"
            runner_python = runner_bin / "python"
            first = root / "first"
            second = root / "second"
            inherited_path = os.pathsep.join(
                (str(first), str(runner_bin), str(second))
            )
            with (
                patch.object(
                    command_runner_module.sys,
                    "executable",
                    str(runner_python),
                ),
                patch.dict(os.environ, {"PATH": inherited_path}),
            ):
                env = command_env(config=config, worktree=repo)

            self.assertEqual(
                env["PATH"].split(os.pathsep),
                [str(runner_bin), str(first), str(second)],
            )
            self.assertEqual(
                env["MERGETRAIN_RUNNER_PYTHON"],
                str(runner_python),
            )

    def test_persistent_validation_workspace_reuses_only_declared_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            observed = root / "observed-cache-count.txt"
            gate = (
                f'{SHELL_PYTHON} -c "from pathlib import Path; '
                "p=Path('.cache/count'); p.parent.mkdir(parents=True, exist_ok=True); "
                "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1'); "
                f"Path('{py_path(observed)}').write_text(p.read_text())\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            (repo / ".gitignore").write_text(".cache/\n", encoding="utf-8")
            git(repo, "add", ".gitignore")
            git(repo, "commit", "-m", "ignore generated validation cache")
            git(repo, "push", "origin", "main")
            enable_persistent_validation_workspace(repo)
            config = load_config(repo=repo)
            runner = GitRunner(config)
            conn = connect(config.state.db)
            try:
                first = enqueue_job(conn, task="first", branch="feature/a")
                first_result = runner.process_batch(
                    conn, [first], deploy=False
                )[0]
                self.assertEqual(first_result.status, "validated")
                self.assertEqual(observed.read_text(encoding="utf-8"), "1")

                workspace = config.validation_worktree_path
                self.assertTrue(workspace.is_dir())
                (workspace / "app.txt").write_text("dirty\n", encoding="utf-8")
                (workspace / "scratch.tmp").write_text("remove me\n", encoding="utf-8")

                second = enqueue_job(
                    conn,
                    task="second",
                    branch="feature/a",
                    allow_duplicate=True,
                )
                second_result = runner.process_batch(
                    conn, [second], deploy=False
                )[0]
                events = list_run_events(conn)
            finally:
                conn.close()

            self.assertEqual(second_result.status, "validated")
            self.assertEqual(observed.read_text(encoding="utf-8"), "2")
            self.assertEqual(
                (config.validation_worktree_path / "app.txt").read_text(
                    encoding="utf-8"
                ),
                "base\n",
            )
            self.assertFalse(
                (config.validation_worktree_path / "scratch.tmp").exists()
            )
            self.assertIn(
                "Persistent validation workspace reused",
                [event.message for event in events],
            )
            self.assertIn(
                "Persistent validation cache reused",
                [event.message for event in events],
            )

    def test_persistent_validation_cache_key_change_invalidates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            observed = root / "observed-cache-count.txt"
            gate = (
                f'{SHELL_PYTHON} -c "from pathlib import Path; '
                "p=Path('.cache/count'); p.parent.mkdir(parents=True, exist_ok=True); "
                "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1'); "
                f"Path('{py_path(observed)}').write_text(p.read_text())\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            (repo / ".gitignore").write_text(".cache/\n", encoding="utf-8")
            git(repo, "add", ".gitignore")
            git(repo, "commit", "-m", "ignore generated validation cache")
            git(repo, "push", "origin", "main")
            enable_persistent_validation_workspace(repo, cache_key="cache-v1")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                first = enqueue_job(conn, task="first", branch="feature/a")
                self.assertEqual(
                    GitRunner(config).process_batch(
                        conn, [first], deploy=False
                    )[0].status,
                    "validated",
                )
                config_path = repo / ".mergetrain.yaml"
                config_path.write_text(
                    config_path.read_text(encoding="utf-8").replace(
                        "cache-v1", "cache-v2"
                    ),
                    encoding="utf-8",
                )
                changed = load_config(repo=repo)
                second = enqueue_job(
                    conn,
                    task="second",
                    branch="feature/a",
                    allow_duplicate=True,
                )
                result = GitRunner(changed).process_batch(
                    conn, [second], deploy=False
                )[0]
            finally:
                conn.close()

            self.assertEqual(result.status, "validated")
            self.assertEqual(observed.read_text(encoding="utf-8"), "1")

    def test_persistent_validation_toolchain_fingerprint_invalidates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            observed = root / "observed-cache-count.txt"
            toolchain = root / "toolchain.txt"
            toolchain.write_text("tool-a\n", encoding="utf-8")
            gate = (
                f'{SHELL_PYTHON} -c "from pathlib import Path; '
                "p=Path('.cache/count'); p.parent.mkdir(parents=True, exist_ok=True); "
                "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1'); "
                f"Path('{py_path(observed)}').write_text(p.read_text())\""
            )
            fingerprint = (
                f'{SHELL_PYTHON} -c "from pathlib import Path; '
                f"print(Path('{py_path(toolchain)}').read_text().strip())\""
            )
            repo, _ = make_demo_repo(
                root,
                gate_command=gate,
                fingerprint_command=fingerprint,
            )
            (repo / ".gitignore").write_text(".cache/\n", encoding="utf-8")
            git(repo, "add", ".gitignore")
            git(repo, "commit", "-m", "ignore generated validation cache")
            git(repo, "push", "origin", "main")
            enable_persistent_validation_workspace(repo)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                first = enqueue_job(conn, task="first", branch="feature/a")
                self.assertEqual(
                    GitRunner(config).process_batch(
                        conn, [first], deploy=False
                    )[0].status,
                    "validated",
                )
                toolchain.write_text("tool-b\n", encoding="utf-8")
                second = enqueue_job(
                    conn,
                    task="second",
                    branch="feature/a",
                    allow_duplicate=True,
                )
                result = GitRunner(config).process_batch(
                    conn, [second], deploy=False
                )[0]
            finally:
                conn.close()

            self.assertEqual(result.status, "validated")
            self.assertEqual(observed.read_text(encoding="utf-8"), "1")

    def test_persistent_validation_cache_rejects_tracked_or_unignored_paths(self) -> None:
        cases = (("app.txt", "tracked files"), ("generated-cache", "ignored by Git"))
        for cache_path, expected in cases:
            with self.subTest(cache_path=cache_path), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                repo, _ = make_demo_repo(root)
                enable_persistent_validation_workspace(
                    repo, cache_paths=(cache_path,)
                )
                config = load_config(repo=repo)
                conn = connect(config.state.db)
                try:
                    job = enqueue_job(conn, task="unsafe cache", branch="feature/a")
                    result = GitRunner(config).process_batch(
                        conn, [job], deploy=False
                    )[0]
                finally:
                    conn.close()

                self.assertEqual(result.status, "blocked")
                self.assertIn(expected, result.note)

    def test_command_env_does_not_prepend_cwd_without_a_python_executable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            inherited_path = os.pathsep.join(
                (str(root / "first"), str(root / "second"))
            )
            with (
                patch.object(command_runner_module.sys, "executable", ""),
                patch.dict(os.environ, {"PATH": inherited_path}),
            ):
                env = command_env(config=config, worktree=repo)

            self.assertEqual(env["PATH"], inherited_path)
            self.assertEqual(env["MERGETRAIN_RUNNER_PYTHON"], "")

    def test_gate_finds_tool_beside_runner_python_without_activation(self) -> None:
        runner_bin = Path(sys.executable).parent
        if shutil.which("ruff", path=str(runner_bin)) is None:
            self.skipTest("ruff is not installed beside the test interpreter")
        runner_bin_key = os.path.normcase(os.path.abspath(runner_bin))
        inherited_path = os.pathsep.join(
            entry
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry
            and os.path.normcase(os.path.abspath(entry)) != runner_bin_key
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root, gate_command="ruff --version")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="runner tools", branch="feature/a")
                with patch.dict(os.environ, {"PATH": inherited_path}):
                    result = GitRunner(config).process_batch(
                        conn,
                        [job],
                        deploy=False,
                    )[0]
            finally:
                conn.close()

            self.assertEqual(result.status, "validated")

    def test_path_scoped_gate_skips_without_a_match_and_records_progress(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root, gate_paths=("src/**",))
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                result = GitRunner(config).process_batch(
                    conn, [job], deploy=False
                )[0]
                events = list_run_events(conn)
            finally:
                conn.close()

            self.assertEqual(result.status, "validated")
            self.assertFalse(marker.exists())
            skipped = next(
                event
                for event in events
                if event.message == "Skipped gate 2/2: marker"
            )
            self.assertEqual(skipped.state, "skipped")
            self.assertEqual(
                skipped.detail, "no changed paths matched configured paths"
            )

    def test_path_scoped_gate_runs_for_a_train_wide_match(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root, gate_paths=("b.txt",))
            git(repo, "switch", "-c", "agent/b", "main")
            (repo / "b.txt").write_text("b\n", encoding="utf-8")
            git(repo, "add", "b.txt")
            git(repo, "commit", "-m", "b")
            git(repo, "switch", "main")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [
                    enqueue_job(conn, task="a", branch="feature/a"),
                    enqueue_job(conn, task="b", branch="agent/b"),
                ]
                results = GitRunner(config).process_batch(
                    conn, jobs, deploy=False
                )
                events = list_run_events(conn)
            finally:
                conn.close()

            self.assertEqual(
                [result.status for result in results],
                ["validated", "validated"],
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            self.assertIn(
                "Running gate 2/2: marker",
                [event.message for event in events],
            )

    def test_path_discovery_failure_runs_scoped_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root, gate_paths=("src/**",))
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                with patch(
                    "mergetrain.gate_runner.parse_name_status_z",
                    side_effect=ValueError("broken diff"),
                ):
                    result = runner.process_batch(conn, [job], deploy=False)[0]
            finally:
                conn.close()

            self.assertEqual(result.status, "validated")
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")

    def test_renamed_path_matches_both_old_and_new_names(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root, gate_paths=("app.txt",))
            git(repo, "switch", "feature/a")
            git(repo, "mv", "app.txt", "renamed.txt")
            git(repo, "commit", "-m", "rename app")
            git(repo, "switch", "main")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="rename", branch="feature/a")
                result = GitRunner(config).process_batch(
                    conn, [job], deploy=False
                )[0]
            finally:
                conn.close()

            self.assertEqual(result.status, "validated")
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")

    def test_windows_stop_uses_taskkill_for_the_process_tree(self) -> None:
        process = Mock()
        process.pid = 4321
        process.poll.return_value = None
        process.wait.return_value = 0
        completed = subprocess.CompletedProcess([], 0)

        with (
            patch("mergetrain.command_runner.os.name", "nt"),
            patch("mergetrain.command_runner.subprocess.run", return_value=completed) as run,
        ):
            stopped = command_runner_module._stop_process(process)

        self.assertTrue(stopped)
        run.assert_called_once_with(
            ["taskkill", "/PID", "4321", "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows process-tree regression")
    def test_timeout_kills_windows_grandchild_process_tree(self) -> None:
        import ctypes

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pid_file = root / "grandchild.pid"
            grandchild = "import time; time.sleep(60)"
            parent = (
                "import pathlib, subprocess, sys, time; "
                f"p=subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
                "time.sleep(60)"
            )

            completed = command_runner_module.run_command(
                [sys.executable, "-c", parent],
                cwd=root,
                check=False,
                timeout_seconds=2,
            )

            self.assertEqual(completed.returncode, 124)
            self.assertTrue(pid_file.is_file())
            pid = int(pid_file.read_text(encoding="utf-8"))
            synchronize = 0x00100000
            wait_timeout = 0x00000102
            handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
            if not handle:
                running = False
            else:
                try:
                    running = (
                        ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
                        == wait_timeout
                    )
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
            self.assertFalse(running, f"grandchild process {pid} survived timeout")

    def test_shell_command_uses_git_for_windows_sh_without_cmd_fallback(self) -> None:
        with (
            patch("mergetrain.command_runner.Path.exists", return_value=False),
            patch("mergetrain.command_runner.shutil.which") as which,
        ):
            which.side_effect = lambda name: (
                "C:/Program Files/Git/bin/sh.exe" if name == "sh" else None
            )

            command = _shell_command("printf '%s' ok")

        self.assertEqual(
            command,
            ["C:/Program Files/Git/bin/sh.exe", "-c", "printf '%s' ok"],
        )

    def test_gate_commands_never_go_through_a_platform_shell(self) -> None:
        """The single-shell guarantee, pinned where it could regress.

        `${repo}`/`${worktree}` escaping targets POSIX sh on every platform, so
        it is only correct while gates are spawned as argv. A `shell=True` here
        would run them under cmd.exe on Windows and quietly invalidate both the
        escaping and the documented gate contract.
        """

        with tempfile.TemporaryDirectory() as td:
            repo, _ = make_demo_repo(Path(td))
            config = load_config(repo=repo)
            with patch("mergetrain.command_runner.subprocess.Popen") as popen:
                popen.return_value.__enter__ = Mock(return_value=popen.return_value)
                popen.return_value.stdout = io.StringIO("")
                popen.return_value.stderr = io.StringIO("")
                popen.return_value.wait.return_value = 0
                popen.return_value.returncode = 0
                run_shell(
                    "printf ok",
                    cwd=repo,
                    env=command_env(config=config, worktree=repo),
                )

        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertEqual(args[0][1:], ["-c", "printf ok"])
        self.assertFalse(kwargs["shell"])
        self.assertNotIn("executable", kwargs)

    def test_path_placeholders_are_shell_safe_in_all_quote_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root $(touch injected)"
            root.mkdir()
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            worktree = root / "worktree $(touch worktree-injected)"
            command = expand_command(
                (
                    f'{SHELL_PYTHON} -c "import json,sys; '
                    'print(json.dumps(sys.argv[1:]))" '
                    '${repo} "${repo}" ${worktree} \'${worktree}\''
                ),
                config=config,
                worktree=worktree,
            )

            completed = run_shell(
                command,
                cwd=repo,
                env=command_env(config=config, worktree=worktree),
            )

            self.assertEqual(
                json.loads(completed.stdout),
                [str(config.repo), str(config.repo), str(worktree), str(worktree)],
            )
            self.assertFalse((repo / "injected").exists())
            self.assertFalse((repo / "worktree-injected").exists())

    def test_unchanged_validated_train_reuses_gates_and_still_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            verify_marker = root / "verify.txt"
            verify = f'{SHELL_PYTHON} -c "from pathlib import Path; Path(\'{py_path(verify_marker)}\').write_text(\'verified\')"'
            repo, marker = make_demo_repo(root, verify_command=verify)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                validated = runner.process_batch(conn, [job], deploy=False)[0]
                deployed = runner.process_batch(
                    conn,
                    [validated],
                    deploy=True,
                    reuse_validated=True,
                )[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.status, "deployed")
            self.assertEqual(deployed.deploy_sha, validated.validation_sha)
            self.assertEqual(deployed.reused_validation_sha, validated.validation_sha)
            self.assertTrue(validated.validation_tree_sha)
            self.assertTrue(validated.validation_gate_policy_sha)
            self.assertTrue(validated.validation_environment_sha)
            self.assertTrue(validated.validation_train_sha)
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            self.assertEqual(verify_marker.read_text(encoding="utf-8"), "verified")
            reused = [event for event in events if event.state == "reused"]
            self.assertEqual(
                [event.message for event in reused],
                ["Reused gate 1/2: diff-check", "Reused gate 2/2: marker"],
            )

    def test_path_skipped_gate_remains_skipped_during_exact_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root, gate_paths=("src/**",))
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                validated = runner.process_batch(conn, [job], deploy=False)[0]
                deployed = runner.process_batch(
                    conn,
                    [validated],
                    deploy=True,
                    reuse_validated=True,
                )[0]
                events = list_run_events(conn)
            finally:
                conn.close()

            self.assertEqual(deployed.status, "deployed")
            self.assertEqual(
                deployed.reused_validation_sha, validated.validation_sha
            )
            self.assertFalse(marker.exists())
            self.assertEqual(
                [
                    event.message
                    for event in events
                    if event.state == "skipped"
                ],
                [
                    "Skipped gate 2/2: marker",
                    "Skipped gate 2/2: marker",
                ],
            )

    def test_reuse_runs_scoped_gate_when_path_discovery_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root, gate_paths=("src/**",))
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                validated = runner.process_batch(conn, [job], deploy=False)[0]
                with patch(
                    "mergetrain.gate_runner.parse_name_status_z",
                    side_effect=ValueError("broken diff"),
                ):
                    deployed = runner.process_batch(
                        conn,
                        [validated],
                        deploy=True,
                        reuse_validated=True,
                    )[0]
            finally:
                conn.close()

            self.assertEqual(deployed.status, "deployed")
            self.assertEqual(
                deployed.reused_validation_sha, validated.validation_sha
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")

    def test_reuse_preview_json_names_exact_validation_sha_without_claiming(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
            finally:
                conn.close()
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "--repo",
                        str(repo),
                        "--db",
                        str(config.state.db),
                        "run-batch",
                        "--deploy",
                        "--train-id",
                        validated.train_id,
                        "--reuse-validated",
                        "--preview",
                        "--json",
                    ]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["preview"])
            self.assertTrue(payload["reuse"]["eligible"])
            self.assertEqual(
                payload["reuse"]["reused_validation_sha"],
                validated.validation_sha,
            )
            checks = {
                check["code"]: check for check in payload["reuse"]["identity_checks"]
            }
            self.assertEqual(checks["integration_base"]["status"], "match")
            self.assertEqual(checks["gate_policy"]["status"], "match")
            self.assertEqual(checks["environment"]["status"], "match")
            self.assertTrue(
                all(
                    gate["action"] == "reuse"
                    for gate in payload["reuse"]["gates"]
                )
            )
            self.assertFalse(
                payload["reuse"]["estimated_savings"]["authorizes_reuse"]
            )
            self.assertGreater(
                payload["reuse"]["estimated_savings"]["timed_gate_count"], 0
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:a.txt")

    def test_config_authorization_reuses_but_required_gate_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(
                root,
                reuse_enabled=True,
                always_rerun_on_deploy=True,
            )
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                validated = runner.process_batch(conn, [job], deploy=False)[0]
                deployed = runner.process_batch(conn, [validated], deploy=True)[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.reused_validation_sha, validated.validation_sha)
            self.assertEqual(marker.read_text(encoding="utf-8"), "xx")
            self.assertIn("Reused gate 1/2: diff-check", [event.message for event in events])
            self.assertIn("Running gate 2/2: marker", [event.message for event in events])

    def test_environment_fingerprint_change_falls_back_to_full_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fingerprint = root / "toolchain.txt"
            fingerprint.write_text("tool-a\n", encoding="utf-8")
            repo, marker = make_demo_repo(
                root,
                reuse_enabled=True,
                # `cat` is not a Windows command; read the file portably.
                fingerprint_command=(
                    f"{SHELL_PYTHON} -c \"from pathlib import Path; "
                    f"print(Path('{py_path(fingerprint)}').read_text())\""
                ),
            )
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                validated = runner.process_batch(conn, [job], deploy=False)[0]
                fingerprint.write_text("tool-b\n", encoding="utf-8")
                deployed = runner.process_batch(conn, [validated], deploy=True)[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.reused_validation_sha, "")
            self.assertEqual(marker.read_text(encoding="utf-8"), "xx")
            fallback = next(
                event
                for event in events
                if event.message == "Validated gates were not reused; rerunning all gates"
            )
            self.assertIn("environment or toolchain fingerprint changed", fallback.detail)

    def test_fingerprint_side_effect_does_not_dirty_reused_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(
                root,
                reuse_enabled=True,
                fingerprint_command=(
                    f"{SHELL_PYTHON} -c \"from pathlib import Path; "
                    "Path('fingerprint.tmp').write_text('side effect'); "
                    "print('tool-a')\""
                ),
            )
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                validated = runner.process_batch(conn, [job], deploy=False)[0]
                deployed = runner.process_batch(conn, [validated], deploy=True)[0]
            finally:
                conn.close()

            self.assertEqual(deployed.status, "deployed")
            self.assertEqual(deployed.reused_validation_sha, validated.validation_sha)
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")

    def test_push_failure_is_not_reported_as_deployed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                failure = CommandFailed(
                    ["git", "push"], 1, stderr="remote rejected the update"
                )
                with patch.object(runner, "push_verified_head", side_effect=failure):
                    result = runner.process_one(conn, job, deploy=True)
            finally:
                conn.close()
            # A non-rejection push failure is AMBIGUOUS (the remote may have
            # accepted it), so it parks needs_reconcile — never a terminal
            # 'failed' that a later deploy would silently push over (guarantee #4).
            # (Marker preservation with a real claim is covered end-to-end in
            # test_reconcile.test_ambiguous_push_parks_needs_reconcile_*.)
            self.assertEqual(result.status, "needs_reconcile")
            # And push_status stays `pending`, the value the durable marker
            # already recorded and the same one a crashed mid-push job carries
            # (test_reconcile asserts that). Reporting `failed` for refs that may
            # be on the remote is the single thing this tool promises not to do;
            # reconcile replaces it with the remote's answer.
            self.assertEqual(result.push_status, "pending")
            self.assertEqual(result.verify_status, "not_run")

    def test_definitive_push_rejection_blocks_not_reconciles(self) -> None:
        # A structured rejection record proves the remote did NOT accept the
        # push, including the normal race where the target advanced first.
        # It must finalize 'blocked', NOT needs_reconcile, so the ambiguous-push
        # fix never mislabels a real rejection as an ambiguous outcome.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                enqueue_job(conn, task="a", branch="feature/a")
                owner = f"runner:{os.getpid()}"
                claimed = claim_deploy_batch(conn, owner=owner)
                runner = GitRunner(config)
                rejection = CommandFailed(
                    ["git", "push"], 1,
                    stderr="! [rejected] main -> main (fetch first)",
                )
                with patch.object(runner, "push_verified_head", side_effect=rejection):
                    result = runner.process_one(
                        conn, claimed[0], deploy=True, owner=owner
                    )
                action = next_action({"counts": counts(conn)})
            finally:
                conn.close()
            self.assertEqual(result.status, "blocked")
            # The other side of the pending/failed split: a definitive rejection
            # proves nothing landed, so here `failed` is the honest value.
            self.assertEqual(result.push_status, "failed")
            self.assertEqual(result.pending_deploy_sha, "")
            self.assertEqual(action, "fix_blocked_job")
            pending = git(
                repo, "for-each-ref", "--format=%(refname)", "refs/mergetrain/pending/"
            )
            self.assertEqual(pending, "")

    def test_a_gate_that_mutates_the_worktree_blocks_the_deploy(self) -> None:
        # Guarantee #1: gates are verification, not mutation. A gate that dirties
        # (or commits to) the integration worktree after the deploy sha is
        # recorded blocks the deploy — a tree differing from the tested sha is
        # never shipped, and the push is never reached.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root, gate_command="echo x > gate-dirty.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                result = GitRunner(config).process_batch(conn, [job], deploy=True)[0]
                pending = git(
                    repo, "for-each-ref", "--format=%(refname)", "refs/mergetrain/pending/"
                )
            finally:
                conn.close()
            self.assertEqual(result.status, "blocked")
            self.assertIn("tree", result.note.lower())
            self.assertEqual(pending, "")  # never reached the push / marker
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:a.txt")

    def test_gate_tripwire_blocks_when_worktree_status_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            runner = GitRunner(load_config(repo=repo))
            deploy_sha = git(repo, "rev-parse", "HEAD")
            status_error = CommandFailed(
                ["git", "status", "--porcelain"],
                128,
                stderr="fatal: index file is unreadable",
                cwd=str(repo),
            )

            with patch(
                "mergetrain.atomic_push.git_worktree_clean",
                side_effect=status_error,
            ):
                with self.assertRaisesRegex(MergeBlocked, "could not verify.*clean"):
                    runner._assert_tree_unchanged_by_gates(repo, deploy_sha)
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:a.txt")

    def test_unexpected_post_push_error_preserves_deployed_truth(self) -> None:
        for batch in (False, True):
            with self.subTest(batch=batch), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                verify = f'{SHELL_PYTHON} -c "import sys; sys.exit(0)"'
                repo, _marker = make_demo_repo(root, verify_command=verify)
                config = load_config(repo=repo)
                conn = connect(config.state.db)
                try:
                    job = enqueue_job(conn, task="a", branch="feature/a")
                    runner = GitRunner(config)
                    with patch.object(
                        runner,
                        "_run_verify_hooks",
                        side_effect=RuntimeError("verification crashed"),
                    ):
                        result = (
                            runner.process_batch(conn, [job], deploy=True)[0]
                            if batch
                            else runner.process_one(conn, job, deploy=True)
                        )
                    events = list_run_events(conn)
                finally:
                    conn.close()
                self.assertEqual(result.status, "deployed")
                self.assertEqual(result.push_status, "succeeded")
                # 'unknown', not 'failed': the hook never returned a verdict, it
                # crashed, so nothing determined that verification failed. A hook
                # that genuinely fails exits non-zero and is recorded 'failed' on
                # the normal path; this boundary only ever sees unexpected errors.
                # 'unknown' is also the value doctor turns into next_action
                # verify_reconciled_deploy, so the operator gets 'run mergetrain
                # verify' instead of a dead end.
                self.assertEqual(result.verify_status, "unknown")
                self.assertIn("post-push completion warning", result.note)
                self.assertEqual(events[-1].phase, "complete")
                # Still a warning, not a plain success: 'unknown' has to draw the
                # same attention 'failed' did, or the completion event hides the
                # thing the operator must discharge.
                self.assertEqual(events[-1].state, "warning")
                self.assertIn("verification needs attention", events[-1].message)
                self.assertEqual(git(root / "remote.git", "show", "main:a.txt"), "a")
                pending = git(
                    repo,
                    "for-each-ref",
                    "--format=%(refname)",
                    "refs/mergetrain/pending/",
                )
                self.assertEqual(pending, "")

    def test_single_deploy_records_verify_success_and_failure(self) -> None:
        for returncode, expected_verify, expected_event_state in [
            (0, "succeeded", "success"),
            (7, "failed", "warning"),
        ]:
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                verify = f'{SHELL_PYTHON} -c "import sys; sys.exit({returncode})"'
                repo, _marker = make_demo_repo(root, verify_command=verify)
                config = load_config(repo=repo)
                conn = connect(config.state.db)
                try:
                    job = enqueue_job(conn, task="a", branch="feature/a")
                    result = GitRunner(config).process_one(conn, job, deploy=True)
                    events = list_run_events(conn)
                finally:
                    conn.close()
                self.assertEqual(result.status, "deployed")
                self.assertEqual(result.push_status, "succeeded")
                self.assertEqual(result.verify_status, expected_verify)
                self.assertEqual(events[-1].phase, "complete")
                self.assertEqual(events[-1].state, expected_event_state)
                if returncode:
                    self.assertIn("verification needs attention", events[-1].message)

    def test_batch_deploy_records_verify_success_and_failure(self) -> None:
        for returncode, expected_verify, expected_event_state in [
            (0, "succeeded", "success"),
            (9, "failed", "warning"),
        ]:
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                verify = f'{SHELL_PYTHON} -c "import sys; sys.exit({returncode})"'
                repo, _marker = make_demo_repo(root, verify_command=verify)
                config = load_config(repo=repo)
                conn = connect(config.state.db)
                try:
                    job = enqueue_job(conn, task="a", branch="feature/a")
                    result = GitRunner(config).process_batch(conn, [job], deploy=True)[0]
                    events = list_run_events(conn)
                finally:
                    conn.close()
                self.assertEqual(result.status, "deployed")
                self.assertEqual(result.push_status, "succeeded")
                self.assertEqual(result.verify_status, expected_verify)
                self.assertEqual(events[-1].phase, "complete")
                self.assertEqual(events[-1].state, expected_event_state)

    def test_deploy_clears_pending_marker_and_pin_ref(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                result = GitRunner(config).process_batch(conn, [job], deploy=True)[0]
            finally:
                conn.close()
            self.assertEqual(result.status, "deployed")
            # The write-ahead marker is cleared once the deploy is finalized,
            self.assertEqual(result.pending_deploy_sha, "")
            # and a clean deploy leaves no pin ref behind.
            pending = git(repo, "for-each-ref", "--format=%(refname)", "refs/mergetrain/pending/")
            self.assertEqual(pending, "")
            # The remote audit ref is retained after the local recovery pin is
            # cleared. It is the durable proof used if main is later rewritten.
            audit_ref = deploy_audit_ref_name(result.deploy_sha)
            self.assertEqual(
                git(root / "remote.git", "rev-parse", audit_ref),
                result.deploy_sha,
            )

    def test_conflicting_deploy_audit_ref_is_never_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            runner = GitRunner(load_config(repo=repo))
            base = git(repo, "rev-parse", "main")
            target = git(repo, "rev-parse", "feature/a")
            audit_ref = deploy_audit_ref_name(target)
            git(repo, "push", "origin", f"{base}:{audit_ref}")

            with self.assertRaisesRegex(PushRejected, "immutable audit evidence"):
                runner.push_verified_head(worktree=repo, deploy_sha=target)

            remote = root / "remote.git"
            self.assertEqual(git(remote, "rev-parse", "main"), base)
            self.assertEqual(git(remote, "rev-parse", audit_ref), base)

    def test_matching_deploy_audit_ref_allows_idempotent_push(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            runner = GitRunner(load_config(repo=repo))
            target = git(repo, "rev-parse", "feature/a")
            audit_ref = deploy_audit_ref_name(target)
            git(repo, "push", "origin", f"{target}:{audit_ref}")

            runner.push_verified_head(worktree=repo, deploy_sha=target)

            remote = root / "remote.git"
            self.assertEqual(git(remote, "rev-parse", "main"), target)
            self.assertEqual(git(remote, "rev-parse", audit_ref), target)

    def test_audit_ref_lease_rejects_a_concurrent_creation_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            runner = GitRunner(load_config(repo=repo))
            base = git(repo, "rev-parse", "main")
            target = git(repo, "rev-parse", "feature/a")
            audit_ref = deploy_audit_ref_name(target)
            # Model another writer creating the ref after our preflight said it
            # was absent. The lease must reject both this ref and main together.
            git(repo, "push", "origin", f"{base}:{audit_ref}")

            with patch.object(
                runner,
                "_audit_ref_expectation",
                return_value=(audit_ref, ""),
            ), self.assertRaises(CommandFailed) as raised:
                runner.push_verified_head(worktree=repo, deploy_sha=target)

            self.assertIn("stale info", raised.exception.stderr)
            remote = root / "remote.git"
            self.assertEqual(git(remote, "rev-parse", "main"), base)
            self.assertEqual(git(remote, "rev-parse", audit_ref), base)

    def test_pin_ref_failure_blocks_before_push_and_preserves_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                runner = GitRunner(config)
                original_run_command = atomic_push_module.run_command

                def fail_pending_ref(command, **kwargs):
                    if list(command[:2]) == ["git", "update-ref"]:
                        raise CommandFailed(command, 1, stderr="cannot lock ref")
                    return original_run_command(command, **kwargs)

                with patch.object(
                    atomic_push_module,
                    "run_command",
                    side_effect=fail_pending_ref,
                ), patch.object(runner, "push_verified_head") as push:
                    result = runner.process_one(conn, job, deploy=True)
            finally:
                conn.close()

            self.assertEqual(result.status, "blocked")
            self.assertTrue(result.pending_deploy_sha)
            self.assertIn("push was not attempted", result.note)
            push.assert_not_called()

    def test_dashboard_command_masks_obvious_secret_values(self) -> None:
        rendered = _dashboard_command(
            "TEST_TOKEN=fixture-value run-check --password fixture-password"
        )
        self.assertEqual(
            rendered,
            "TEST_TOKEN=[redacted] run-check --password [redacted]",
        )

    def test_command_failed_str_redacts_inline_secrets(self) -> None:
        # redact_secrets is the single masking primitive; CommandFailed.__str__
        # runs through it so the persisted job note never carries an inline
        # credential, matching what the dashboard already masks live.
        self.assertEqual(
            redact_secrets("deploy API_TOKEN=sk-abc123 --password hunter2"),
            "deploy API_TOKEN=[redacted] --password [redacted]",
        )
        exc = CommandFailed(["run-check", "--token", "sk-secret-xyz"], 1, stderr="boom")
        rendered = str(exc)
        self.assertNotIn("sk-secret-xyz", rendered)
        self.assertIn("--token [redacted]", rendered)
        self.assertIn("boom", rendered)
        url = "https://x-access-token:fixture-secret@example.com/repo.git"
        self.assertEqual(
            redact_secrets(url),
            "https://x-access-token:[redacted]@example.com/repo.git",
        )
        for error_type in (AmbiguousPush, PushRejected):
            with self.subTest(error_type=error_type.__name__):
                rendered = str(error_type(f"push stderr: {url}"))
                self.assertNotIn("fixture-secret", rendered)
                self.assertIn("[redacted]", rendered)

    def test_redact_secrets_covers_token_userinfo_and_quoted_values(self) -> None:
        cases = {
            "https://ghp_fixture-secret@example.com/org/repo.git": (
                "https://[redacted]@example.com/org/repo.git"
            ),
            'mysql --password="p@ss word" -h db': (
                "mysql --password=[redacted] -h db"
            ),
            "deploy --auth-token 'fixture secret' --access-token=second": (
                "deploy --auth-token [redacted] --access-token=[redacted]"
            ),
            'DB_PASS="fixture secret" PGPASS=second GITHUB_PAT=third': (
                "DB_PASS=[redacted] PGPASS=[redacted] GITHUB_PAT=[redacted]"
            ),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                redacted = redact_secrets(raw)
                self.assertEqual(redacted, expected)
                self.assertEqual(redact_secrets(redacted), expected)

        self.assertEqual(
            redact_secrets("MODE=release https://example.com/org/repo.git"),
            "MODE=release https://example.com/org/repo.git",
        )

    def test_failed_gate_note_redacts_inline_command_secret(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # The secret is inline in the gate command itself (not just its
            # output), so it lands in CommandFailed.command -> the job note.
            gate = (
                f'{SHELL_PYTHON} -c "import sys; sys.exit(5)" '
                "--token sk-inline-secret-value"
            )
            repo, _marker = make_demo_repo(root, gate_command=gate)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                result = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
            finally:
                conn.close()
            self.assertEqual(result.status, "failed")
            self.assertIn("[redacted]", result.note)
            self.assertNotIn("sk-inline-secret-value", result.note)

    def test_command_output_is_kept_out_of_structured_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            secret = "fixture-secret-output"
            gate = (
                f'{SHELL_PYTHON} -c "import sys; '
                "import os; print(os.environ['FIXTURE_EVENT_SECRET'], "
                "file=sys.stderr); sys.exit(5)\""
            )
            repo, _marker = make_demo_repo(root, gate_command=gate)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                with patch.dict(
                    os.environ, {"FIXTURE_EVENT_SECRET": secret}, clear=False
                ):
                    result = GitRunner(config).process_batch(
                        conn, [job], deploy=False
                    )[0]
                events = list_run_events(conn, limit=200)
            finally:
                conn.close()
            self.assertIn(secret, result.note)
            serialized_events = json.dumps(
                [event.to_dict() for event in events], ensure_ascii=False
            )
            self.assertNotIn(secret, serialized_events)
            self.assertIn("exit_code=5", serialized_events)

    def test_managed_command_timeout_terminates_process_group(self) -> None:
        # ignore_cleanup_errors: this test kills a subprocess mid-run; on
        # Windows the OS may still hold the killed process's cwd/pipe handles
        # when TemporaryDirectory tears down (WinError 32). Production worktree
        # cleanup is best-effort + gc for the same reason, so tolerate it here.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            started = time.monotonic()
            with self.assertRaises(CommandFailed) as raised:
                run_shell(
                    f'{SHELL_PYTHON} -c "import time; time.sleep(10)"',
                    cwd=td,
                    env=os.environ.copy(),
                    log=io.StringIO(),
                    timeout_seconds=0.2,
                    pulse_interval_seconds=0.1,
                )
            self.assertEqual(raised.exception.returncode, 124)
            self.assertLess(time.monotonic() - started, 3)

    def test_managed_command_replaces_invalid_utf8_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy()
            env.update({"LC_ALL": "C", "LANG": "C"})
            log = io.StringIO()
            completed = run_shell(
                f"{SHELL_PYTHON} -c \"import sys; "
                "sys.stdout.buffer.write(b'\\xff\\n')\"",
                cwd=td,
                env=env,
                log=log,
                timeout_seconds=2,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertIn("\ufffd", completed.stdout)
            self.assertIn("\ufffd", log.getvalue())

    def test_success_before_timeout_is_not_reclassified_while_log_drains(self) -> None:
        class SlowLog(io.StringIO):
            def write(self, value: str) -> int:
                if value == "slow-tail\n":
                    time.sleep(1.25)
                return super().write(value)

        with tempfile.TemporaryDirectory() as td:
            # This is a _run_managed ordering regression, not a shell-startup
            # benchmark. Direct argv keeps Windows sh.exe startup/Defender
            # jitter out of a deliberately narrow timing boundary, while the
            # log drain still lasts longer than the command timeout.
            completed = command_runner_module.run_command(
                [sys.executable, "-c", "print('slow-tail')"],
                cwd=td,
                env=os.environ.copy(),
                log=SlowLog(),
                check=False,
                timeout_seconds=1.0,
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(completed.stdout, "slow-tail\n")

    def test_run_shell_defaults_to_managed_noninteractive_execution(self) -> None:
        expected = subprocess.CompletedProcess("true", 0, "", "")
        with patch("mergetrain.command_runner._run_managed", return_value=expected) as run:
            completed = run_shell(
                "true", cwd=".", env={"PATH": os.environ.get("PATH", "")}
            )
        self.assertIs(completed, expected)
        self.assertEqual(run.call_args.kwargs["timeout_seconds"], 600.0)
        self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_batch_merges_jobs_and_runs_gate_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                results = GitRunner(config).process_batch(conn, [job], deploy=False)
                stored = get_job(conn, job.id)
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual([result.status for result in results], ["validated"])
            self.assertEqual(stored.status, "validated")
            self.assertTrue(stored.train_id)
            self.assertEqual(stored.train_size, 1)
            self.assertTrue(stored.validated_at)
            self.assertTrue(stored.validation_base_sha)
            self.assertEqual(stored.validation_sha, stored.deploy_sha)
            self.assertEqual(stored.validated_head_sha, git(repo, "rev-parse", "feature/a"))
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            self.assertIn("Merged feature/a", [event.message for event in events])
            self.assertIn("Running gate 2/2: marker", [event.message for event in events])
            running_gate = next(event for event in events if event.message == "Running gate 2/2: marker")
            self.assertEqual(running_gate.detail, config.gates[0].run)

    def test_same_named_tag_cannot_shadow_uncaptured_task_branch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            # The stale tag points at main while refs/heads/feature/a carries
            # the task. Enqueue deliberately omits head_sha, matching the
            # default CLI path without --capture-sha.
            git(repo, "tag", "feature/a", "refs/heads/main")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                result = GitRunner(config).process_batch(conn, [job], deploy=True)[0]
            finally:
                conn.close()

            self.assertEqual(result.status, "deployed")
            self.assertEqual(git(root / "remote.git", "show", "main:a.txt"), "a")

    def test_validated_batch_deploys_after_integration_ref_moves(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(
                    conn,
                    task="a",
                    branch="feature/a",
                    base_sha=git(repo, "rev-parse", "origin/main"),
                    head_sha=git(repo, "rev-parse", "feature/a"),
                )
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                (repo / "base-moved.txt").write_text("moved\n", encoding="utf-8")
                git(repo, "add", "base-moved.txt")
                git(repo, "commit", "-m", "move integration")
                git(repo, "push", "origin", "main")
                deployed = GitRunner(config).process_batch(
                    conn,
                    [validated],
                    deploy=True,
                    reuse_validated=True,
                )[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.status, "deployed")
            self.assertEqual(deployed.push_status, "succeeded")
            self.assertEqual(deployed.verify_status, "not_configured")
            self.assertEqual(deployed.reused_validation_sha, "")
            self.assertNotEqual(deployed.validation_base_sha, deployed.deploy_sha)
            self.assertEqual(git(root / "remote.git", "show", "main:a.txt"), "a")
            self.assertEqual(git(root / "remote.git", "show", "main:base-moved.txt"), "moved")
            self.assertEqual(marker.read_text(encoding="utf-8"), "xx")
            fallback = next(
                event
                for event in events
                if event.message == "Validated gates were not reused; rerunning all gates"
            )
            self.assertIn("integration ref moved", fallback.detail)

    def test_changed_branch_head_blocks_validated_train(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                git(repo, "switch", "feature/a")
                (repo / "changed.txt").write_text("changed\n", encoding="utf-8")
                git(repo, "add", "changed.txt")
                git(repo, "commit", "-m", "change after validation")
                git(repo, "switch", "main")
                result = GitRunner(config).process_batch(
                    conn,
                    [validated],
                    deploy=True,
                    reuse_validated=True,
                )[0]
            finally:
                conn.close()
            self.assertEqual(result.status, "blocked")
            self.assertIn("HEAD changed since validation", result.note)
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:a.txt")

    def test_changed_gate_policy_falls_back_to_full_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first_marker = root / "first-gate.txt"
            second_marker = root / "second-gate.txt"
            first_gate = f'{SHELL_PYTHON} -c "from pathlib import Path; Path(\'{py_path(first_marker)}\').write_text(\'x\')"'
            second_gate = f'{SHELL_PYTHON} -c "from pathlib import Path; Path(\'{py_path(second_marker)}\').write_text(\'y\')"'
            repo, _marker = make_demo_repo(root, gate_command=first_gate)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                config.config_path.write_text(
                    config.config_path.read_text(encoding="utf-8").replace(
                        first_gate, second_gate
                    ),
                    encoding="utf-8",
                )
                changed_config = load_config(repo=repo)
                deployed = GitRunner(changed_config).process_batch(
                    conn,
                    [validated],
                    deploy=True,
                    reuse_validated=True,
                )[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.reused_validation_sha, "")
            self.assertEqual(first_marker.read_text(encoding="utf-8"), "x")
            self.assertEqual(second_marker.read_text(encoding="utf-8"), "y")
            fallback = next(
                event
                for event in events
                if event.message == "Validated gates were not reused; rerunning all gates"
            )
            self.assertIn("gate or fingerprint policy changed", fallback.detail)

    def test_missing_validation_commit_falls_back_to_full_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                conn.execute(
                    "UPDATE deploy_queue SET validation_sha = ? WHERE id = ?",
                    ("f" * 40, validated.id),
                )
                conn.commit()
                validated = get_job(conn, validated.id)
                deployed = GitRunner(config).process_batch(
                    conn,
                    [validated],
                    deploy=True,
                    reuse_validated=True,
                )[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.reused_validation_sha, "")
            self.assertEqual(marker.read_text(encoding="utf-8"), "xx")
            fallback = next(
                event
                for event in events
                if event.message == "Validated gates were not reused; rerunning all gates"
            )
            self.assertIn("validation commit is missing", fallback.detail)

    def test_stale_validation_falls_back_to_full_gates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root, reuse_max_age_minutes=1)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                conn.execute(
                    "UPDATE deploy_queue SET validated_at = ? WHERE id = ?",
                    ("2000-01-01T00:00:00Z", validated.id),
                )
                conn.commit()
                validated = get_job(conn, validated.id)
                deployed = GitRunner(config).process_batch(
                    conn,
                    [validated],
                    deploy=True,
                    reuse_validated=True,
                )[0]
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(deployed.reused_validation_sha, "")
            self.assertEqual(marker.read_text(encoding="utf-8"), "xx")
            fallback = next(
                event
                for event in events
                if event.message == "Validated gates were not reused; rerunning all gates"
            )
            self.assertIn("older than the configured reuse age", fallback.detail)

    def test_mismatch_policy_can_fail_closed_without_rerunning_or_pushing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(
                root,
                reuse_max_age_minutes=1,
                reuse_on_mismatch="fail",
            )
            config = load_config(repo=repo)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            token = ""
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                validated = GitRunner(config).process_batch(conn, [job], deploy=False)[0]
                conn.execute(
                    "UPDATE deploy_queue SET validated_at = ? WHERE id = ?",
                    ("2000-01-01T00:00:00Z", validated.id),
                )
                conn.commit()
                claimed = claim_deploy_batch(
                    conn,
                    owner=owner,
                    train_id=validated.train_id,
                )
                token = claimed[0].claim_token
                result = GitRunner(config).process_batch(
                    conn,
                    claimed,
                    deploy=True,
                    owner=owner,
                    reuse_validated=True,
                )[0]
            finally:
                if token:
                    release_runner_lock(conn, owner=owner, token=token)
                conn.close()
            self.assertEqual(result.status, "blocked")
            self.assertIn("failed closed", result.note)
            self.assertEqual(marker.read_text(encoding="utf-8"), "x")
            with self.assertRaises(AssertionError):
                git(root / "remote.git", "show", "main:a.txt")

    def test_batch_refreshes_lease_while_holding_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            config = load_config(repo=repo)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            try:
                enqueue_job(conn, task="a", branch="feature/a")
                claimed = claim_all_queued(conn, owner=owner, ttl_minutes=-1)
                before = get_lock(conn)
                results = GitRunner(config).process_batch(
                    conn, claimed, deploy=False, owner=owner, ttl_minutes=30
                )
                after = get_lock(conn)
            finally:
                conn.close()
            self.assertEqual([result.status for result in results], ["validated"])
            self.assertIsNotNone(before)
            self.assertIsNotNone(after)
            # Lease advanced from expired (past) to valid (~30 min ahead).
            self.assertGreater(after.expires_at, before.expires_at)

    def test_long_gate_heartbeats_and_cooperatively_cancels(self) -> None:
        # ignore_cleanup_errors: cancelling mid-gate kills the gate subprocess
        # and tears down its integration worktree; on Windows the OS may still
        # hold those handles at TemporaryDirectory cleanup (WinError 32), the
        # same best-effort situation production handles via gc.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            root = Path(td)
            gate = f'{SHELL_PYTHON} -c "import time; time.sleep(10)"'
            repo, _marker = make_demo_repo(root, gate_command=gate)
            config = load_config(repo=repo)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            job = enqueue_job(conn, task="a", branch="feature/a")
            claimed = claim_all_queued(conn, owner=owner, ttl_minutes=1)
            token = claimed[0].claim_token
            conn.close()

            results: list = []
            errors: list[Exception] = []

            def run() -> None:
                worker_conn = connect(config.state.db)
                try:
                    results.extend(
                        GitRunner(config).process_batch(
                            worker_conn,
                            claimed,
                            deploy=False,
                            owner=owner,
                            ttl_minutes=1,
                        )
                    )
                except Exception as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
                finally:
                    worker_conn.close()

            worker = threading.Thread(target=run)
            worker.start()
            time.sleep(0.5)
            control = connect(config.state.db)
            active = get_job(control, job.id)
            self.assertTrue(active.log_path)
            self.assertTrue(Path(active.log_path).is_file())
            control.execute(
                "UPDATE locks SET expires_at = '2000-01-01T00:00:00Z' WHERE token = ?",
                (token,),
            )
            control.commit()
            time.sleep(1.5)
            self.assertGreater(get_lock(control).expires_at, "2000-01-01T00:00:00Z")
            cancel_job(control, job.id)
            control.close()
            worker.join(timeout=6)

            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual([result.status for result in results], ["canceled"])
            cleanup = connect(config.state.db)
            try:
                release_runner_lock(cleanup, owner=owner, token=token)
            finally:
                cleanup.close()


def add_branch(repo: Path, name: str, filename: str) -> None:
    git(repo, "switch", "-c", name, "main")
    (repo / filename).write_text(f"{name}\n", encoding="utf-8")
    git(repo, "add", filename)
    git(repo, "commit", "-m", name)
    git(repo, "switch", "main")


class BisectIsolationTests(unittest.TestCase):
    def test_semantic_conflict_classification_is_independent_of_batch_size(self) -> None:
        for filler_count in (0, 2):
            with self.subTest(filler_count=filler_count), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                gate = (
                    f"{SHELL_PYTHON} -c \"import sys, pathlib; "
                    "sys.exit(1 if (pathlib.Path('left.txt').exists() "
                    "and pathlib.Path('right.txt').exists()) else 0)\""
                )
                repo, _ = make_demo_repo(root, gate_command=gate)
                add_branch(repo, "agent/left", "left.txt")
                add_branch(repo, "agent/right", "right.txt")
                for index in range(filler_count):
                    add_branch(repo, f"agent/ok{index}", f"ok{index}.txt")
                config = load_config(repo=repo)
                conn = connect(config.state.db)
                try:
                    jobs = [
                        enqueue_job(conn, task=name, branch=f"agent/{name}")
                        for name in (
                            "left",
                            "right",
                            *(f"ok{index}" for index in range(filler_count)),
                        )
                    ]
                    GitRunner(config).process_batch(conn, jobs, deploy=False)
                    stored = {job.branch: get_job(conn, job.id) for job in jobs}
                    ids = {job.branch: job.id for job in jobs}
                finally:
                    conn.close()

                left, right = stored["agent/left"], stored["agent/right"]
                self.assertEqual(left.status, "blocked")
                self.assertEqual(right.status, "blocked")
                self.assertEqual(left.conflict_with, str(ids["agent/right"]))
                self.assertEqual(right.conflict_with, str(ids["agent/left"]))
                self.assertFalse(left.train_id)
                self.assertFalse(right.train_id)
                for index in range(filler_count):
                    survivor = stored[f"agent/ok{index}"]
                    self.assertEqual(survivor.status, "validated")
                    self.assertEqual(survivor.conflict_with, "")

    def test_two_job_semantic_conflict_never_pushes_in_direct_deploy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate = (
                f"{SHELL_PYTHON} -c \"import sys, pathlib; "
                "sys.exit(1 if (pathlib.Path('left.txt').exists() "
                "and pathlib.Path('right.txt').exists()) else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            add_branch(repo, "agent/left", "left.txt")
            add_branch(repo, "agent/right", "right.txt")
            config = load_config(repo=repo)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            token = ""
            try:
                for name in ("left", "right"):
                    enqueue_job(
                        conn,
                        task=name,
                        branch=f"agent/{name}",
                        auto_deploy=True,
                    )
                claimed = claim_all_queued(
                    conn,
                    owner=owner,
                    auto_only=True,
                    deploy=True,
                )
                token = claimed[0].claim_token
                runner = GitRunner(config)
                with patch.object(runner, "push_verified_head") as push:
                    runner.process_batch(conn, claimed, deploy=True, owner=owner)
                stored = {job.branch: get_job(conn, job.id) for job in claimed}
                ids = {job.branch: job.id for job in claimed}
            finally:
                if token:
                    release_runner_lock(conn, owner=owner, token=token)
                conn.close()

            push.assert_not_called()
            self.assertEqual(stored["agent/left"].status, "blocked")
            self.assertEqual(stored["agent/right"].status, "blocked")
            self.assertEqual(
                stored["agent/left"].conflict_with,
                str(ids["agent/right"]),
            )
            self.assertEqual(
                stored["agent/right"].conflict_with,
                str(ids["agent/left"]),
            )

    def test_exact_three_job_semantic_conflict_blocks_all_members(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate = (
                f"{SHELL_PYTHON} -c \"import sys, pathlib; p=pathlib.Path; "
                "sys.exit(1 if all(p(f'{name}.txt').exists() for name in "
                "('one', 'two', 'three')) else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            for name in ("one", "two", "three"):
                add_branch(repo, f"agent/{name}", f"{name}.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [
                    enqueue_job(conn, task=name, branch=f"agent/{name}")
                    for name in ("one", "two", "three")
                ]
                GitRunner(config).process_batch(conn, jobs, deploy=False)
                stored = {job.id: get_job(conn, job.id) for job in jobs}
            finally:
                conn.close()

            ids = {job.id for job in jobs}
            for job in jobs:
                result = stored[job.id]
                self.assertEqual(result.status, "blocked")
                self.assertEqual(
                    {int(value) for value in result.conflict_with.split(",")},
                    ids - {job.id},
                )
                self.assertFalse(result.train_id)

    def test_bisect_isolates_single_bad_job_and_revalidates_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate = (
                f"{SHELL_PYTHON} -c \"import sys, pathlib; "
                "sys.exit(1 if pathlib.Path('bad.txt').exists() else 0)\""
            )
            repo, _ = make_demo_repo(
                root, gate_command=gate, gate_paths=("bad.txt",)
            )
            add_branch(repo, "agent/bad", "bad.txt")
            add_branch(repo, "agent/b", "b.txt")
            add_branch(repo, "agent/c", "c.txt")
            add_branch(repo, "agent/d", "d.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [enqueue_job(conn, task="a", branch="feature/a")]
                jobs.extend(
                    enqueue_job(conn, task=name, branch=f"agent/{name}")
                    for name in ("bad", "b", "c", "d")
                )
                results = GitRunner(config).process_batch(conn, jobs, deploy=False)
                stored = {job.id: get_job(conn, job.id) for job in jobs}
                events = list_run_events(conn)
            finally:
                conn.close()
            by_branch = {job.branch: stored[job.id] for job in jobs}
            self.assertEqual(by_branch["agent/bad"].status, "failed")
            self.assertIn("bisect isolation", by_branch["agent/bad"].note)
            self.assertEqual(by_branch["agent/bad"].conflict_with, "")
            for branch in ("feature/a", "agent/b", "agent/c", "agent/d"):
                self.assertEqual(by_branch[branch].status, "validated", branch)
                self.assertEqual(by_branch[branch].conflict_with, "")
                self.assertEqual(by_branch[branch].train_size, 4, branch)
            self.assertEqual(len(results), 5)
            messages = [event.message for event in events]
            self.assertIn(
                "Train gate failed; probing 5 jobs for semantic conflicts",
                messages,
            )
            self.assertIn("Bisect isolation complete: 4 job(s) rejoin the train", messages)
            self.assertIn("Skipped gate 2/2: marker", messages)

    def test_bisect_reports_semantic_conflict_pair_with_conflict_with(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate = (
                f"{SHELL_PYTHON} -c \"import sys, pathlib; "
                "sys.exit(1 if (pathlib.Path('left.txt').exists() "
                "and pathlib.Path('right.txt').exists()) else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            add_branch(repo, "agent/left", "left.txt")
            add_branch(repo, "agent/right", "right.txt")
            add_branch(repo, "agent/ok1", "ok1.txt")
            add_branch(repo, "agent/ok2", "ok2.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [
                    enqueue_job(conn, task=name, branch=f"agent/{name}")
                    for name in ("left", "right", "ok1", "ok2")
                ]
                results = GitRunner(config).process_batch(conn, jobs, deploy=False)
                stored = {job.branch: get_job(conn, job.id) for job in jobs}
                ids = {job.branch: job.id for job in jobs}
            finally:
                conn.close()
            left, right = stored["agent/left"], stored["agent/right"]
            self.assertEqual(left.status, "blocked")
            self.assertEqual(right.status, "blocked")
            self.assertEqual(left.conflict_with, str(ids["agent/right"]))
            self.assertEqual(right.conflict_with, str(ids["agent/left"]))
            self.assertIn("semantic conflict", left.note)
            self.assertIn("agent/right", left.note)
            self.assertIn("agent/left", right.note)
            for branch in ("agent/ok1", "agent/ok2"):
                self.assertEqual(stored[branch].status, "validated", branch)
                self.assertEqual(stored[branch].conflict_with, "")
            self.assertEqual(len(results), 4)

    def test_bisect_reports_three_way_conflict_and_frees_innocent_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate = (
                f"{SHELL_PYTHON} -c \"import sys, pathlib; "
                "sys.exit(1 if (pathlib.Path('t1.txt').exists() "
                "and pathlib.Path('t3.txt').exists() "
                "and pathlib.Path('t5.txt').exists()) else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            for name in ("t1", "t2", "t3", "t4", "t5"):
                add_branch(repo, f"agent/{name}", f"{name}.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [
                    enqueue_job(conn, task=name, branch=f"agent/{name}")
                    for name in ("t1", "t2", "t3", "t4", "t5")
                ]
                GitRunner(config).process_batch(conn, jobs, deploy=False)
                stored = {job.branch: get_job(conn, job.id) for job in jobs}
                ids = {job.branch: job.id for job in jobs}
            finally:
                conn.close()
            conflicted = ("agent/t1", "agent/t3", "agent/t5")
            for branch in conflicted:
                self.assertEqual(stored[branch].status, "blocked", branch)
                partners = {
                    int(part) for part in stored[branch].conflict_with.split(",")
                }
                expected = {ids[other] for other in conflicted if other != branch}
                self.assertEqual(partners, expected, branch)
                self.assertIn("semantic conflict", stored[branch].note)
            for branch in ("agent/t2", "agent/t4"):
                self.assertEqual(stored[branch].status, "validated", branch)
                self.assertEqual(stored[branch].conflict_with, "")

    def test_bisect_masked_failure_does_not_blame_innocent_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # bad fails alone but is masked by fix; the real conflict is x+y.
            gate = (
                f"{SHELL_PYTHON} -c \"import sys, pathlib; e=pathlib.Path; "
                "sys.exit(1 if ((e('bad.txt').exists() and not e('fix.txt').exists()) "
                "or (e('x.txt').exists() and e('y.txt').exists())) else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            for name in ("x", "l2", "bad", "fix", "y"):
                add_branch(repo, f"agent/{name}", f"{name}.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [
                    enqueue_job(conn, task=name, branch=f"agent/{name}")
                    for name in ("x", "l2", "bad", "fix", "y")
                ]
                GitRunner(config).process_batch(conn, jobs, deploy=False)
                stored = {job.branch: get_job(conn, job.id) for job in jobs}
                ids = {job.branch: job.id for job in jobs}
            finally:
                conn.close()
            self.assertEqual(stored["agent/x"].status, "blocked")
            self.assertEqual(stored["agent/y"].status, "blocked")
            self.assertEqual(stored["agent/x"].conflict_with, str(ids["agent/y"]))
            self.assertEqual(stored["agent/y"].conflict_with, str(ids["agent/x"]))
            # bad is masked by fix in the surviving combination, which
            # genuinely passes — nobody gets falsely blamed.
            for branch in ("agent/l2", "agent/bad", "agent/fix"):
                self.assertEqual(stored[branch].status, "validated", branch)
                self.assertEqual(stored[branch].conflict_with, "")

    def test_bisect_falls_back_to_linear_when_failure_does_not_reproduce(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            counter = root / "count.txt"
            gate = (
                f"{SHELL_PYTHON} -c \"import pathlib, sys; "
                f"p = pathlib.Path('{py_path(counter)}'); "
                "n = (int(p.read_text()) + 1) if p.exists() else 1; "
                "p.write_text(str(n)); sys.exit(1 if n == 1 else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            for name in ("b", "c", "d"):
                add_branch(repo, f"agent/{name}", f"{name}.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [enqueue_job(conn, task="a", branch="feature/a")]
                jobs.extend(
                    enqueue_job(conn, task=name, branch=f"agent/{name}")
                    for name in ("b", "c", "d")
                )
                GitRunner(config).process_batch(conn, jobs, deploy=False)
                stored = [get_job(conn, job.id) for job in jobs]
                events = list_run_events(conn, limit=200)
            finally:
                conn.close()
            self.assertEqual([job.status for job in stored], ["validated"] * 4)
            self.assertEqual([job.conflict_with for job in stored], [""] * 4)
            messages = [event.message for event in events]
            self.assertIn("Bisect inconclusive; isolating jobs one-by-one", messages)

    def test_bisect_fallback_stops_after_ambiguous_push(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            counter = root / "count.txt"
            gate = (
                f"{SHELL_PYTHON} -c \"import pathlib, sys; "
                f"p = pathlib.Path('{py_path(counter)}'); "
                "n = (int(p.read_text()) + 1) if p.exists() else 1; "
                "p.write_text(str(n)); sys.exit(1 if n == 1 else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            for name in ("b", "c", "d"):
                add_branch(repo, f"agent/{name}", f"{name}.txt")
            config = load_config(repo=repo)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            token = ""
            try:
                for task, branch in (
                    ("a", "feature/a"),
                    ("b", "agent/b"),
                    ("c", "agent/c"),
                    ("d", "agent/d"),
                ):
                    enqueue_job(conn, task=task, branch=branch)
                claimed = claim_deploy_batch(conn, owner=owner)
                token = claimed[0].claim_token
                failure = CommandFailed(
                    ["git", "push"], 1, stderr="transport timed out"
                )
                runner = GitRunner(config)
                with patch.object(
                    runner, "push_verified_head", side_effect=failure
                ) as push:
                    runner.process_batch(conn, claimed, deploy=True, owner=owner)
                stored = [get_job(conn, job.id) for job in claimed]
            finally:
                if token:
                    release_runner_lock(conn, owner=owner, token=token)
                conn.close()

            self.assertEqual(push.call_count, 1)
            self.assertEqual(
                [job.status for job in stored],
                ["needs_reconcile", "queued", "queued", "queued"],
            )

    def test_small_train_isolates_single_failure_and_revalidates_survivors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate = (
                f"{SHELL_PYTHON} -c \"import sys, pathlib; "
                "sys.exit(1 if pathlib.Path('bad.txt').exists() else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            add_branch(repo, "agent/bad", "bad.txt")
            add_branch(repo, "agent/b", "b.txt")
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [enqueue_job(conn, task="a", branch="feature/a")]
                jobs.append(enqueue_job(conn, task="bad", branch="agent/bad"))
                jobs.append(enqueue_job(conn, task="b", branch="agent/b"))
                GitRunner(config).process_batch(conn, jobs, deploy=False)
                stored = {job.branch: get_job(conn, job.id) for job in jobs}
                events = list_run_events(conn)
            finally:
                conn.close()
            self.assertEqual(stored["agent/bad"].status, "failed")
            self.assertEqual(stored["feature/a"].status, "validated")
            self.assertEqual(stored["agent/b"].status, "validated")
            messages = [event.message for event in events]
            self.assertIn(
                "Train gate failed; probing 3 jobs for semantic conflicts",
                messages,
            )

    def test_semantic_conflicts_remain_blocked_when_survivor_push_is_ambiguous(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            gate = (
                f"{SHELL_PYTHON} -c \"import pathlib, sys; p=pathlib.Path; "
                "sys.exit(1 if sum(p(name).exists() for name in "
                "('a.txt', 'b.txt', 'c.txt')) > 1 else 0)\""
            )
            repo, _ = make_demo_repo(root, gate_command=gate)
            add_branch(repo, "agent/b", "b.txt")
            add_branch(repo, "agent/c", "c.txt")
            config = load_config(repo=repo)
            owner = f"runner:{os.getpid()}"
            conn = connect(config.state.db)
            token = ""
            try:
                for task, branch in (
                    ("a", "feature/a"),
                    ("b", "agent/b"),
                    ("c", "agent/c"),
                ):
                    enqueue_job(conn, task=task, branch=branch)
                claimed = claim_deploy_batch(conn, owner=owner)
                token = claimed[0].claim_token
                failure = CommandFailed(
                    ["git", "push"], 1, stderr="transport timed out"
                )
                runner = GitRunner(config)
                with patch.object(
                    runner, "push_verified_head", side_effect=failure
                ) as push:
                    results = runner.process_batch(
                        conn, claimed, deploy=True, owner=owner
                    )
                stored = [get_job(conn, job.id) for job in claimed]
            finally:
                if token:
                    release_runner_lock(conn, owner=owner, token=token)
                conn.close()

            self.assertEqual(push.call_count, 1)
            self.assertEqual(
                [job.status for job in stored],
                ["needs_reconcile", "blocked", "blocked"],
            )
            self.assertEqual(stored[1].conflict_with, str(stored[2].id))
            self.assertEqual(stored[2].conflict_with, str(stored[1].id))
            self.assertEqual(
                [job.status for job in results],
                ["blocked", "blocked", "needs_reconcile"],
            )
            self.assertIn("reconcile", stored[0].note)
            self.assertTrue(all("semantic conflict" in job.note for job in stored[1:]))


class PushRejectionTests(unittest.TestCase):
    def test_classifier_distinguishes_permission_from_other_failures(self) -> None:
        from mergetrain.git_ops import is_push_rejection

        self.assertTrue(is_push_rejection("remote: error: GH006 Protected branch update failed"))
        self.assertTrue(is_push_rejection("! [remote rejected] main -> main (protected branch hook declined)"))
        self.assertTrue(is_push_rejection("! [rejected] main -> main (fetch first)"))
        self.assertTrue(is_push_rejection("! [rejected] main -> main (non-fast-forward)"))
        self.assertTrue(is_push_rejection("remote: Permission to org/repo denied to user."))
        self.assertFalse(is_push_rejection("remote: Changes must be made through a pull request."))
        self.assertFalse(is_push_rejection("remote: advice: use a pull request after reconnecting"))
        self.assertFalse(is_push_rejection("fatal: could not read from remote repository"))

    def test_inspect_categorizes_a_push_blocked_job_as_push_rejected(self) -> None:
        from mergetrain.models import Job
        from mergetrain.observability import job_outcome

        job = Job(
            id=1, task="a", branch="feature/a", status="blocked",
            push_status="failed",
            note="remote rejected the push (protected branch, required pull request)",
        )
        self.assertEqual(job_outcome(job)["category"], "push_rejected")

    def test_protected_branch_push_lands_blocked_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            # A pre-receive hook that rejects with a protected-branch message,
            # so the real push path exercises the rejection classifier.
            hook = root / "remote.git" / "hooks" / "pre-receive"
            hook.write_text(
                "#!/bin/sh\necho 'remote: error: GH006 Protected branch update failed' 1>&2\nexit 1\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="a", branch="feature/a")
                result = GitRunner(config).process_batch(conn, [job], deploy=True)[0]
            finally:
                conn.close()
            # Not `failed` (which means "bad code, rebase") — this is a repo
            # policy issue the operator must fix, so the job is blocked.
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.push_status, "failed")
            self.assertIn("rejected the push", result.note)


class GcWorktreeGuardTests(unittest.TestCase):
    def test_gc_protects_configured_persistent_workspace_and_removes_it_when_disabled(
        self,
    ) -> None:
        from mergetrain.git_ops import apply_gc, find_worktree_gc_candidates

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            enable_persistent_validation_workspace(repo)
            config = load_config(repo=repo)
            workspace = config.validation_worktree_path
            workspace.mkdir(parents=True)
            marker = (
                config.state.worktree_root
                / f".{config.project.name}-validation-workspace.json"
            )
            marker.write_text("{}\n", encoding="utf-8")

            configured = find_worktree_gc_candidates(config)

            self.assertEqual(
                configured,
                [
                    {
                        "path": str(workspace),
                        "reason": "configured persistent validation workspace, skipped",
                        "protected": True,
                    }
                ],
            )
            config_path = repo / ".mergetrain.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "mode: persistent", "mode: ephemeral"
                ),
                encoding="utf-8",
            )
            disabled = load_config(repo=repo)
            preview = find_worktree_gc_candidates(disabled)
            result = apply_gc(disabled)

            self.assertEqual(
                preview[0]["reason"], "disabled persistent validation workspace"
            )
            self.assertFalse(workspace.exists())
            self.assertFalse(marker.exists())
            self.assertEqual(result["removed_worktrees"], preview)

    def test_gc_never_removes_a_live_runners_worktree(self) -> None:
        # Blocker: gc --apply force-removed the worktree a running deploy was
        # merging/gating inside. A live runner's worktree must be protected.
        from mergetrain.git_ops import apply_gc, find_worktree_gc_candidates

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            wt_root = config.state.worktree_root
            wt_root.mkdir(parents=True, exist_ok=True)
            live = wt_root / f"{config.project.name}-mergetrain-1-abc"
            orphan = wt_root / f"{config.project.name}-mergetrain-2-def"
            live.mkdir()
            orphan.mkdir()

            # The live worktree is reported as protected in the candidate list...
            cands = find_worktree_gc_candidates(config, protect=[str(live)])
            protected = [c for c in cands if c.get("protected")]
            self.assertEqual([c["path"] for c in protected], [str(live)])

            # ...and apply never removes it, while the orphan is swept.
            apply_gc(config, protect=[str(live)])
            self.assertTrue(live.is_dir(), "live runner worktree was destroyed")
            self.assertFalse(orphan.exists(), "orphan worktree should be gc'd")

    def test_gc_rechecks_a_runner_that_started_after_the_snapshot(self) -> None:
        # #84 defect 5: the protect list is a snapshot taken before apply_gc
        # runs. A runner that acquires the lock AFTER it is built is absent from
        # protect — but a per-deletion recheck of the live lock still spares its
        # worktree, while a genuine orphan is still swept.
        from mergetrain.git_ops import apply_gc

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)
            config = load_config(repo=repo)
            wt_root = config.state.worktree_root
            wt_root.mkdir(parents=True, exist_ok=True)
            started_late = wt_root / f"{config.project.name}-mergetrain-9-late"
            orphan = wt_root / f"{config.project.name}-mergetrain-2-def"
            started_late.mkdir()
            orphan.mkdir()

            # protect is empty (the snapshot predates the runner), but the live
            # lock now points at started_late.
            result = apply_gc(
                config,
                protect=[],
                live_worktree_now=lambda: str(started_late),
            )
            self.assertTrue(
                started_late.is_dir(),
                "a runner that started after the snapshot was destroyed",
            )
            self.assertFalse(orphan.exists(), "the genuine orphan should still be gc'd")
            self.assertNotIn(
                str(started_late),
                [c["path"] for c in result["removed_worktrees"]],
            )


class MergeConflictTests(unittest.TestCase):
    """Real git-level merge conflicts during assembly (the BisectIsolation
    suite only fakes semantic conflicts via gate exit codes)."""

    def test_real_merge_conflict_blocks_the_job_and_pushes_nothing(self) -> None:
        from mergetrain.observability import job_outcome

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, marker = make_demo_repo(root)
            # the branch edits app.txt line 1...
            git(repo, "switch", "-c", "agent/x", "main")
            (repo / "app.txt").write_text("x-change\n", encoding="utf-8")
            git(repo, "add", "app.txt")
            git(repo, "commit", "-m", "x")
            # ...and the integration ref moves to a conflicting state on the same line
            git(repo, "switch", "main")
            (repo / "app.txt").write_text("main-change\n", encoding="utf-8")
            git(repo, "add", "app.txt")
            git(repo, "commit", "-m", "move main")
            git(repo, "push", "origin", "main")

            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                job = enqueue_job(conn, task="x", branch="agent/x")
                result = GitRunner(config).process_batch(conn, [job], deploy=True)[0]
                stored = get_job(conn, job.id)
                pending = git(
                    repo, "for-each-ref", "--format=%(refname)", "refs/mergetrain/pending/"
                )
            finally:
                conn.close()

            self.assertEqual(stored.status, "blocked")
            self.assertIn("conflict", stored.note.lower())
            self.assertEqual(job_outcome(stored)["category"], "merge_conflict")
            self.assertEqual(result.push_status, "not_run")
            # nothing shipped: the remote still holds the integration change (not
            # the branch's), no write-ahead marker was written, no gate ran.
            self.assertEqual(git(root / "remote.git", "show", "main:app.txt"), "main-change")
            self.assertEqual(pending, "")
            self.assertFalse(marker.exists())

    def test_merge_conflict_isolates_one_job_and_siblings_still_deploy(self) -> None:
        from mergetrain.observability import job_outcome

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _marker = make_demo_repo(root)
            # two branches, each editing the SAME line of app.txt differently
            for name, content in (("agent/x", "x-change\n"), ("agent/y", "y-change\n")):
                git(repo, "switch", "-c", name, "main")
                (repo / "app.txt").write_text(content, encoding="utf-8")
                git(repo, "add", "app.txt")
                git(repo, "commit", "-m", name)
                git(repo, "switch", "main")

            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [
                    enqueue_job(conn, task="x", branch="agent/x"),
                    enqueue_job(conn, task="y", branch="agent/y"),
                ]
                GitRunner(config).process_batch(conn, jobs, deploy=True)
                stored = {job.branch: get_job(conn, job.id) for job in jobs}
            finally:
                conn.close()

            # assembly merges in list order: agent/x lands, agent/y conflicts and
            # is isolated — the innocent sibling still gates once and deploys.
            self.assertEqual(stored["agent/x"].status, "deployed")
            self.assertEqual(stored["agent/x"].push_status, "succeeded")
            self.assertEqual(stored["agent/y"].status, "blocked")
            self.assertEqual(job_outcome(stored["agent/y"])["category"], "merge_conflict")
            # only the sibling's change reached the remote
            self.assertEqual(git(root / "remote.git", "show", "main:app.txt"), "x-change")

    def test_successful_merge_that_dirties_worktree_is_reset_and_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, _ = make_demo_repo(root)

            # The custom driver reports a successful content merge but mutates
            # another tracked file after Git built the merge index. The merge
            # commit therefore succeeds while the integration worktree is dirty.
            (repo / ".gitattributes").write_text(
                "app.txt merge=dirty\n", encoding="utf-8"
            )
            (repo / "sentinel.txt").write_text("clean\n", encoding="utf-8")
            git(repo, "add", ".gitattributes", "sentinel.txt")
            git(repo, "commit", "-m", "configure dirty merge driver")
            git(repo, "push", "origin", "main")
            git(repo, "config", "merge.dirty.driver", "echo dirty > sentinel.txt")

            git(repo, "switch", "-c", "agent/dirty", "main")
            (repo / "app.txt").write_text("dirty-branch\n", encoding="utf-8")
            git(repo, "add", "app.txt")
            git(repo, "commit", "-m", "dirty branch")
            git(repo, "switch", "main")
            (repo / "app.txt").write_text("main-moved\n", encoding="utf-8")
            git(repo, "add", "app.txt")
            git(repo, "commit", "-m", "move main")
            git(repo, "push", "origin", "main")
            add_branch(repo, "agent/clean", "clean.txt")

            config = load_config(repo=repo)
            conn = connect(config.state.db)
            try:
                jobs = [
                    enqueue_job(conn, task="dirty", branch="agent/dirty"),
                    enqueue_job(conn, task="clean", branch="agent/clean"),
                ]
                GitRunner(config).process_batch(conn, jobs, deploy=True)
                stored = {job.branch: get_job(conn, job.id) for job in jobs}
            finally:
                conn.close()

            self.assertEqual(stored["agent/dirty"].status, "blocked")
            self.assertIn("dirty after merge", stored["agent/dirty"].note)
            self.assertEqual(stored["agent/clean"].status, "deployed")
            self.assertEqual(
                git(root / "remote.git", "show", "main:app.txt"), "main-moved"
            )
            self.assertEqual(
                git(root / "remote.git", "show", "main:sentinel.txt"), "clean"
            )
            self.assertEqual(
                git(root / "remote.git", "show", "main:clean.txt"), "agent/clean"
            )


class JobOutcomeCategoryTests(unittest.TestCase):
    def test_gate_named_push_is_not_mislabeled_push_failed(self) -> None:
        from mergetrain.models import Job
        from mergetrain.observability import job_outcome

        # A gate failure whose note merely contains "push" (e.g. a gate called
        # "no-force-push") ran before any push — push_status stays not_run, so it
        # must categorize as gate_failed, not push_failed, which would steer
        # remediation toward branch-protection instead of the failing gate.
        gate = Job(
            id=1, task="a", branch="agent/a", status="failed",
            push_status="not_run", note="gate 'no-force-push' failed: exit 1",
        )
        self.assertEqual(job_outcome(gate)["category"], "gate_failed")

        # A genuine push failure is still push_failed — via the structured field.
        pushed = Job(
            id=2, task="a", branch="agent/b", status="failed",
            push_status="failed", note="remote rejected the update",
        )
        self.assertEqual(job_outcome(pushed)["category"], "push_failed")


if __name__ == "__main__":
    unittest.main()
