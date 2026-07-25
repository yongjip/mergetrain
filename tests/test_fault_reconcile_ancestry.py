"""Reconcile ancestry: the remote tip MOVED after the deploy landed (0.3.0 §5).

``tests/test_reconcile.py`` pins the two equality-shaped remote states — the push
ref sits exactly at the recorded sha (``deployed``), or still at the pre-deploy
base (``queued``). Neither notices if ``merge-base --is-ancestor`` were
simplified to ``remote_sha == pending_deploy_sha``. In a busy repo that
simplification is wrong for the *most likely* crash shape: between the ambiguous
push and the reconcile, a third party lands a hotfix, so the recorded sha becomes
a strict **ancestor** of the tip. Equality would read that as "never landed",
requeue the train, and re-push it over somebody else's commit — exactly the
"lie about deployed" the 1.0 recovery gate forbids.

The three ancestry shapes pinned here, all against a real bare remote:

* tip is a strict **descendant** of the recorded sha → ``deployed``, and the
  healed row keeps the *recorded* sha (not the tip) as ``deploy_sha``;
* tip is an **ancestor** of it (remote rewound to base) → ``queued``;
* tip has **diverged** from it → never ``deployed``, never ``failed``, remote
  untouched (see the docstring of the diverged test for the documented verdict).

Every case also asserts the remote is byte-identical before and after: reconcile
reads truth, it never pushes.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# Reuse the bare-remote fixture from the sibling test module. `unittest discover
# -s tests` puts this dir on sys.path; add it explicitly so a single-module run
# (python -m unittest tests.test_fault_reconcile_ancestry) resolves it too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_git_runner import git, make_demo_repo

from mergetrain.config import load_config
from mergetrain.git_runner import git_ref_exists, pending_ref_name
from mergetrain.recovery import reconcile
from mergetrain.store import (
    connect,
    enqueue_job,
    get_job,
    mark_job,
    record_pending_push,
    utc_now,
)


def _assemble_train_head(repo: Path) -> str:
    """A real train HEAD (main + feature/a) that no branch points at.

    Mirrors what the runner pushes: a merge commit living only in the object
    store, kept alive by the pin ref.
    """
    git(repo, "switch", "-c", "_train_tmp", "main")
    git(repo, "merge", "--no-ff", "-m", "train", "feature/a")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    git(repo, "branch", "-D", "_train_tmp")
    return sha


def _pending_refs(repo: Path) -> str:
    return git(repo, "for-each-ref", "--format=%(refname)", "refs/mergetrain/pending/")


def _park_with_marker(conn, repo: Path, config, job_id: int, sha: str) -> None:
    """Drive a job into the post-crash ``needs_reconcile`` shape the product writes.

    Uses the real durable-marker API (``record_pending_push`` + the pin ref) so the
    marker carries the same remote/push-ref target a live push would have recorded;
    a hand-written UPDATE would silently exercise only the legacy fallback path.
    """
    conn.execute(
        "UPDATE deploy_queue SET status='in_progress', claim_token=?, started_at=? WHERE id=?",
        ("t-crash", utc_now(), job_id),
    )
    conn.commit()
    record_pending_push(
        conn,
        job_ids=[job_id],
        deploy_sha=sha,
        claim_token="t-crash",
        remote=config.git.remote,
        push_refs=config.git.push_refs,
    )
    git(repo, "update-ref", pending_ref_name(job_id), sha)
    mark_job(
        conn,
        job_id,
        status="needs_reconcile",
        note="parked for reconcile after previous runner stopped",
        expected_claim_token="t-crash",
    )


def _third_party_clone(root: Path, remote: Path) -> Path:
    """A second clone standing in for another human/CI pushing to the same remote.

    Commits made here are absent from the mergetrain repo's object store until
    reconcile fetches them. That matters: if reconcile stopped localizing the
    remote tip, ``merge-base --is-ancestor`` could not resolve it and the verdict
    would degrade from ``deployed`` to ``blocked``. ``--branch main`` avoids
    depending on the bare repo's HEAD, which ``git init --bare`` may leave
    pointing at a branch that was never created.
    """
    other = root / "third-party"
    git(root, "clone", "--branch", "main", str(remote), str(other))
    git(other, "config", "user.email", "hotfix@example.invalid")
    git(other, "config", "user.name", "Hotfix Bot")
    return other


class ReconcileAncestryTests(unittest.TestCase):
    def _stage(self, root: Path):
        """A landed-but-unconfirmed deploy: remote at the train HEAD, job parked."""
        repo, _ = make_demo_repo(root)
        remote = root / "remote.git"
        config = load_config(repo=repo)
        conn = connect(config.state.db)
        job = enqueue_job(conn, task="a", branch="feature/a")
        base = git(repo, "rev-parse", "main")
        pending = _assemble_train_head(repo)
        _park_with_marker(conn, repo, config, job.id, pending)
        # Guard the "pin released" assertions below against going vacuous: if the
        # pin were never written (or written under a renamed prefix), an empty
        # listing after reconcile would prove nothing about reconcile releasing it.
        self.assertEqual(_pending_refs(repo), pending_ref_name(job.id))
        return repo, remote, config, conn, job, base, pending

    def test_third_party_hotfix_on_top_of_a_landed_deploy_stays_deployed(self) -> None:
        # The single most likely real-world reconcile shape: our push landed, then
        # someone else pushed on top before we recovered, so remote_sha != the
        # recorded sha while still containing it. A `remote_sha == pending sha`
        # comparison would requeue and re-push this train over the hotfix.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, remote, config, conn, job, _base, pending = self._stage(root)
            try:
                git(repo, "push", "origin", f"{pending}:main")  # the deploy landed
                other = _third_party_clone(root, remote)
                (other / "hotfix.txt").write_text("hotfix\n", encoding="utf-8")
                git(other, "add", "hotfix.txt")
                git(other, "commit", "-m", "hotfix on top of the landed deploy")
                git(other, "push", "origin", "main")
                tip = git(remote, "rev-parse", "main")

                self.assertNotEqual(tip, pending)  # the tip really moved
                # The hotfix commit is not in our object store yet; reconcile's own
                # fetch has to bring it local for the ancestry test to resolve.
                self.assertFalse(git_ref_exists(repo, tip))

                before = git(remote, "rev-parse", "main")
                outcome = reconcile(config, conn, apply=True)
                after = git(remote, "rev-parse", "main")
                healed = get_job(conn, job.id)
            finally:
                conn.close()

            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(outcome.summary["reconciled_deployed"], 1)
            self.assertEqual(outcome.summary["requeued"], 0)
            self.assertEqual(outcome.summary["conflicts"], 0)
            self.assertEqual(healed.status, "deployed")
            # The row records what WE shipped. Adopting the remote tip would
            # attribute the third party's hotfix to this train, and a later
            # verify/rollback would then act on a sha this job never pushed.
            self.assertEqual(healed.deploy_sha, pending)
            self.assertNotEqual(healed.deploy_sha, tip)
            self.assertEqual(healed.push_status, "succeeded")
            # reconcile can prove the push landed, never that verify hooks ran.
            self.assertEqual(healed.verify_status, "unknown")
            self.assertEqual(healed.pending_deploy_sha, "")
            self.assertEqual(_pending_refs(repo), "")  # pin released
            self.assertEqual(after, before)  # reconcile never pushes
            self.assertEqual(after, tip)  # the hotfix was not clobbered
            # The JSON contract must expose the ancestry, not just the verdict:
            # remote_sha is the moved tip while `contains` is still true.
            payload = outcome.jobs[0]
            self.assertEqual(payload["decision"], "deployed")
            self.assertEqual(payload["pending_deploy_sha"], pending)
            self.assertEqual(payload["push_refs"], [{"ref": "main", "remote_sha": tip, "contains": True}])

    def test_remote_rewound_below_the_recorded_sha_requeues(self) -> None:
        # Mirror twin of the case above: the recorded sha is absent from the
        # remote's history entirely (the push never landed, or was rolled back to
        # the base). Containment is definitively false, so requeue — and the
        # rollback must survive reconcile untouched.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, remote, config, conn, job, base, pending = self._stage(root)
            try:
                git(repo, "push", "origin", f"{pending}:main")
                # force the payload ref back below the deploy, as a human rollback
                # (`push --force <base>:main`) would leave it
                git(remote, "update-ref", "refs/heads/main", base)
                self.assertEqual(git(remote, "rev-parse", "main"), base)

                before = git(remote, "rev-parse", "main")
                outcome = reconcile(config, conn, apply=True)
                after = git(remote, "rev-parse", "main")
                healed = get_job(conn, job.id)
            finally:
                conn.close()

            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(outcome.summary["requeued"], 1)
            self.assertEqual(outcome.summary["reconciled_deployed"], 0)
            self.assertEqual(healed.status, "queued")
            # Nothing may claim a deploy happened, and the "push attempted,
            # outcome unknown" push_status must not survive a proven non-landing.
            self.assertEqual(healed.deploy_sha, "")
            self.assertEqual(healed.push_status, "not_run")
            self.assertEqual(healed.pending_deploy_sha, "")
            self.assertEqual(_pending_refs(repo), "")
            self.assertEqual(after, before)
            self.assertEqual(after, base)  # reconcile did not re-push the train
            self.assertEqual(outcome.jobs[0]["push_refs"][0]["contains"], False)

    def test_diverged_remote_tip_never_claims_deployed_or_failed(self) -> None:
        # Landed-then-rewound *sideways*: our sha reached main, then main was
        # force-pushed to a sibling commit built on the base, so the tip is
        # neither the recorded sha nor a descendant of it.
        #
        # Verdict today is `queued`, and that is deliberate, not an oversight:
        # docs/proposals/0.3.0-recovery.md §5 defers landed-then-rewound detection
        # to Phase 3 because distinguishing "never landed" from "landed, then
        # rewritten" requires the `refs/mergetrain/deploys/<sha>` audit ref, which
        # is not pushed yet. What must hold either way is
        # the truthfulness gate: a diverged remote may never be reported as
        # deployed and may never be parked terminal `failed` — a `failed` row
        # would be a lie the operator cannot recover from, and a `deployed` row
        # would credit a sha the payload ref no longer carries. When the audit ref
        # lands, this test is the place that should flip to `blocked`.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, remote, config, conn, job, base, pending = self._stage(root)
            try:
                git(repo, "push", "origin", f"{pending}:main")  # the deploy landed
                other = _third_party_clone(root, remote)
                git(other, "switch", "-c", "sidetrack", base)
                (other / "sidetrack.txt").write_text("elsewhere\n", encoding="utf-8")
                git(other, "add", "sidetrack.txt")
                git(other, "commit", "-m", "rewrite main sideways")
                diverged = git(other, "rev-parse", "HEAD")
                git(other, "push", "--force", "origin", f"{diverged}:main")

                # neither direction of ancestry holds: a true fork
                self.assertNotEqual(diverged, pending)
                self.assertEqual(git(remote, "rev-parse", "main"), diverged)
                # Why the rewind is undetectable: the remote holds no record of
                # what mergetrain pushed, only the payload ref's current value.
                # This documents the fixture's premise, not a Phase 3 tripwire —
                # the push here is staged by hand, so no product code could write
                # an audit ref in this test even once Phase 3 ships.
                self.assertEqual(
                    git(remote, "for-each-ref", "--format=%(refname)", "refs/mergetrain/"),
                    "",
                )

                before = git(remote, "rev-parse", "main")
                outcome = reconcile(config, conn, apply=True)
                after = git(remote, "rev-parse", "main")
                healed = get_job(conn, job.id)
            finally:
                conn.close()

            # the truthfulness gate, independent of which verdict Phase 3 picks
            self.assertNotEqual(healed.status, "deployed")
            self.assertNotEqual(healed.status, "failed")
            self.assertEqual(healed.deploy_sha, "")
            self.assertEqual(after, before)  # reconcile did not push over the fork
            self.assertEqual(after, diverged)
            # the documented (Phase-2) verdict and its reason
            self.assertEqual(healed.status, "queued")
            self.assertEqual(outcome.summary["requeued"], 1)
            self.assertIn("push did not land", outcome.jobs[0]["reason"])
            self.assertEqual(outcome.jobs[0]["push_refs"][0]["remote_sha"], diverged)
            self.assertEqual(outcome.jobs[0]["push_refs"][0]["contains"], False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
