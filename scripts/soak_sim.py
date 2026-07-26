#!/usr/bin/env python3
"""Soak load generator for mergetrain issue #179.

Organic dogfooding of one repo over two weeks is too slow and too narrow a
usage pattern to gather the soak's evidence-based exit criteria quickly:

  - >= 20 landed trains
  - at least one through a recovery path (blocked / needs_reconcile / an
    operator intervention)
  - one of those on a real repository that is not mergetrain itself
  - every unplanned operator intervention (unlock/reconcile/dismiss/retry/
    manual git surgery) logged and classified as a bug or a docs gap; the
    harness's planned retry/dismiss/recover actions are labeled ``expected``
  - exactly one deliberate crash-recovery exercise against a real remote
  - a readout via `mergetrain stats --json`

This script replaces the *pace*, not the realism: every branch, commit,
push, gate run, and recovery command it drives is real, against a real
throwaway GitHub repo (see --repo). It is a dev/ops tool, not part of the
installed mergetrain package -- same category as scripts/e2e.sh.

Safety contract:

* The target must be a GitHub repository whose exact ``owner/name`` is supplied
  through ``--confirm-repo``.
* ``origin/main`` must contain a committed ``.mergetrain-soak-target.json``:

      {
        "version": 1,
        "purpose": "mergetrain-soak-target",
        "repository": "owner/name"
      }

* The target worktree must be clean and operationally idle before a scenario
  starts. The script then hard-resets only that confirmed throwaway target's
  local ``main`` to ``origin/main``.
* A first run must supply an explicit ``--baseline``. Later runs reuse the
  persisted baseline and a run-specific namespace from ``--state``.
* The invoked mergetrain must match ``--expected-version`` and be an installed
  wheel unless ``--allow-non-wheel`` is explicitly supplied for harness
  development.

Usage:
    python3 scripts/soak_sim.py --repo /path/to/mergetrain-soak-target
      --confirm-repo owner/mergetrain-soak-target --expected-version 0.9.0
      --baseline 2026-07-26T01:56:04Z

Run once with ``--skip-crash --target-landed 6`` first to build confidence in
the non-destructive scenarios before letting it SIGKILL a real push. A smoke
run may succeed while the report still (correctly) leaves the overall
crash-recovery criterion unchecked.

Resumability: the landed-train count is always read with the persisted
``--baseline``; recovery and crash state survive across invocations; and every
generated branch/module uses a persisted random namespace. A process
interruption in the middle of a scenario still requires operator triage before
the next run: startup refuses a live/attention queue rather than guessing how
to continue it. Record that intervention with ``--record-intervention``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MERGETRAIN_REPO = Path(__file__).resolve().parent.parent
PROJECT_REPO = "yongjip/mergetrain"
SOAK_SENTINEL = ".mergetrain-soak-target.json"
SOAK_STATE_VERSION = 1
SOAK_PURPOSE = "mergetrain-soak-target"
MIN_LANDED_TRAINS = 20
ATTENTION_COUNTS = (
    "queued",
    "validated",
    "in_progress",
    "needs_reconcile",
    "blocked",
    "failed",
    "deployed_verify_unknown",
)
INTERVENTION_CLASSIFICATIONS = {"expected", "bug", "docs_gap"}
CRASH_STATUSES = {
    "pending",
    "attempting",
    "completed",
    "failed",
    "needs_triage",
}

GATEFAIL_SNIPPET = "\nimport os\n\n\ndef unused_import_marker():\n    return 1\n"
_CREDENTIAL_URL_RE = re.compile(r"(?P<scheme>https?://)[^/@\s]+@")
_GITHUB_SCP_RE = re.compile(
    r"^(?:git@)?github\.com:(?P<path>[^/\s]+/[^/\s]+?)(?:\.git)?$",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SoakError(RuntimeError):
    """An outcome the scenario schedule did not plan for. Always fatal."""


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _CREDENTIAL_URL_RE.sub(r"\g<scheme>***@", value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


def _valid_intervention(intervention: dict[str, Any]) -> bool:
    classification = intervention.get("classification")
    planned = intervention.get("planned") is True
    reason = str(intervention.get("reason", "")).strip()
    issue_url = str(intervention.get("issue_url", "")).strip()
    if classification not in INTERVENTION_CLASSIFICATIONS or not reason:
        return False
    if planned:
        return classification == "expected"
    return classification in {"bug", "docs_gap"} and _valid_issue_url(issue_url)


def _valid_issue_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


@dataclass
class Logger:
    log_path: Path
    interventions: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path.exists():
            for line_number, line in enumerate(
                self.log_path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SoakError(
                        f"{self.log_path}:{line_number}: invalid JSONL: {exc}"
                    ) from exc
                if record.get("intervention"):
                    self.interventions.append(record)
        self._fh = self.log_path.open("a", encoding="utf-8")

    def event(self, **fields: Any) -> None:
        record = _redact({"ts": now_iso(), **fields})
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()
        if record.get("intervention"):
            self.interventions.append(record)

    def note(self, scenario: str, message: str) -> None:
        print(f"[{scenario}] {message}")
        self.event(event="note", scenario=scenario, message=message)

    def close(self) -> None:
        self._fh.close()

    def untriaged_interventions(self) -> list[dict[str, Any]]:
        return [
            record
            for record in self.interventions
            if not _valid_intervention(record.get("intervention", {}))
        ]


def run(cmd: list[str], *, cwd: Path, timeout: int = 120, check: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise SoakError(
            f"command failed rc={proc.returncode}: {' '.join(cmd)}\n{proc.stderr}"
        )
    return proc


def normalize_baseline(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SoakError("--baseline must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SoakError("--baseline must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def github_repo_slug(origin: str) -> str:
    """Return a credential-free GitHub owner/name or fail closed."""
    matched = _GITHUB_SCP_RE.fullmatch(origin.strip())
    if matched is not None:
        path = matched.group("path")
    else:
        parsed = urlsplit(origin.strip())
        if parsed.scheme not in {"https", "ssh"} or parsed.hostname != "github.com":
            raise SoakError(
                "origin must be an https://github.com/owner/name.git or "
                "git@github.com:owner/name.git remote"
            )
        if parsed.scheme == "https" and parsed.username is not None:
            raise SoakError(
                "credential-bearing HTTPS origins are refused; use a credential "
                "helper with a clean https://github.com/owner/name.git URL"
            )
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if (
        len(parts) != 2
        or not all(parts)
        or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)
    ):
        raise SoakError("origin must identify exactly one GitHub owner/name repository")
    return f"{parts[0]}/{parts[1]}".lower()


def validate_sentinel(document: Any, *, repository: str) -> None:
    expected = {
        "version": SOAK_STATE_VERSION,
        "purpose": SOAK_PURPOSE,
        "repository": repository,
    }
    if document != expected:
        raise SoakError(
            f"{SOAK_SENTINEL} must contain exactly {json.dumps(expected, sort_keys=True)}"
        )


def _read_json_document(raw: str, *, source: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SoakError(f"{source}: invalid JSON: {exc}") from exc


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def validate_evidence_path(repo: Path, path: Path) -> None:
    resolved = path.resolve()
    root = repo.resolve()
    if resolved.is_relative_to(root) and not resolved.is_relative_to(
        root / ".mergetrain"
    ):
        raise SoakError(
            f"evidence path {resolved} would dirty the target; keep it under "
            f"{root / '.mergetrain'} or outside the clone"
        )


def load_state(
    path: Path,
    *,
    repository: str,
    baseline: str,
) -> dict[str, Any]:
    if path.exists():
        state = _read_json_document(
            path.read_text(encoding="utf-8"), source=str(path)
        )
        if not isinstance(state, dict):
            raise SoakError(f"{path}: state must be a JSON object")
        if state.get("version") != SOAK_STATE_VERSION:
            raise SoakError(
                f"{path}: unsupported state version {state.get('version')!r}"
            )
        if state.get("repository") != repository:
            raise SoakError(
                f"{path}: repository is {state.get('repository')!r}, expected "
                f"{repository!r}"
            )
        persisted_baseline = normalize_baseline(str(state.get("baseline", "")))
        if baseline and normalize_baseline(baseline) != persisted_baseline:
            raise SoakError(
                f"{path}: --baseline differs from persisted baseline "
                f"{persisted_baseline}"
            )
        state["baseline"] = persisted_baseline
        if state.get("crash_status") not in CRASH_STATUSES:
            raise SoakError(f"{path}: invalid crash_status")
        if not isinstance(state.get("recovery_events"), list):
            raise SoakError(f"{path}: recovery_events must be a list")
        if not isinstance(state.get("next_batch"), int) or state["next_batch"] < 1:
            raise SoakError(f"{path}: next_batch must be a positive integer")
        if not re.fullmatch(r"[0-9a-f]{12}", str(state.get("namespace", ""))):
            raise SoakError(f"{path}: namespace must be 12 lowercase hex characters")
        return state

    if not baseline:
        raise SoakError(
            f"{path} does not exist; the first run requires an explicit --baseline"
        )
    state = {
        "version": SOAK_STATE_VERSION,
        "repository": repository,
        "baseline": normalize_baseline(baseline),
        "namespace": uuid.uuid4().hex[:12],
        "next_batch": 1,
        "recovery_events": [],
        "crash_status": "pending",
        "crash_attempts": 0,
        "completed": False,
    }
    save_state(path, state)
    return state


def allocate_batch(state: dict[str, Any], state_path: Path) -> int:
    batch_no = int(state["next_batch"])
    state["next_batch"] = batch_no + 1
    save_state(state_path, state)
    return batch_no


def record_recovery(
    state: dict[str, Any],
    state_path: Path,
    *,
    kind: str,
    batch_no: int,
) -> None:
    state["recovery_events"].append(
        {"kind": kind, "batch_no": batch_no, "ts": now_iso()}
    )
    save_state(state_path, state)


class MT:
    """Thin wrapper over the real mergetrain CLI. Every call is logged."""

    def __init__(self, binary: str, repo: Path, log: Logger, timeout: int = 180) -> None:
        self.binary = binary
        self.repo = repo
        self.log = log
        self.timeout = timeout

    def call(
        self,
        *args: str,
        scenario: str,
        intervention: dict[str, Any] | None = None,
        expect_ok: bool = True,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        cmd = [self.binary, "--repo", str(self.repo), *args, "--json"]
        started = time.monotonic()
        proc = run(cmd, cwd=self.repo, timeout=timeout or self.timeout)
        elapsed = time.monotonic() - started
        payload: dict[str, Any] | None
        try:
            payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        except json.JSONDecodeError:
            payload = None
        record = {
            "event": "cli_call",
            "scenario": scenario,
            "cmd": cmd,
            "returncode": proc.returncode,
            "elapsed_s": round(elapsed, 3),
            "json": payload,
            "stdout_tail": None if payload is not None else proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
        }
        if intervention is not None:
            record["intervention"] = intervention
        self.log.event(**record)
        if payload is None:
            raise SoakError(
                f"unparseable JSON from {' '.join(args)} (rc={proc.returncode}): "
                f"stdout={proc.stdout[-500:]!r} stderr={proc.stderr[-500:]!r}"
            )
        if expect_ok and not payload.get("ok", False):
            code = payload.get("error", {}).get("code", "?")
            if intervention is not None:
                intervention["expected"] = False
                self.log.event(
                    event="unexpected_intervention_outcome",
                    scenario=scenario,
                    cmd=cmd,
                    error_code=code,
                    payload=payload,
                )
            raise SoakError(
                f"{' '.join(args)} failed unexpectedly: error.code={code} "
                f"message={payload.get('error', {}).get('message')}"
            )
        return payload

    def doctor(self, scenario: str) -> dict[str, Any]:
        return self.call("doctor", scenario=scenario)

    def status(self, scenario: str) -> dict[str, Any]:
        return self.call("status", scenario=scenario)

    def stats(self, scenario: str, *, since: str) -> dict[str, Any]:
        return self.call("stats", "--since", since, scenario=scenario)

    def version(self, scenario: str) -> dict[str, Any]:
        return self.call("version", scenario=scenario)

    def enqueue(self, *, task: str, branch: str, scenario: str) -> dict[str, Any]:
        return self.call(
            "enqueue",
            "--task", task,
            "--branch", branch,
            "--worktree", str(self.repo),
            "--capture-sha",
            "--allow-branch-mismatch",
            scenario=scenario,
        )

    def validate_only(self, scenario: str) -> dict[str, Any]:
        return self.call("run-batch", "--validate-only", scenario=scenario, expect_ok=True)

    def deploy(self, scenario: str, timeout: int | None = None) -> dict[str, Any]:
        return self.call("run-batch", "--deploy", scenario=scenario, timeout=timeout)

    def verify(self, job_id: int, *, scenario: str) -> dict[str, Any]:
        result = self.call(
            "verify",
            "--job",
            str(job_id),
            scenario=scenario,
        )
        if result.get("result") != "success":
            raise SoakError(
                f"post-reconcile verify failed for job {job_id}: {result}"
            )
        return result

    def retry(
        self,
        job_id: int,
        *,
        scenario: str,
        reason: str,
        rebase: bool = False,
    ) -> dict[str, Any]:
        args = ["retry", str(job_id)]
        if rebase:
            args.append("--rebase")
        return self.call(
            *args, scenario=scenario,
            intervention={
                "type": "retry",
                "job_id": job_id,
                "planned": True,
                "classification": "expected",
                "reason": reason,
                "issue_url": "",
            },
        )

    def dismiss(
        self,
        job_id: int,
        *,
        scenario: str,
        note: str,
        reason: str,
    ) -> dict[str, Any]:
        return self.call(
            "dismiss", str(job_id), "--note", note, scenario=scenario,
            intervention={
                "type": "dismiss",
                "job_id": job_id,
                "planned": True,
                "classification": "expected",
                "reason": reason,
                "issue_url": "",
            },
        )

    def recover(self, scenario: str, *, reason: str) -> subprocess.CompletedProcess:
        # recover/reconcile use a bespoke, non-generic exit-code table
        # (0/2/3/4/5/7/10), so this bypasses call()'s ok:true assumption and
        # lets the caller branch on the real exit code.
        cmd = [self.binary, "--repo", str(self.repo), "recover", "--json"]
        proc = run(cmd, cwd=self.repo, timeout=self.timeout)
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        self.log.event(
            event="cli_call", scenario=scenario, cmd=cmd, returncode=proc.returncode,
            json=payload, stderr_tail=proc.stderr[-2000:] if proc.stderr else "",
            intervention={
                "type": "recover",
                "planned": True,
                "classification": "expected",
                "reason": reason,
                "issue_url": "",
                "exit_code": proc.returncode,
            },
        )
        return proc


# --- git helpers against the soak-target clone -----------------------------

def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(["git", *args], cwd=repo, check=check)


def validate_target_repository(repo: Path, *, confirmed_repository: str) -> str:
    if not repo.is_dir():
        raise SoakError(f"--repo {repo} does not exist; clone it first")
    root = git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(root).resolve() != repo.resolve():
        raise SoakError(f"--repo must be the Git root itself, got {root!r}")

    origin = git(repo, "remote", "get-url", "origin").stdout.strip()
    repository = github_repo_slug(origin)
    confirmed = confirmed_repository.strip().lower()
    if repository == PROJECT_REPO:
        raise SoakError(
            f"refusing the mergetrain product repository ({PROJECT_REPO}); "
            "use a dedicated throwaway repository"
        )
    if confirmed != repository:
        raise SoakError(
            f"--confirm-repo is {confirmed_repository!r}, but origin is "
            f"https://github.com/{repository}"
        )

    dirty = git(repo, "status", "--porcelain", "--untracked-files=all").stdout
    if dirty.strip():
        paths = ", ".join(
            line[3:] for line in dirty.splitlines()[:8] if len(line) > 3
        )
        raise SoakError(
            f"target worktree must be clean before soak; dirty paths: {paths}"
        )
    for evidence_name in (
        ".mergetrain/soak-state.json",
        ".mergetrain/soak-log.jsonl",
        ".mergetrain/soak-report.md",
    ):
        ignored = git(
            repo,
            "check-ignore",
            "-q",
            evidence_name,
            check=False,
        )
        if ignored.returncode != 0:
            raise SoakError(
                "target must ignore .mergetrain/ before the harness writes "
                f"local state and evidence ({evidence_name} is not ignored)"
            )

    local_sentinel = repo / SOAK_SENTINEL
    if not local_sentinel.is_file():
        raise SoakError(f"target is missing committed sentinel {SOAK_SENTINEL}")
    validate_sentinel(
        _read_json_document(
            local_sentinel.read_text(encoding="utf-8"),
            source=str(local_sentinel),
        ),
        repository=repository,
    )

    git(repo, "fetch", "-q", "origin")
    remote_sentinel = git(
        repo, "show", f"origin/main:{SOAK_SENTINEL}"
    ).stdout
    validate_sentinel(
        _read_json_document(
            remote_sentinel,
            source=f"origin/main:{SOAK_SENTINEL}",
        ),
        repository=repository,
    )
    try:
        github = run(
            ["gh", "api", f"repos/{repository}", "--jq", ".full_name"],
            cwd=repo,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SoakError("gh is required for the real-remote soak") from exc
    if github.returncode != 0 or github.stdout.strip().lower() != repository:
        raise SoakError(
            f"gh API access to {repository} is required so the post-push "
            "GitHub verification cannot silently run without credentials"
        )
    return repository


def validate_target_shape(repo: Path) -> None:
    core = repo / "src" / "soaktarget" / "core.py"
    tests = repo / "tests"
    if not core.is_file() or not tests.is_dir():
        raise SoakError(
            "target must contain src/soaktarget/core.py and tests/; see the "
            "soak harness docstring"
        )
    if _ADD_BODY_RE.search(core.read_text(encoding="utf-8")) is None:
        raise SoakError(
            "src/soaktarget/core.py must define a single-line "
            "`def add(a: int, b: int) -> int:` body"
        )


def validate_mergetrain_runtime(
    mt: MT,
    repo: Path,
    *,
    expected_version: str,
    allow_non_wheel: bool,
    require_idle: bool,
) -> dict[str, Any]:
    version = mt.version(scenario="preflight")
    if version.get("version") != expected_version:
        raise SoakError(
            f"mergetrain version is {version.get('version')!r}; expected "
            f"{expected_version!r}"
        )
    install_mode = version.get("runtime", {}).get("install_mode")
    if not allow_non_wheel and install_mode != "wheel":
        raise SoakError(
            f"mergetrain install_mode is {install_mode!r}; the real soak requires "
            "the published wheel (use --allow-non-wheel only for harness development)"
        )

    doctor = mt.doctor(scenario="preflight")
    config = doctor.get("config", {})
    git_config = config.get("git", {})
    if (
        git_config.get("remote") != "origin"
        or git_config.get("integration_branch") != "main"
        or git_config.get("push_refs") != ["main"]
    ):
        raise SoakError(
            "target config must use remote=origin, integration_branch=main, "
            "and push_refs=[main]"
        )
    gate_names = {gate.get("name") for gate in config.get("gates", [])}
    if not {"ruff", "tests"}.issubset(gate_names):
        raise SoakError("target config must include named `ruff` and `tests` gates")
    if not config.get("deploy", {}).get("verify"):
        raise SoakError("target config must include at least one post-push verify hook")
    if config.get("agent", {}).get("require_clean_worktree_before_enqueue") is not True:
        raise SoakError("target config must require a clean worktree before enqueue")

    if require_idle:
        if doctor.get("lock") is not None:
            raise SoakError("target has a live or stale runner lock; inspect it first")
        counts = doctor.get("counts", {})
        attention = {
            name: int(counts.get(name, 0))
            for name in ATTENTION_COUNTS
            if int(counts.get(name, 0))
        }
        if attention:
            raise SoakError(
                f"target queue is not at a clean checkpoint: {attention}; "
                "triage it and record the intervention before resuming"
            )
        if doctor.get("git", {}).get("worktree_clean") is not True:
            raise SoakError("doctor reports the target worktree is dirty")
    validate_target_shape(repo)
    return doctor


def sync_main(repo: Path) -> None:
    git(repo, "fetch", "-q", "origin")
    git(repo, "switch", "-q", "main")
    git(repo, "reset", "-q", "--hard", "origin/main")


def new_branch(repo: Path, name: str, mutate) -> None:
    git(repo, "switch", "-q", "-c", name, "main")
    mutate(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", name)
    git(repo, "switch", "-q", "main")


def _gen_file_stem(namespace: str, n: int) -> str:
    # Every scenario gets its own numeric namespace (not just a distinct
    # prefix on the .py filename) so that batch_no-derived numbers can never
    # collide across scenario types even if build_schedule() changes later
    # to run a scenario more than once -- gen_success_12.py and
    # gen_gatefail_12.py are always two different files.
    safe_namespace = re.sub(r"[^a-zA-Z0-9_]", "_", namespace)
    return f"gen_{safe_namespace}_{n}"


def write_success_change(n: int, namespace: str):
    def _mutate(repo: Path) -> None:
        stem = _gen_file_stem(f"success_{namespace}", n)
        src = repo / "src" / "soaktarget" / f"{stem}.py"
        test = repo / "tests" / f"test_{stem}.py"
        src.write_text(f"def item_{n}(x):\n    return x + {n}\n", encoding="utf-8")
        test.write_text(
            "import unittest\n\n"
            f"from soaktarget.{stem} import item_{n}\n\n\n"
            f"class Item{n}Tests(unittest.TestCase):\n"
            f"    def test_item_{n}(self) -> None:\n"
            f"        self.assertEqual(item_{n}(1), {n + 1})\n",
            encoding="utf-8",
        )
    return _mutate


def write_gatefail_change(n: int, namespace: str, fixed: bool = False):
    def _mutate(repo: Path) -> None:
        stem = _gen_file_stem(f"gatefail_{namespace}", n)
        src = repo / "src" / "soaktarget" / f"{stem}.py"
        if fixed:
            src.write_text(f"def item_{n}(x):\n    return x + {n}\n", encoding="utf-8")
        else:
            # A real, unforced ruff F401 (unused import) -- fails only the
            # ruff gate, not tests, so the failure is attributable to one gate.
            src.write_text(GATEFAIL_SNIPPET, encoding="utf-8")
    return _mutate


_ADD_BODY_RE = re.compile(
    r"(def add\(a: int, b: int\) -> int:\n)(    .*\n)"
)


def write_conflicting_change(label: str):
    """Rewrite add()'s single-line body via regex, not append-only text.

    An append-only mutation (two branches both adding a new function at the
    end of the file) is NOT guaranteed to actually conflict -- git can often
    apply both patches cleanly one after another. Overwriting the same
    existing line with two different replacements, from the same parent
    commit, is the unambiguous case. Regex-based (not an exact old-text
    match) so this stays correct even if a previous soak run already left a
    different contested body here.
    """
    def _mutate(repo: Path) -> None:
        core = repo / "src" / "soaktarget" / "core.py"
        text = core.read_text(encoding="utf-8")
        # Both branches remain semantically correct alone. Their distinct
        # comments still replace the exact same line from the same parent, so
        # Git must surface the joint textual conflict without turning branch B
        # into an unrelated unit-test failure.
        replacement = f"    return a + b  # {label} contested\n"
        new_text, count = _ADD_BODY_RE.subn(
            lambda m: m.group(1) + replacement, text, count=1
        )
        if count != 1:
            raise SoakError(
                "conflict scenario: could not find add()'s body in core.py "
                "to mutate (has core.py's add() signature changed?)"
            )
        core.write_text(new_text, encoding="utf-8")
    return _mutate


# --- crash exercise ---------------------------------------------------------

def _ps_snapshot() -> list[tuple[int, int, str]]:
    proc = subprocess.run(
        ["ps", "-eo", "pid,ppid,command"], capture_output=True, text=True, check=True
    )
    rows = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        rows.append((pid, ppid, parts[2]))
    return rows


def _find_push_descendant(root_pid: int) -> int | None:
    """A pid whose ancestry chain leads back to root_pid and whose command
    is a real `git push`. Matching on the ancestry chain (not command text
    alone) is what stops this from ever targeting an unrelated `git push`
    some other tool on the machine happens to run concurrently.
    """
    rows = _ps_snapshot()
    parent_of = {pid: ppid for pid, ppid, _ in rows}
    cmd_of = {pid: cmd for pid, _, cmd in rows}
    for pid, cmd in cmd_of.items():
        if "git" not in cmd or "push" not in cmd or "--atomic" not in cmd:
            continue
        ancestor = pid
        seen = set()
        while ancestor in parent_of and ancestor not in seen:
            seen.add(ancestor)
            if ancestor == root_pid:
                return pid
            ancestor = parent_of[ancestor]
    return None


def run_crash_exercise(
    mt: MT,
    repo: Path,
    log: Logger,
    namespace: str,
    batch_no: int,
) -> dict[str, Any] | None:
    """Kill a real `git push --atomic` mid-flight, then recover, against the
    real remote. Returns a verdict after a real kill. ``None`` means the timing
    was missed and the caller should retry.
    """
    if os.name != "posix":
        log.note("crash", "not POSIX; skipping the crash exercise (mirrors "
                  "tests/test_fault_push_kill.py's own POSIX-only guard -- "
                  "this needs real process-group semantics)")
        raise SoakError("the deliberate crash exercise requires POSIX")

    n = f"soak-{namespace}-crash-{batch_no}"
    sync_main(repo)
    new_branch(
        repo,
        n,
        write_success_change(10_000 + batch_no, namespace),
    )
    mt.enqueue(task=n, branch=n, scenario="crash")
    mt.validate_only(scenario="crash")

    cmd = [mt.binary, "--repo", str(repo), "run-batch", "--deploy", "--json"]
    proc = subprocess.Popen(
        cmd, cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    log.event(event="crash_launch", scenario="crash", cmd=cmd, pid=proc.pid)

    deadline = time.monotonic() + 15
    push_pid: int | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        push_pid = _find_push_descendant(proc.pid)
        if push_pid is not None:
            break
        time.sleep(0.015)

    if push_pid is None:
        # Either the deploy finished before we ever observed a `git push`
        # descendant, or it timed out. Either way nothing was killed: clean
        # up the still-running process (if any) and report a miss rather
        # than silently counting this as the exercise.
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        out, err = proc.communicate() if proc.stdout is None else (
            proc.stdout.read() if proc.stdout else "", proc.stderr.read() if proc.stderr else ""
        )
        log.note("crash", f"attempt {batch_no}: missed -- process exited before a "
                  f"push descendant was observed (rc={proc.returncode}); its own "
                  f"run, if it completed, still counts toward trains.landed")
        log.event(event="crash_missed", scenario="crash", stdout_tail=out[-500:], stderr_tail=err[-500:])
        return None

    # The push client was alive at snapshot time -- kill its own process
    # group, not a bare pid: git_runner.py starts every git subprocess with
    # start_new_session=True, so the push client is its own group leader and
    # children of *it* (if any) die with it. ps->kill is not atomic, so a
    # very fast push can still finish in this window; ProcessLookupError
    # here means we lost that race, not a bug -- fall through to "missed".
    try:
        pgid = os.getpgid(push_pid)
        if pgid != push_pid:
            log.note("crash", f"push pid {push_pid} pgid {pgid} != pid; killing "
                      f"the group anyway, this only affects whether other members "
                      f"of the same group also die")
        kill_ts = now_iso()
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        log.note("crash", f"attempt {batch_no}: lost the race -- push pid "
                  f"{push_pid} already exited between observing it and killing "
                  f"it; its own run, if it completed, still counts toward "
                  f"trains.landed")
        log.event(event="crash_missed", scenario="crash", reason="race_lost", push_pid=push_pid)
        return None
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    log.event(event="crash_kill", scenario="crash", push_pid=push_pid, pgid=pgid, ts=kill_ts)

    remote_before = git(
        repo, "ls-remote", "origin", "refs/heads/main"
    ).stdout.strip()
    log.event(event="crash_remote_snapshot", scenario="crash", when="immediately_after_kill",
              ls_remote=remote_before)

    doctor_before = mt.doctor(scenario="crash")
    recover_proc = mt.recover(
        scenario="crash",
        reason="deliberate crash-recovery exercise against the confirmed remote",
    )
    if recover_proc.returncode == 3:
        raise SoakError("recover reported the lock still held by a live runner "
                         "right after we killed it -- investigate before retrying")
    if recover_proc.returncode == 7:
        log.note("crash", "recover: remote unreachable, nothing changed -- will "
                  "retry recover on the next loop iteration via the normal flow")
    if recover_proc.returncode == 10:
        log.note("crash", "recover left >=1 job BLOCKED for human inspection -- "
                  "this needs manual triage, not further automation")

    status_after = mt.status(scenario="crash")
    remote_after = git(
        repo, "ls-remote", "origin", "refs/heads/main"
    ).stdout.strip()
    remote_after_sha = remote_after.split()[0] if remote_after else ""
    jobs = status_after.get("jobs", [])
    crash_job = next((j for j in jobs if j.get("branch") == n), None)
    final_status = crash_job.get("status") if crash_job else "unknown"
    crash_head_sha = crash_job.get("head_sha", "") if crash_job else ""

    # The actual point of the exercise: whatever mergetrain concluded must
    # match what is really on the remote, independent of mergetrain's own
    # say-so. If the crash branch's commit IS the remote tip, only
    # "deployed" is honest; if it is NOT, anything but "deployed" is honest.
    really_landed = bool(crash_head_sha) and remote_after_sha == crash_head_sha
    verdict_matches = (
        (final_status == "deployed") == really_landed
        if final_status in ("deployed", "queued", "blocked", "needs_reconcile")
        else False
    )
    log.event(
        event="crash_verdict", scenario="crash",
        doctor_before=doctor_before, recover_exit_code=recover_proc.returncode,
        recover_json=json.loads(recover_proc.stdout) if recover_proc.stdout.strip() else None,
        final_job_status=final_status, remote_before=remote_before, remote_after=remote_after,
        really_landed=really_landed, verdict_matches=verdict_matches,
    )
    log.note("crash", f"exercise complete: final job status={final_status}, "
              f"really landed on remote={really_landed}, verdict {'MATCHES' if verdict_matches else 'MISMATCH -- BUG'} "
              f"(remote before-kill={remote_before!r} after-recover={remote_after!r}, "
              f"recover exit={recover_proc.returncode})")
    if not verdict_matches:
        raise SoakError(
            f"crash exercise verdict MISMATCH: mergetrain reported job "
            f"{crash_job.get('id') if crash_job else '?'} as {final_status!r} but "
            f"the real remote tip is {remote_after_sha!r} vs job head_sha "
            f"{crash_head_sha!r} (really_landed={really_landed}). This is exactly "
            f"the 1.0 gate ('never lie about deployed/failed') -- stop and file a bug."
        )
    return {
        "branch": n,
        "job_id": crash_job.get("id") if crash_job else None,
        "final_status": final_status,
        "really_landed": really_landed,
        "recover_exit_code": recover_proc.returncode,
        "resolved": final_status in {"deployed", "queued"},
    }


# --- scenarios ---------------------------------------------------------------

def scenario_success(
    mt: MT,
    repo: Path,
    log: Logger,
    namespace: str,
    batch_no: int,
    size: int = 2,
) -> None:
    sync_main(repo)
    branches = []
    for i in range(size):
        n = f"soak-{namespace}-success-{batch_no}-{i}"
        new_branch(
            repo,
            n,
            write_success_change(batch_no * 10 + i, namespace),
        )
        branches.append(n)
    for n in branches:
        mt.enqueue(task=n, branch=n, scenario="success")
    result = mt.validate_only(scenario="success")
    if result["result"] != "success":
        raise SoakError(f"success scenario failed to validate cleanly: {result}")
    deployed = mt.deploy(scenario="success")
    if deployed["result"] not in ("success", "warning"):
        raise SoakError(f"success scenario failed to deploy cleanly: {deployed}")
    log.note("success", f"batch {batch_no}: {len(branches)} job(s) landed "
              f"(result={deployed['result']})")


def scenario_gatefail(
    mt: MT,
    repo: Path,
    log: Logger,
    namespace: str,
    batch_no: int,
) -> bool:
    """Returns True: this always produces a recovery-path event (a blocked job
    fixed by retry)."""
    sync_main(repo)
    n = f"soak-{namespace}-gatefail-{batch_no}"
    new_branch(repo, n, write_gatefail_change(batch_no, namespace))
    job = mt.enqueue(task=n, branch=n, scenario="gatefail")["job"]
    result = mt.validate_only(scenario="gatefail")
    bad = next(j for j in result["jobs"] if j["id"] == job["id"])
    if bad["status"] not in ("blocked", "failed"):
        raise SoakError(f"gatefail scenario did not block/fail as expected: {bad}")
    log.note("gatefail", f"job {job['id']} correctly {bad['status']}: {bad['note'][:120]}")

    git(repo, "switch", "-q", n)
    write_gatefail_change(batch_no, namespace, fixed=True)(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"{n}: fix the unused import")
    mt.retry(
        job["id"],
        scenario="gatefail",
        reason="planned retry after the synthetic Ruff gate failure was fixed",
    )
    git(repo, "switch", "-q", "main")

    result = mt.validate_only(scenario="gatefail")
    if result["result"] != "success":
        raise SoakError(f"gatefail scenario did not validate cleanly after retry: {result}")
    deployed = mt.deploy(scenario="gatefail")
    if deployed["result"] not in ("success", "warning"):
        raise SoakError(f"gatefail scenario failed to deploy cleanly after retry: {deployed}")
    log.note("gatefail", f"batch {batch_no}: landed after retry")
    return True


def scenario_conflict(
    mt: MT,
    repo: Path,
    log: Logger,
    namespace: str,
    batch_no: int,
) -> bool:
    """Two branches edit the same lines of core.py. One blocks; resolved by
    dismissing it and re-submitting the same conceptual change against the
    now-current main, rather than scripting a real rebase-conflict
    resolution (retry --rebase only auto-resolves non-conflicting rebases,
    per docs/cli.md: 'any... rebase conflict leaves the old queue row
    untouched'). Returns True: this always produces a recovery-path event.
    """
    sync_main(repo)
    a = f"soak-{namespace}-conflict-{batch_no}-a"
    b = f"soak-{namespace}-conflict-{batch_no}-b"
    new_branch(repo, a, write_conflicting_change("a"))
    new_branch(repo, b, write_conflicting_change("b"))
    job_a = mt.enqueue(task=a, branch=a, scenario="conflict")["job"]
    job_b = mt.enqueue(task=b, branch=b, scenario="conflict")["job"]
    result = mt.validate_only(scenario="conflict")
    by_id = {j["id"]: j for j in result["jobs"]}
    blocked = [j for j in (by_id[job_a["id"]], by_id[job_b["id"]]) if j["status"] == "blocked"]
    validated = [j for j in (by_id[job_a["id"]], by_id[job_b["id"]]) if j["status"] == "validated"]
    if not blocked or not validated:
        raise SoakError(f"conflict scenario did not produce one blocked + one "
                         f"validated job: {by_id}")
    loser = blocked[0]
    log.note("conflict", f"job {loser['id']} correctly blocked "
              f"(conflict_with={loser.get('conflict_with')!r}): {loser['note'][:120]}")

    winner_deploy = mt.deploy(scenario="conflict")  # lands the winner, advancing main
    if winner_deploy["result"] not in ("success", "warning"):
        raise SoakError(f"conflict scenario's winner failed to deploy cleanly: {winner_deploy}")
    mt.dismiss(
        loser["id"],
        scenario="conflict",
        note="superseded, resubmitting after conflict",
        reason="planned dismissal before resubmitting the synthetic conflict loser",
    )

    sync_main(repo)
    label = "a" if loser["id"] == job_a["id"] else "b"
    retry_branch = f"{loser['branch']}-retry"
    new_branch(repo, retry_branch, write_conflicting_change(label))
    mt.enqueue(task=retry_branch, branch=retry_branch, scenario="conflict")
    result = mt.validate_only(scenario="conflict")
    if result["result"] != "success":
        raise SoakError(f"conflict scenario's resubmission did not validate cleanly: {result}")
    loser_deploy = mt.deploy(scenario="conflict")
    if loser_deploy["result"] not in ("success", "warning"):
        raise SoakError(f"conflict scenario's resubmitted loser failed to deploy cleanly: {loser_deploy}")
    log.note("conflict", f"batch {batch_no}: winner + resubmitted loser both landed")
    return True


# --- main loop ---------------------------------------------------------------

def build_schedule() -> list[str]:
    return (
        ["success", "success", "gatefail", "success", "conflict", "success", "crash"]
        + ["success"] * 60
    )


def _attention_counts(doctor: dict[str, Any]) -> dict[str, int]:
    counts = doctor.get("counts", {})
    return {
        name: int(counts.get(name, 0))
        for name in ATTENTION_COUNTS
        if int(counts.get(name, 0))
    }


def evaluate_criteria(
    stats: dict[str, Any],
    log: Logger,
    state: dict[str, Any],
    doctor: dict[str, Any],
    *,
    repository: str,
) -> dict[str, Any]:
    trains = stats.get("trains", {})
    landed = int(trains.get("landed", 0))
    untriaged = log.untriaged_interventions()
    attention = _attention_counts(doctor)
    criteria = {
        "landed": landed,
        "minimum_landed_met": landed >= MIN_LANDED_TRAINS,
        "recovery_events": len(state["recovery_events"]),
        "recovery_met": len(state["recovery_events"]) >= 1,
        "non_mergetrain_repository": repository != PROJECT_REPO,
        "crash_status": state["crash_status"],
        "crash_met": (
            state["crash_status"] == "completed"
            and not state.get("crash_queue_pending", False)
        ),
        "interventions": len(log.interventions),
        "untriaged_interventions": len(untriaged),
        "interventions_triaged": not untriaged,
        "attention": attention,
        "operational_clear": doctor.get("lock") is None and not attention,
    }
    criteria["complete"] = all(
        (
            criteria["minimum_landed_met"],
            criteria["recovery_met"],
            criteria["non_mergetrain_repository"],
            criteria["crash_met"],
            criteria["interventions_triaged"],
            criteria["operational_clear"],
        )
    )
    return criteria


def write_report(
    report_path: Path,
    stats: dict[str, Any],
    log: Logger,
    state: dict[str, Any],
    doctor: dict[str, Any],
    *,
    repository: str,
    repo: Path,
    session_target: int,
    skip_crash: bool,
) -> dict[str, Any]:
    criteria = evaluate_criteria(
        stats, log, state, doctor, repository=repository
    )
    landed = criteria["landed"]
    lines = [
        "# mergetrain soak run report",
        "",
        f"Generated: {now_iso()}",
        f"Target repo: https://github.com/{repository} (local clone: {repo})",
        f"Baseline: {state['baseline']}",
        f"Namespace: {state['namespace']}",
        f"Session target: {session_target} landed trains",
        f"Session mode: {'smoke (crash intentionally deferred)' if skip_crash else 'full soak'}",
        "",
        "## issue #179 exit criteria",
        "",
        f"- [{'x' if criteria['minimum_landed_met'] else ' '}] >= "
        f"{MIN_LANDED_TRAINS} landed trains since baseline (actual: {landed})",
        f"- [{'x' if criteria['recovery_met'] else ' '}] >= 1 train through a "
        f"recovery path (actual: {criteria['recovery_events']})",
        "- [x] one non-mergetrain repository (this one)",
        f"- [{'x' if criteria['crash_met'] else ' '}] one deliberate "
        f"crash-recovery exercise against a real remote "
        f"(status: {criteria['crash_status']})",
        f"- [{'x' if criteria['interventions_triaged'] else ' '}] every operator "
        f"intervention classified (untriaged: {criteria['untriaged_interventions']})",
        f"- [{'x' if criteria['operational_clear'] else ' '}] final queue and "
        f"runner state are clear (attention: {criteria['attention']})",
        "- [x] readout via `mergetrain stats --json` (below)",
        "",
        f"Overall soak complete: **{'YES' if criteria['complete'] else 'NO'}**",
        "",
        "## mergetrain stats --json",
        "",
        "```json",
        json.dumps(stats, indent=2, default=str),
        "```",
        "",
        f"## Operator interventions ({len(log.interventions)})",
        "",
    ]
    for rec in log.interventions:
        iv = rec.get("intervention", {})
        lines.append(
            f"- `{iv.get('type')}` scenario={rec.get('scenario')} "
            f"classification={iv.get('classification')} planned={iv.get('planned')} "
            f"reason={iv.get('reason')!r} issue={iv.get('issue_url') or '-'} "
            f"ts={rec.get('ts')}"
        )
    if not log.interventions:
        lines.append("(none recorded)")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return criteria


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", required=True, type=Path, help="the soak-target clone")
    parser.add_argument(
        "--confirm-repo",
        required=True,
        help="exact GitHub owner/name of the disposable target",
    )
    parser.add_argument(
        "--expected-version",
        required=True,
        help="exact mergetrain release to exercise, for example 0.9.0",
    )
    parser.add_argument("--baseline", default="", help="first-run ISO-8601 lower bound")
    parser.add_argument("--mt", default="mergetrain", help="mergetrain binary to invoke")
    parser.add_argument("--target-landed", type=int, default=22)
    parser.add_argument("--max-batches", type=int, default=80)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--skip-crash", action="store_true")
    parser.add_argument(
        "--allow-non-wheel",
        action="store_true",
        help="harness development only; real soak evidence must use a wheel",
    )
    parser.add_argument(
        "--reset-crash-attempts",
        action="store_true",
        help="retry after three timing misses; refused for ambiguous/triage states",
    )
    parser.add_argument(
        "--record-intervention",
        choices=("unlock", "reconcile", "dismiss", "retry", "manual_git_surgery"),
    )
    parser.add_argument(
        "--classification",
        choices=("bug", "docs_gap"),
        help="classification for --record-intervention",
    )
    parser.add_argument("--reason", default="")
    parser.add_argument("--issue-url", default="")
    return parser


def _record_manual_intervention(
    args: argparse.Namespace,
    log: Logger,
) -> None:
    if (
        not args.classification
        or not args.reason.strip()
        or not _valid_issue_url(args.issue_url.strip())
    ):
        raise SoakError(
            "--record-intervention requires --classification, --reason, and a "
            "credential-free HTTP(S) --issue-url"
        )
    log.event(
        event="manual_intervention",
        scenario="operator",
        intervention={
            "type": args.record_intervention,
            "planned": False,
            "classification": args.classification,
            "reason": args.reason.strip(),
            "issue_url": args.issue_url.strip(),
        },
    )


def _session_goal_met(
    criteria: dict[str, Any],
    *,
    target_landed: int,
    skip_crash: bool,
) -> bool:
    return all(
        (
            criteria["landed"] >= target_landed,
            criteria["recovery_met"],
            skip_crash or criteria["crash_met"],
            criteria["interventions_triaged"],
            criteria["operational_clear"],
        )
    )


def main() -> int:
    args = _parser().parse_args()
    if args.target_landed <= 0 or args.max_batches <= 0:
        raise SoakError("--target-landed and --max-batches must be positive")
    if not args.skip_crash and args.target_landed < MIN_LANDED_TRAINS:
        raise SoakError(
            f"a full soak must target at least {MIN_LANDED_TRAINS} landed trains"
        )
    if not args.skip_crash and os.name != "posix":
        raise SoakError("the deliberate crash exercise requires POSIX")
    if args.record_intervention is None and (
        args.classification or args.reason or args.issue_url
    ):
        raise SoakError(
            "--classification, --reason, and --issue-url require "
            "--record-intervention"
        )

    repo = args.repo.resolve()
    if repo == MERGETRAIN_REPO.resolve():
        raise SoakError(
            f"refusing mergetrain's own repo ({MERGETRAIN_REPO}); use a "
            "dedicated throwaway target"
        )
    repository = validate_target_repository(
        repo, confirmed_repository=args.confirm_repo
    )

    state_path = (
        args.state.resolve()
        if args.state
        else repo / ".mergetrain" / "soak-state.json"
    )
    log_path = (
        args.log.resolve()
        if args.log
        else repo / ".mergetrain" / "soak-log.jsonl"
    )
    report_path = (
        args.report.resolve()
        if args.report
        else repo / ".mergetrain" / "soak-report.md"
    )
    for path in (state_path, log_path, report_path):
        validate_evidence_path(repo, path)

    state = load_state(
        state_path,
        repository=repository,
        baseline=args.baseline,
    )
    if args.reset_crash_attempts:
        if state["crash_status"] not in {"pending", "failed"}:
            raise SoakError(
                "--reset-crash-attempts is refused after a kill or ambiguous "
                f"attempt (status={state['crash_status']!r})"
            )
        state["crash_attempts"] = 0
        state["crash_status"] = "pending"
        save_state(state_path, state)

    log = Logger(log_path)
    try:
        mt = MT(args.mt, repo, log)
        validate_mergetrain_runtime(
            mt,
            repo,
            expected_version=args.expected_version,
            allow_non_wheel=args.allow_non_wheel,
            require_idle=args.record_intervention is None,
        )

        if args.record_intervention is not None:
            _record_manual_intervention(args, log)
            stats = mt.stats(scenario="intervention", since=state["baseline"])
            doctor = mt.doctor(scenario="intervention")
            if (
                state["crash_status"] == "needs_triage"
                and isinstance(state.get("crash_verdict"), dict)
                and doctor.get("lock") is None
                and not _attention_counts(doctor)
            ):
                state["crash_status"] = "completed"
                state["crash_queue_pending"] = False
                save_state(state_path, state)
                log.note(
                    "crash",
                    "operator intervention cleared the post-verdict attention "
                    "state; deliberate crash exercise is now resolved",
                )
            write_report(
                report_path,
                stats,
                log,
                state,
                doctor,
                repository=repository,
                repo=repo,
                session_target=args.target_landed,
                skip_crash=args.skip_crash,
            )
            print(f"intervention recorded in {log_path}")
            return 0

        if state["crash_status"] in {"attempting", "needs_triage"}:
            raise SoakError(
                f"persisted crash status is {state['crash_status']!r}; compare "
                "the queue with origin/main and record/resolve the intervention "
                "before any further destructive exercise"
            )

        stats = mt.stats(scenario="startup", since=state["baseline"])
        trains = stats.get("trains", {})
        print(
            "resuming from persisted state: "
            f"baseline={state['baseline']} "
            f"trains.landed={trains.get('landed', 0)} "
            f"recovery_events={len(state['recovery_events'])} "
            f"crash_status={state['crash_status']}"
        )

        schedule = build_schedule()
        schedule_index = 0
        executed = 0
        while executed < args.max_batches and schedule_index < len(schedule):
            stats = mt.stats(scenario="loop", since=state["baseline"])
            doctor = mt.doctor(scenario="loop")
            criteria = evaluate_criteria(
                stats, log, state, doctor, repository=repository
            )
            if _session_goal_met(
                criteria,
                target_landed=args.target_landed,
                skip_crash=args.skip_crash,
            ):
                break

            if state.get("crash_queue_pending", False):
                kind = "success"
            else:
                kind = schedule[schedule_index]
                schedule_index += 1
            if kind == "crash" and (
                args.skip_crash or state["crash_status"] == "completed"
            ):
                continue
            if kind == "crash" and int(state["crash_attempts"]) >= 3:
                state["crash_status"] = "failed"
                save_state(state_path, state)
                log.note(
                    "crash",
                    "three timing attempts were missed; crash exercise remains "
                    "incomplete (inspect the log, then use "
                    "--reset-crash-attempts to try again)",
                )
                break

            batch_no = allocate_batch(state, state_path)
            executed += 1
            landed = criteria["landed"]
            print(f"--- batch {batch_no}: {kind} (landed since baseline: {landed}) ---")

            if kind == "crash":
                state["crash_status"] = "attempting"
                state["crash_attempts"] = int(state["crash_attempts"]) + 1
                save_state(state_path, state)
                try:
                    outcome = run_crash_exercise(
                        mt,
                        repo,
                        log,
                        state["namespace"],
                        batch_no,
                    )
                except BaseException:
                    state["crash_status"] = "needs_triage"
                    save_state(state_path, state)
                    raise
                if outcome is None:
                    state["crash_status"] = "pending"
                    save_state(state_path, state)
                    schedule.insert(schedule_index, "crash")
                    continue
                state["crash_verdict"] = outcome
                save_state(state_path, state)
                if outcome["final_status"] == "deployed":
                    job_id = outcome.get("job_id")
                    if not isinstance(job_id, int):
                        state["crash_status"] = "needs_triage"
                        save_state(state_path, state)
                        raise SoakError(
                            "recovered deploy has no job id for post-crash verify"
                        )
                    try:
                        mt.verify(job_id, scenario="crash-verify")
                    except BaseException:
                        state["crash_status"] = "needs_triage"
                        save_state(state_path, state)
                        raise
                    outcome["verify_status"] = "succeeded"
                state["crash_queue_pending"] = outcome["final_status"] == "queued"
                state["crash_status"] = (
                    "completed" if outcome["resolved"] else "needs_triage"
                )
                record_recovery(
                    state,
                    state_path,
                    kind="crash",
                    batch_no=batch_no,
                )
                if not outcome["resolved"]:
                    log.note(
                        "crash",
                        f"remote truth matched, but final status "
                        f"{outcome['final_status']!r} still needs operator triage",
                    )
                    break
                continue

            if kind == "gatefail":
                if scenario_gatefail(
                    mt, repo, log, state["namespace"], batch_no
                ):
                    record_recovery(
                        state,
                        state_path,
                        kind="gatefail",
                        batch_no=batch_no,
                    )
            elif kind == "conflict":
                if scenario_conflict(
                    mt, repo, log, state["namespace"], batch_no
                ):
                    record_recovery(
                        state,
                        state_path,
                        kind="conflict",
                        batch_no=batch_no,
                    )
            else:
                scenario_success(
                    mt, repo, log, state["namespace"], batch_no
                )
                if state.get("crash_queue_pending", False):
                    status = mt.status(scenario="crash-settle")
                    branch = state.get("crash_verdict", {}).get("branch")
                    crash_job = next(
                        (
                            job
                            for job in status.get("jobs", [])
                            if job.get("branch") == branch
                        ),
                        None,
                    )
                    if (
                        crash_job
                        and crash_job.get("status") == "deployed"
                        and crash_job.get("verify_status") == "succeeded"
                    ):
                        state["crash_queue_pending"] = False
                        save_state(state_path, state)
                    else:
                        raise SoakError(
                            "the recovered crash job did not deploy and verify "
                            "successfully in the follow-up train; inspect status "
                            "before continuing"
                        )

        stats = mt.stats(scenario="final", since=state["baseline"])
        doctor = mt.doctor(scenario="final")
        criteria = write_report(
            report_path,
            stats,
            log,
            state,
            doctor,
            repository=repository,
            repo=repo,
            session_target=args.target_landed,
            skip_crash=args.skip_crash,
        )
        state["completed"] = bool(criteria["complete"])
        save_state(state_path, state)
        session_ok = _session_goal_met(
            criteria,
            target_landed=args.target_landed,
            skip_crash=args.skip_crash,
        )
        print(
            f"done. report written to {report_path}; "
            f"session_goal={'met' if session_ok else 'NOT MET'}; "
            f"overall_soak={'complete' if criteria['complete'] else 'incomplete'}"
        )
        return 0 if session_ok else 1
    finally:
        log.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SoakError as exc:
        print(f"\nSOAK STOPPED: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\ninterrupted; rerun with the same --repo to resume", file=sys.stderr)
        sys.exit(130)
