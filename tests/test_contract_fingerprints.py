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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mergetrain.cli import main
from mergetrain.config import load_config
from mergetrain.contract import CONTRACT_VERSION
from mergetrain.store import connect, enqueue_job, record_run_event
from test_git_runner import make_demo_repo

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
    return _run_json(["--repo", str(repo), "gc"])


def _cap_reconcile(repo):
    return _run_json(["--repo", str(repo), "reconcile"])


def _cap_inspect(repo):
    conn = connect(_db(repo))
    job = enqueue_job(conn, task="a", branch="feature/a")
    conn.close()
    return _run_json(["--repo", str(repo), "inspect", str(job.id)])


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
    return frames


SURFACES = {
    "doctor": _cap_doctor,
    "status": _cap_status,
    "version": _cap_version,
    "agent_contract": _cap_agent_contract,
    "enqueue": _cap_enqueue,
    "run_batch_validate": _cap_run_validate,
    "gc": _cap_gc,
    "reconcile": _cap_reconcile,
    "inspect": _cap_inspect,
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


if __name__ == "__main__":
    unittest.main()
