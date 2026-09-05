"""Local-only real CLI checks of one-shot safety and eventual execution."""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("cli", type=Path)
    args = parser.parse_args()
    args.root.mkdir()
    args.evidence.mkdir(parents=True)
    records, outcomes = [], {}
    children = []

    def run(command, cwd, check=True):
        started = time.time()
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=30)
        records.append(
            {
                "command": command,
                "cwd": str(cwd),
                "started": started,
                "seconds": time.time() - started,
                "code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if check and result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result

    def git(repo, *command):
        return run(["git", *command], repo).stdout.strip()

    def mt(repo, *command):
        return run([str(args.cli), "--repo", str(repo), *command], repo)

    def fixture(name):
        case = args.root / name
        case.mkdir()
        repo = case / "repo"
        repo.mkdir()
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.name", "Local lifecycle test")
        git(repo, "config", "user.email", "local-test@example.invalid")
        gate = case / "gate.py"
        gate.write_text(
            "import pathlib,time\np=pathlib.Path("
            + repr(str(case))
            + ")\n(p/'entered').touch()\nend=time.monotonic()+20\nwhile not (p/'release').exists():\n if time.monotonic()>end: raise TimeoutError('bounded gate')\n time.sleep(.05)\n"
        )
        (repo / "base.txt").write_text("base\n")
        config = {
            "version": 2,
            "project": {"name": "oneshot-" + name},
            "git": {"remote": "origin", "integration_branch": "main", "push_refs": ["main"]},
            "gates": [{"name": "bounded-gate", "run": sys.executable + " " + str(gate)}],
            "deploy": {"verify": [{"name": "local-verify", "run": "git diff --check"}]},
        }
        (repo / ".mergetrain.yaml").write_text(json.dumps(config))
        git(repo, "add", "base.txt", ".mergetrain.yaml")
        git(repo, "commit", "-m", "Local fixture base")
        remote = case / "remote.git"
        git(repo, "clone", "--bare", str(repo), str(remote))
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "fetch", "origin")
        for task in ["a", "b"]:
            wt = case / task
            git(repo, "worktree", "add", "-b", "codex/" + task, str(wt), "main")
            (wt / (task + ".txt")).write_text(task + "\n")
            git(wt, "add", task + ".txt")
            git(wt, "commit", "-m", "Add " + task)
        mt(repo, "status", "--json")
        return case, repo, remote

    def rows(repo):
        with sqlite3.connect(
            "file:" + str(repo / ".mergetrain/queue.sqlite") + "?mode=ro", uri=True
        ) as conn:
            return conn.execute("SELECT id,status FROM deploy_queue ORDER BY id").fetchall()

    def start(repo, name):
        log = (args.evidence / (name + ".log")).open("w")
        child = subprocess.Popen(
            [str(args.cli), "--repo", str(repo), "daemon", "--once"],
            cwd=repo,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        children.append((child, log))
        return child

    def entered(case, child):
        deadline = time.monotonic() + 10
        while not (case / "entered").exists():
            if child.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("runner did not reach gate")
            time.sleep(0.05)

    def enqueue(repo, task):
        mt(repo, "enqueue", "--task", task, "--branch", "codex/" + task, "--auto")

    def remote_files(repo, remote):
        return git(repo, "--git-dir", str(remote), "ls-tree", "--name-only", "main").splitlines()

    try:
        case, repo, remote = fixture("sequential")
        (case / "release").touch()
        for task in ["a", "b"]:
            enqueue(repo, task)
            mt(repo, "daemon", "--once")
        assert rows(repo) == [(1, "deployed"), (2, "deployed")]
        assert {"a.txt", "b.txt"} <= set(remote_files(repo, remote))
        outcomes["sequential"] = "both deployed"

        case, repo, remote = fixture("overlap")
        enqueue(repo, "a")
        first = start(repo, "overlap-first")
        entered(case, first)
        enqueue(repo, "b")
        second = mt(repo, "daemon", "--once")
        during = rows(repo)
        (case / "release").touch()
        first.wait(timeout=15)
        after = rows(repo)
        files = remote_files(repo, remote)
        assert after == [(1, "deployed"), (2, "queued")], after
        assert "a.txt" in files and "b.txt" not in files
        outcomes["overlap"] = {
            "second_exit": second.returncode,
            "second_output": second.stdout,
            "during": during,
            "after_both_exited": after,
            "no_b_without_another_trigger": True,
        }
        # A bounded coordinator explicitly checks its known tasks and runs again.
        mt(repo, "status", "--json")
        mt(repo, "daemon", "--once")
        assert rows(repo) == [(1, "deployed"), (2, "deployed")]
        assert {"a.txt", "b.txt"} <= set(remote_files(repo, remote))
        outcomes["coordinator_recheck"] = "pending B deployed with one explicit recheck/run"

        case, repo, remote = fixture("prepush-crash")
        enqueue(repo, "a")
        child = start(repo, "crash-first")
        entered(case, child)
        before = rows(repo)
        # Kill only this fixture's own process group before its bounded gate ends.
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
        assert "a.txt" not in remote_files(repo, remote)
        stranded = rows(repo)
        (case / "release").touch()
        recovery = mt(repo, "daemon", "--once")
        recovered = rows(repo)
        outcomes["prepush_crash"] = {
            "before": before,
            "after_kill": stranded,
            "after_explicit_restart": recovered,
            "output": recovery.stdout,
        }
        assert recovered == [(1, "deployed")], recovered
        assert "a.txt" in remote_files(repo, remote)

        case, repo, remote = fixture("approval-boundary")
        (case / "release").touch()
        mt(repo, "enqueue", "--task", "a", "--branch", "codex/a")
        mt(repo, "daemon", "--once")
        assert rows(repo) == [(1, "queued")]
        assert "a.txt" not in remote_files(repo, remote)
        mt(repo, "daemon", "--once", "--validate-only")
        assert rows(repo) == [(1, "validated")]
        assert "a.txt" not in remote_files(repo, remote)
        outcomes["approval_boundary"] = "manual job not deployed; validate-only remains unpushed"
        print(json.dumps(outcomes, indent=2), flush=True)
    finally:
        for child, log in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
            log.close()
        (args.evidence / "results.json").write_text(
            json.dumps({"outcomes": outcomes, "commands": records}, indent=2) + "\n"
        )


if __name__ == "__main__":
    main()
