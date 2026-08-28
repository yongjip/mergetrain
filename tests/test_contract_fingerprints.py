"""The forcing function behind CONTRACT_VERSION (issue #44, Phase 3).

JSON has no physical version gate like SQLite's ``user_version``, so without a
check the emitted ``contract_version`` silently goes stale — the exact
write-without-read gap the registry once had. This test captures a recursive
KEY-SET fingerprint (keys only, values ignored, nested included) of a
representative payload for every agent-facing ``--json`` surface and each JSONL
frame, and fails whenever a live fingerprint diverges from the checked-in
golden.

When it fails, the diff classifies the change:
- keys only ADDED -> additive, allowed within a contract major. Regenerate the
  golden (``MERGETRAIN_REGEN_FINGERPRINTS=1 python -m unittest ...``) and note
  it in the changelog.
- keys REMOVED/RENAMED -> breaking. Bump ``CONTRACT_VERSION`` (and the config
  or stream contract if relevant) deliberately, then regenerate.

It cannot see a same-keys value-type or value-meaning change; that residual
still rests on review. But no shape change reaches users without a conscious
human decision.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_runner import make_demo_repo

from mergetrain.cli import main
from mergetrain.config import load_config
from mergetrain.contract import CONTRACT_VERSION
from mergetrain.store import (
    acquire_runner_lock,
    connect,
    default_owner,
    enqueue_job,
    record_run_event,
)

GOLDEN = Path(__file__).resolve().parent / "contract_fingerprints.json"


def keyset(obj):
    """Recursive key-set signature: keys only, sorted, values collapsed.

    A list becomes the union of its elements' signatures so a one-element
    representative captures the element shape; a scalar leaf becomes ``None``.
    """

    if isinstance(obj, dict):
        return {key: keyset(obj[key]) for key in sorted(obj)}
    if isinstance(obj, list):
        merged: dict = {}
        scalar = False
        for item in obj:
            sig = keyset(item)
            if isinstance(sig, dict):
                merged.update(sig)
            else:
                scalar = True
        if merged:
            return {"[]": merged}
        return "[]" if scalar else "[]?"
    return None


def _run_json(argv: list[str]) -> dict:
    out = io.StringIO()
    with redirect_stdout(out):
        main([*argv, "--json"])
    return json.loads(out.getvalue())


def _capture_simple(argv):
    def capture(repo: Path) -> dict:
        return _run_json(["--repo", str(repo), *argv])

    return capture


# --- surface capture functions -------------------------------------------


def _cap_doctor(repo):
    return _run_json(["--repo", str(repo), "doctor"])


def _cap_status(repo):
    return _run_json(["--repo", str(repo), "status"])


def _cap_version(repo):
    return _run_json(["--repo", str(repo), "version"])


def _cap_agent_contract(repo):
    return _run_json(["--repo", str(repo), "agent-contract"])


def _cap_enqueue(repo):
    # Enqueue emits the job envelope; seed a branch so it succeeds.
    return _run_json(
        [
            "--repo",
            str(repo),
            "enqueue",
            "--task",
            "a",
            "--branch",
            "feature/a",
            "--no-ready-check",
        ]
    )


def _cap_run_validate(repo):
    return _run_json(["--repo", str(repo), "run-batch", "--validate-only"])


def _cap_gc(repo):
    # Seed a terminal job so branch_candidates[] is NON-empty: keyset() renders an
    # empty list as "[]?" and pins nothing inside it, which is how a string
    # job_id sat next to an int job_id in this very payload without CI noticing.
    from mergetrain.store import mark_job

    conn = connect(_db(repo))
    job = enqueue_job(conn, task="a", branch="feature/gc")
    mark_job(conn, job.id, status="deployed", note="done")
    conn.close()
    return _run_json(["--repo", str(repo), "gc"])


def _cap_gc_applied(repo):
    # `gc --apply` returns a different object (result{removed_worktrees,
    # deleted_branches, failed, swept_pending_refs}) that docs/contract.md claims
    # is covered. It was not.
    from mergetrain.store import mark_job

    conn = connect(_db(repo))
    job = enqueue_job(conn, task="a", branch="feature/gc")
    mark_job(conn, job.id, status="deployed", note="done")
    conn.close()
    return _run_json(["--repo", str(repo), "gc", "--apply"])


def _cap_reconcile(repo):
    return _run_json(["--repo", str(repo), "reconcile"])


def _cap_inspect(repo):
    conn = connect(_db(repo))
    job = enqueue_job(conn, task="a", branch="feature/a")
    conn.close()
    return _run_json(["--repo", str(repo), "inspect", str(job.id)])


def _seed_history(repo):
    conn = connect(_db(repo))
    job = enqueue_job(conn, task="a", branch="feature/a")
    conn.execute(
        "UPDATE deploy_queue SET status='deployed', train_id='train-1', train_size=1, "
        "requested_at='2026-07-22T00:00:00Z', started_at='2026-07-22T00:01:00Z', "
        "finished_at='2026-07-22T00:02:00Z', push_status='succeeded', "
        "verify_status='succeeded' WHERE id=?",
        (job.id,),
    )
    conn.executemany(
        "INSERT INTO run_events "
        "(claim_token, phase, state, message, detail, created_at) "
        "VALUES ('metrics', 'gating', ?, ?, '', ?)",
        [
            ("active", "Running gate 1/1: tests", "2026-07-22T00:01:10Z"),
            ("success", "Passed gate 1/1: tests", "2026-07-22T00:01:20Z"),
        ],
    )
    conn.commit()
    conn.close()


def _cap_history(repo):
    _seed_history(repo)
    return _run_json(["--repo", str(repo), "history"])


def _cap_stats(repo):
    _seed_history(repo)
    return _run_json(["--repo", str(repo), "stats"])


def _cap_failure_envelope(repo):
    # A config_error routes through main()'s unified failure envelope.
    (repo / ".mergetrain.yaml").write_text(
        "git:\n  push_refs: []\n", encoding="utf-8"
    )
    out = io.StringIO()
    with redirect_stdout(out):
        main(["--repo", str(repo), "doctor", "--json"])
    return json.loads(out.getvalue())


def _db(repo: Path) -> Path:
    # make_demo_repo points the config's db at an absolute path outside
    # .mergetrain/, so resolve the real one instead of assuming a layout.
    return Path(load_config(repo=repo).state.db)


# JSONL frames come out of one events stream over a seeded event.
def _cap_jsonl_frames(repo):
    conn = connect(_db(repo))
    job = enqueue_job(conn, task="a", branch="feature/a")
    conn.execute(
        "UPDATE deploy_queue SET status='in_progress', claim_token='t' WHERE id=?",
        (job.id,),
    )
    record_run_event(
        conn,
        claim_token="t",
        job_id=job.id,
        phase="gating",
        state="active",
        message="Running gate 1/1: tests",
    )
    conn.commit()
    conn.close()
    out = io.StringIO()
    with redirect_stdout(out):
        main(
            [
                "--repo",
                str(repo),
                "events",
                "--job",
                str(job.id),
                "--after",
                "0",
                "--jsonl",
            ]
        )
    frames = {}
    for line in out.getvalue().splitlines():
        rec = json.loads(line)
        frames[rec["type"]] = keyset(rec)

    # The one-shot path above emits stream_start + event only. A follower also
    # sees heartbeat, and any stream that ends emits stream_end -- the frame that
    # says WHY it stopped, and the one docs/contract.md promises carries ok:false
    # and error. Both were outside the gate, so capture them too.
    #
    # stream_end (normal): a scoped follow over a terminal job returns at once.
    conn = connect(_db(repo))
    terminal_job = enqueue_job(conn, task="b", branch="feature/b")
    conn.execute(
        "UPDATE deploy_queue SET status='deployed' WHERE id=?", (terminal_job.id,)
    )
    conn.commit()
    conn.close()
    ended = io.StringIO()
    with redirect_stdout(ended):
        main(
            [
                "--repo",
                str(repo),
                "events",
                "--job",
                str(terminal_job.id),
                "--jsonl",
                "--follow",
                "--poll-interval",
                "0.05",
            ]
        )
    for line in ended.getvalue().splitlines():
        rec = json.loads(line)
        frames.setdefault(rec["type"], keyset(rec))

    # stream_end (interrupted) is emitted by main()'s KeyboardInterrupt handler
    # and has a DIFFERENT key set from the normal one, which is exactly the kind
    # of divergence an un-pinned family hides.
    interrupted = io.StringIO()
    with (
        mock.patch("mergetrain.cli.cmd_events", side_effect=KeyboardInterrupt),
        redirect_stdout(interrupted),
    ):
        main(["--repo", str(repo), "events", "--jsonl"])
    for line in interrupted.getvalue().splitlines():
        rec = json.loads(line)
        frames[f"{rec['type']}_interrupted"] = keyset(rec)

    # heartbeat needs a live lease mid-run, which a bounded capture cannot hold
    # open, so pin the builder the CLI prints verbatim (_print_event_record adds
    # nothing to it).
    from mergetrain.models import RunnerLock
    from mergetrain.observability import heartbeat_record

    frames["heartbeat"] = keyset(
        heartbeat_record(
            [terminal_job],
            RunnerLock(
                name="runner",
                owner="runner:1",
                token="t",
                acquired_at="2026-07-25T00:00:00Z",
                heartbeat_at="2026-07-25T00:00:10Z",
                expires_at="2026-07-25T00:30:00Z",
            ),
            after_event_id=0,
            latest_event=None,
        )
    )
    return frames


def _cap_recover(repo):
    return _run_json(["--repo", str(repo), "recover"])


def _cap_unlock(repo):
    # No lock present: the command runs, ok:true, cleared:false (exit 5).
    return _run_json(["--repo", str(repo), "unlock"])


def _cap_cancel(repo):
    conn = connect(_db(repo))
    job = enqueue_job(conn, task="a", branch="feature/a")
    conn.close()
    return _run_json(["--repo", str(repo), "cancel", str(job.id)])


def _cap_verify(repo):
    # Seed a deployed+unknown job so `resolved` has a representative element.
    from mergetrain.store import mark_job

    conn = connect(_db(repo))
    job = enqueue_job(conn, task="a", branch="feature/a")
    mark_job(
        conn, job.id, status="deployed", deploy_sha="e" * 40,
        push_status="succeeded", verify_status="unknown",
    )
    conn.close()
    return _run_json(["--repo", str(repo), "verify", "--ack", "succeeded"])


def _cap_dismiss(repo):
    # Seed a blocked job so `dismissed` has a representative element.
    from mergetrain.store import mark_job

    conn = connect(_db(repo))
    job = enqueue_job(conn, task="a", branch="feature/a")
    mark_job(conn, job.id, status="blocked", note="gate failed")
    conn.close()
    return _run_json(["--repo", str(repo), "dismiss", str(job.id)])


def _cap_retry(repo):
    from mergetrain.store import mark_job

    subprocess.run(["git", "add", ".mergetrain.yaml"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "track config for retry capture"],
        cwd=repo,
        check=True,
    )
    conn = connect(_db(repo))
    job = enqueue_job(
        conn,
        task="a",
        branch="main",
        worktree_path=str(repo),
        note="retry me",
    )
    mark_job(conn, job.id, status="failed", note="gate failed")
    conn.close()
    return _run_json(["--repo", str(repo), "retry", str(job.id)])


def _cap_hub_status(repo):
    # Seed a one-repo registry with a live queue so repos[] has a
    # representative entry carrying an embedded snapshot.
    from mergetrain.registry import add_repo

    registry = repo / "hub-repos.json"
    conn = connect(_db(repo))
    enqueue_job(conn, task="a", branch="feature/a")
    conn.close()
    add_repo(repo, registry)
    return _run_json(["--repo", str(repo), "hub", "status", "--registry", str(registry)])


def _cap_init(repo):
    # `init --write` reports the scaffold it produced and what to do next. It
    # has no --json flag: the payload is unconditional, so capture it directly.
    out = io.StringIO()
    with redirect_stdout(out):
        main(["--repo", str(repo), "init", "--project", "demo", "--write", "--force"])
    return json.loads(out.getvalue())


def _cap_run_preview(repo):
    # The pre-deploy preview is its own shape -- push plan, reuse decision, and
    # the exact train -- and it is what an operator or agent reads *before*
    # approving a push, so it belongs under the freeze like any other surface.
    _run_json(
        [
            "--repo",
            str(repo),
            "enqueue",
            "--task",
            "a",
            "--branch",
            "feature/a",
            "--no-ready-check",
        ]
    )
    _run_json(["--repo", str(repo), "run-batch", "--validate-only"])
    return _run_json(["--repo", str(repo), "run-batch", "--deploy", "--preview"])


def _hub_registry(repo: Path) -> Path:
    return repo / "hub-repos.json"


def _cap_hub_add(repo):
    return _run_json(
        ["hub", "add", str(repo), "--registry", str(_hub_registry(repo))]
    )


def _cap_hub_list(repo):
    from mergetrain.registry import add_repo

    add_repo(repo, _hub_registry(repo))
    return _run_json(["hub", "list", "--registry", str(_hub_registry(repo))])


def _cap_hub_remove(repo):
    from mergetrain.registry import add_repo

    add_repo(repo, _hub_registry(repo))
    return _run_json(
        ["hub", "remove", str(repo), "--registry", str(_hub_registry(repo))]
    )


SURFACES = {
    "doctor": _cap_doctor,
    "status": _cap_status,
    "version": _cap_version,
    "agent_contract": _cap_agent_contract,
    "enqueue": _cap_enqueue,
    "run_batch_validate": _cap_run_validate,
    "gc": _cap_gc,
    "reconcile": _cap_reconcile,
    "recover": _cap_recover,
    "unlock": _cap_unlock,
    "verify": _cap_verify,
    "dismiss": _cap_dismiss,
    "retry": _cap_retry,
    "cancel": _cap_cancel,
    "hub_status": _cap_hub_status,
    "gc_applied": _cap_gc_applied,
    "hub_add": _cap_hub_add,
    "hub_list": _cap_hub_list,
    "hub_remove": _cap_hub_remove,
    "init": _cap_init,
    "run_batch_preview": _cap_run_preview,
    "inspect": _cap_inspect,
    "history": _cap_history,
    "stats": _cap_stats,
    "failure_envelope": _cap_failure_envelope,
}


class ContractFingerprintTests(unittest.TestCase):
    def _capture_all(self) -> dict:
        live: dict = {}
        for name, capture in SURFACES.items():
            with tempfile.TemporaryDirectory() as td:
                repo, _ = make_demo_repo(Path(td))
                live[name] = keyset(capture(repo))
        # JSONL frames captured as a nested map of type -> keyset.
        with tempfile.TemporaryDirectory() as td:
            repo, _ = make_demo_repo(Path(td))
            live["_jsonl_frames"] = _cap_jsonl_frames(repo)
        return live

    def test_every_surface_carries_the_expected_contract_stamp(self) -> None:
        # One-shot surfaces stamp contract_version top-level; nested payloads
        # do not. The JSONL stream_start header carries it; other frames don't.
        for name in SURFACES:
            with tempfile.TemporaryDirectory() as td:
                repo, _ = make_demo_repo(Path(td))
                payload = SURFACES[name](repo)
                self.assertEqual(
                    payload.get("contract_version"),
                    CONTRACT_VERSION,
                    f"{name} must stamp contract_version top-level",
                )
        with tempfile.TemporaryDirectory() as td:
            repo, _ = make_demo_repo(Path(td))
            frames = _cap_jsonl_frames(repo)
            self.assertIn("stream_start", frames)
            self.assertIn("contract_version", frames["stream_start"])
            self.assertNotIn("contract_version", frames.get("event", {}))

    def test_fingerprints_match_golden(self) -> None:
        live = self._capture_all()
        if os.environ.get("MERGETRAIN_REGEN_FINGERPRINTS"):
            GOLDEN.write_text(
                json.dumps(
                    {"contract_version": CONTRACT_VERSION, "surfaces": live},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            self.skipTest("regenerated golden fingerprints")
        self.assertTrue(
            GOLDEN.is_file(),
            "missing tests/contract_fingerprints.json — regenerate with "
            "MERGETRAIN_REGEN_FINGERPRINTS=1",
        )
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(
            golden["contract_version"],
            CONTRACT_VERSION,
            "golden was captured under a different CONTRACT_VERSION; "
            "regenerate deliberately when bumping",
        )
        expected = golden["surfaces"]
        for name in sorted(set(live) | set(expected)):
            self.assertIn(name, expected, f"new surface '{name}': regen golden after review")
            self.assertIn(name, live, f"surface '{name}' vanished: shape change — bump/regen")
            self.assertEqual(
                live[name],
                expected[name],
                f"contract shape changed for '{name}'. Added keys ⇒ additive "
                "(regen golden); removed/renamed keys ⇒ breaking (bump "
                "CONTRACT_VERSION, then regen). Set "
                "MERGETRAIN_REGEN_FINGERPRINTS=1 to regenerate.",
            )


class ErrorTaxonomyTests(unittest.TestCase):
    """keyset() nulls every value, so the error.code strings and retryable flags
    that agents dispatch on live OUTSIDE the fingerprint golden. Pin the literal
    values here so the taxonomy cannot drift silently."""

    def _repo(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        repo, _marker = make_demo_repo(Path(td.name))
        return repo

    def test_config_error_is_not_retryable(self) -> None:
        repo = self._repo()
        (repo / ".mergetrain.yaml").write_text("git:\n  push_refs: []\n", encoding="utf-8")
        error = _run_json(["--repo", str(repo), "doctor"])["error"]
        self.assertEqual(error["code"], "config_error")
        self.assertFalse(error["retryable"])

    def test_queue_error_is_not_retryable(self) -> None:
        repo = self._repo()
        error = _run_json(["--repo", str(repo), "inspect", "999"])["error"]
        self.assertEqual(error["code"], "queue_error")
        self.assertFalse(error["retryable"])

    def test_duplicate_active_branch_is_not_retryable(self) -> None:
        repo = self._repo()
        argv = ["--repo", str(repo), "enqueue", "--task", "a",
                "--branch", "feature/a", "--no-ready-check"]
        _run_json(argv)  # the first enqueue succeeds
        error = _run_json(argv)["error"]  # the duplicate is rejected
        self.assertEqual(error["code"], "duplicate_active_branch")
        self.assertFalse(error["retryable"])

    def test_lock_held_is_retryable(self) -> None:
        # LockHeld / LostLease are the ONLY retryable=true classes on the generic
        # failure path — pin the flag, not just the code.
        repo = self._repo()
        conn = connect(_db(repo))
        try:
            enqueue_job(conn, task="a", branch="feature/a")
            acquire_runner_lock(conn, owner=default_owner())  # a live lock this process owns
        finally:
            conn.close()
        error = _run_json(["--repo", str(repo), "run-batch", "--deploy"])["error"]
        self.assertEqual(error["code"], "lock_held")
        self.assertTrue(error["retryable"])

    def test_remote_unreachable_from_recovery_is_retryable(self) -> None:
        # The recovery commands use a SECOND rule -- exit code 3 or 7 means
        # retryable -- so a code can be retryable there while its class is not
        # on the generic path. docs/contract.md got this wrong for
        # remote_unreachable until this test existed.
        repo = self._repo()
        conn = connect(_db(repo))
        try:
            job = enqueue_job(conn, task="a", branch="feature/a")
            conn.execute(
                "UPDATE deploy_queue SET status='needs_reconcile', "
                "pending_deploy_sha=?, push_status='pending' WHERE id=?",
                ("a" * 40, job.id),
            )
            conn.commit()
        finally:
            conn.close()
        # Do NOT rewrite the config here: make_demo_repo points state.db at an
        # absolute path, so a fresh config would send reconcile at an empty
        # database and it would never reach the remote at all.
        subprocess.run(
            ["git", "remote", "set-url", "origin", str(repo / "does-not-exist.git")],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        payload = _run_json(["--repo", str(repo), "reconcile"])
        self.assertEqual(payload["error"]["code"], "remote_unreachable")
        self.assertTrue(payload["error"]["retryable"])

    def test_documented_surface_coverage_matches_the_golden(self) -> None:
        # docs/contract.md describes the coverage the freeze promises, and it had
        # already drifted: gc_applied was under the gate but missing from the
        # list. A hand-maintained description of the enforcement mechanism is the
        # worst place for a stale claim, so check it rather than trust it.
        import re

        doc = (
            Path(__file__).resolve().parents[1] / "docs" / "contract.md"
        ).read_text(encoding="utf-8")
        paragraph = doc.split("Coverage is")[1].split("Run results share")[0]
        documented = set(re.findall(r"`([a-z_]+)`", paragraph))
        golden = set(json.loads(GOLDEN.read_text(encoding="utf-8"))["surfaces"])

        self.assertEqual(
            golden - documented,
            set(),
            "surfaces under the gate that docs/contract.md does not list",
        )
        stated = re.search(r"currently (\d+) surfaces", paragraph)
        self.assertIsNotNone(stated, "the coverage paragraph no longer states a count")
        # The count is payload surfaces; _jsonl_frames is named separately.
        self.assertEqual(int(stated.group(1)), len(golden) - 1)

    def test_documented_error_codes_match_the_implementation(self) -> None:
        # The freeze tells consumers to branch on error.code, so the documented
        # vocabulary has to BE the vocabulary. Two codes were absent from every
        # document until this cycle; nothing compared the two lists.
        import re

        from mergetrain import errors as errors_module

        doc = (
            Path(__file__).resolve().parents[1] / "docs" / "contract.md"
        ).read_text(encoding="utf-8")
        table = doc.split("## `error.code` vocabulary")[1].split("\n\n##")[0]
        documented = {
            row.group(1): "yes" in row.group(2).lower()
            for row in re.finditer(r"^\| `([a-z_]+)` \| ([^|]+) \|", table, re.M)
        }

        def wire_code(name: str) -> str:
            return "".join(
                f"_{char.lower()}" if char.isupper() else char for char in name
            ).lstrip("_")

        implemented = {
            wire_code(name)
            for name, obj in vars(errors_module).items()
            if isinstance(obj, type)
            and issubclass(obj, Exception)
            and obj.__module__ == errors_module.__name__
        }
        source_root = Path(__file__).resolve().parents[1] / "src" / "mergetrain"
        cli_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(source_root.rglob("*.py"))
        )
        implemented |= set(re.findall(r'error_code="([a-z_]+)"', cli_sources))
        implemented |= set(
            re.findall(r'_error_payload\(\s*\n?\s*"([a-z_]+)"', cli_sources)
        )

        self.assertEqual(
            implemented - set(documented),
            set(),
            "error.code values the code can emit but docs/contract.md does not list",
        )
        self.assertEqual(
            set(documented) - implemented,
            set(),
            "error.code values documented but not emitted anywhere",
        )
        # And the flags the other tests pin must agree with the table.
        self.assertTrue(documented["lock_held"])
        self.assertTrue(documented["lost_lease"])
        self.assertTrue(documented["remote_unreachable"])
        self.assertFalse(documented["config_error"])

    def test_interrupted_envelope_has_the_full_shape(self) -> None:
        repo = self._repo()
        with mock.patch(
            "mergetrain.commands.inspection.config_from_args",
            side_effect=KeyboardInterrupt,
        ):
            payload = _run_json(["--repo", str(repo), "doctor"])
        self.assertEqual(payload["error"]["code"], "interrupted")
        self.assertEqual(payload["error"]["message"], "interrupted")
        self.assertFalse(payload["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
