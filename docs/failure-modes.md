# Failure modes

## Merge conflict

A branch that cannot merge into the integration worktree is marked `blocked`.
Fix it on the owning branch, commit the clean result, and enqueue a new job.

```sh
git switch <blocked-branch>
git fetch <remote>
git rebase <remote>/<integration-branch>
# resolve conflicts
git add .
git commit --amend
mergetrain enqueue --task "rebased task" --branch <blocked-branch> --capture-sha
```

## Gate failure

Gate failures are pre-push failures. The deploy ref is not updated. In batch
mode, mergetrain isolates the failure so unrelated jobs can still
validate/deploy:

- **Trains of up to 3 jobs** are isolated one-by-one: each merged job is
  re-run individually through the full merge → gate → (deploy) path.
- **Larger trains** are bisected: subsets are re-assembled and gated
  (O(log n) gate runs instead of O(n)) until the failure is pinned to either
  an individually failing job (finished `failed`) or a **semantic conflict**
  — jobs that pass gates alone but fail combined. Conflicting jobs finish
  `blocked` with both partners' SHAs in the note and a machine-readable
  `conflict_with` field listing the partner job IDs. Surviving jobs are
  re-run as a fresh train, so nothing ships without a full gate pass over
  the exact final combination.

To resolve a semantic conflict, rebase one side onto the other (or onto the
integration branch with the other side merged), fix the joint breakage, and
enqueue a fresh job.

## Push failure

Push failures mark jobs as `failed` with `push_status=failed` and
`verify_status=not_run`. Use `mergetrain inspect <job-id> --json` for the stable
`push_failed` category and `mergetrain logs <job-id> --tail 200` for explicit raw
diagnostics.

## Validated-gate reuse declined

Reuse is an opt-in optimization, not a deploy prerequisite. If the integration
ref, task head, train membership, validation commit/tree, gate policy,
environment fingerprint, or age differs, `on_mismatch: rerun` records a warning
event and performs the full reassembly and gate run. `on_mismatch: fail` blocks
before push. A missing or changed task head remains fail-closed even when the
general mismatch policy is rerun.

## Post-push verify failure

Verify hooks run after push. The remote ref is already updated, so jobs remain
`deployed` with `push_status=succeeded` and `verify_status=failed` instead of
being rewritten as a pre-push failure. Run JSON returns `result=warning` and
`ok=false`, human output names both outcomes, the final completion event remains
a warning, and the dashboard keeps the job in its Attention history.

## Stale lock

The runner lock records an owner, unique token, and lease expiry. Claimed jobs
store the same token. Managed subprocesses renew the lease periodically, and a
refresh or state update with a stale token fails immediately.

Reclaim rules when another runner tries to acquire:

- **Dead owner PID** — reclaimed immediately.
- **Valid (non-expired) lease** — never stolen, whether the owner PID looks alive
  or unknown. This is the concurrency guarantee.
- **Expired lease, no in-progress jobs** — reclaimed, even if the owner PID still
  looks alive. A healthy runner would have refreshed its lease, so an expired
  lease means the owner is dead, hung, or a recycled PID. This prevents a reused
  PID from holding an abandoned lock open forever.
- **Expired lease with in-progress jobs** — not auto-reclaimed; left for operator
  investigation.

Inspect with:

```sh
mergetrain doctor --json
mergetrain status --json
mergetrain inspect <job-id> --json
mergetrain events --job <job-id> --after <last-event-id> --follow --jsonl
```

A scoped event follower emits `stream_end.reason=lost_lease` and exits `1` if an
`in_progress` job no longer matches a live runner lease. This distinguishes an
abandoned run from a merely quiet long-running gate.

## Orphan `in_progress`

If the runner lock is gone and `in_progress` jobs remain, the next lock claim
re-queues them with this note:

```text
re-queued by mergetrain (previous runner gone)
```

If an orphan already had `cancel_requested_at`, recovery finalizes it as
`canceled` instead of re-queueing it.

## Cancellation while running

Cancellation is cooperative until atomic push begins. `cancel` records a
request for the whole active claim; the runner heartbeat terminates the process
group and records `canceled`. Once push begins, the runner continues to renew
ownership without interrupting the irreversible remote update and records the
actual deployed result.

## Command timeout

Git operations, gates, and verify hooks are bounded by
`queue.command_timeout_seconds`. A timeout terminates the process group and is
reported as a command failure; pre-push timeouts leave deploy refs unchanged.

## Temporary worktrees

Dry-run cleanup:

```sh
mergetrain gc --json
```

Apply cleanup:

```sh
mergetrain gc --apply --json
```

Delete terminal local branches as well:

```sh
mergetrain gc --apply --delete-branches --json
```

## Why a persisted marker, instead of reconstructing from Git?

A question worth answering once, properly (it came up in the launch thread —
see issue #38's origin): after a crash, why does recovery need the SQLite
marker at all, when the Git objects and refs are all still there?

Because Git alone cannot distinguish **"never pushed"** from **"pushed, then
died before hearing back."** The local objects, the assembled commit, even the
pin ref look identical in both worlds; the only difference is on the remote.
So the runner persists two things *before* the push — the lease (a SQLite lock
row with token, heartbeat, TTL, and PID liveness) and a fsynced
`pending_deploy_sha` marker plus a `refs/mergetrain/pending/<id>` pin — and
recovery then reads the marker as *what we intended* and asks the remote for
*what actually happened*. A train is marked deployed only when the push ref
really carries its SHA; a landed train is never pushed twice; and when the
remote is unreachable, reconcile refuses to guess and exits with its own code
instead.
