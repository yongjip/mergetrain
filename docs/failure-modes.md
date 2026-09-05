# Failure modes

## Merge conflict

A branch that cannot merge into the integration worktree is marked `blocked`.
Fix it on the owning branch, commit the clean result, and run
`mergetrain retry <blocked-job-id>`. Enqueueing the same active branch again is
rejected; retry atomically replaces the old blocked/failed outcome.

```sh
git switch <blocked-branch>
git fetch <remote>
git rebase <remote>/<integration-branch>
# resolve conflicts
git add .
git rebase --continue
# Finish the rebase and commit any further fixes; leave the worktree clean.
# Atomically dismiss the old outcome and enqueue a fresh SHA-pinned job:
mergetrain retry <blocked-job-id>
```

Or let mergetrain fetch and start the rebase with
`mergetrain retry <blocked-job-id> --rebase`. A rebase conflict leaves the
worktree ready for manual resolution and does not dismiss the old job. `retry`
only replaces a blocked/failed job (never queued or in-progress work), inherits
its task and note, and captures fresh base/head SHAs. It inherits `--auto`
eligibility only while both the approved destination and execution-policy
hashes still match; a changed destination, gate/reuse policy, command timeout,
or verify hook turns the replacement into a manual job.

## Deploy authorization changed

Auto jobs bind approval to a credential-free hash of the fetch URL, the one
effective push URL (including `remote.<name>.pushurl`), integration ref, push
refs, and permanent audit-ref policy. Multiple push URLs and relative local
push paths are rejected. The daemon checks that identity inside claim, and the
runner resolves it again after gates immediately before any recovery marker or
push. A mismatch finishes the job `blocked` with
`approval_destination_changed` and `inspect` category
`deploy_authorization_changed`. Review the new destination. To create a fresh
auto-approved job, dismiss the old blocked job, then enqueue the clean committed
branch with `--auto` only if unattended deployment is approved for that new
destination. A retry after an identity change creates a manual replacement.

Auto jobs separately bind the execution policy: effective gates, default
command timeout, validation-reuse configuration and authorization, and verify
hooks. The daemon compares that identity inside claim, and the runner reloads
the control checkout before gates and immediately before the push marker. A
blank legacy identity or mismatch becomes `blocked` with
`approval_execution_policy_changed`, under the same `inspect` category. Review
the current QA/deploy/verify policy. Dismiss the old blocked job before enqueueing
a fresh job with newly authorized `--auto`, or use retry for a manual replacement;
do not retrofit a hash onto the old row.

MCP and other preview-driven confirmations use the broader deploy-plan hash.
If the train, destination, gate/reuse policy, or verify hooks change, the CLI
returns `deploy_plan_changed` before claim or blocks before push. Generate a new
preview/summary; do not reuse the stale hash.

## Gate failure

Gate failures are pre-push failures. The deploy ref is not updated. In batch
mode, mergetrain isolates the failure so unrelated jobs can still
validate/deploy:

- A **one-job train** is re-run through the full merge → gate → (deploy) path.
- **Multi-job trains** are probed by re-assembling and gating subsets until the
  failure is pinned to either
  an individually failing job (finished `failed`) or a **semantic conflict**
  — jobs that pass gates alone but fail combined. Conflicting jobs finish
  `blocked` with both partners' SHAs in the note and a machine-readable
  `conflict_with` field listing the partner job IDs. Surviving jobs are
  re-run as a fresh train, so nothing ships without a full gate pass over the
  exact final combination. This classification is independent of unrelated
  compatible jobs being added to or removed from the batch.

To resolve a semantic conflict, rebase one side onto the other (or onto the
integration branch with the other side merged), fix the joint breakage, commit
a clean result, and run `mergetrain retry <blocked-job-id>` for each affected
job that should rejoin the queue.

## Push failure

A push the remote **rejects for policy/permission** — a protected branch, a
required pull request, a denied ref update, a declined pre-receive hook — is a
repo-configuration issue, not bad code. mergetrain parks that job **`blocked`**
(not `failed`, which would tell an agent to rebase and re-enqueue) with
`push_status=failed`, and `inspect` reports the `push_rejected` category. Fix
the branch protection / push permission (or point `git.push_refs` at a branch
you can push, and land through your forge's own flow), then re-deploy. Because
this rejection proves that no ref update landed, mergetrain clears the durable
push marker and its pending pin.

Any other push error after the write-ahead marker is durable — including a
transport drop or timeout — has an **ambiguous remote outcome**. The remote may
have accepted the atomic update before the client lost the response, so the job
is parked `needs_reconcile` with `push_status=failed` and its marker/pin intact;
it is never made terminal `failed` and blindly pushed again. All deploy paths
pause until `mergetrain reconcile --apply` checks the remote and resolves it.
The marker also carries a credential-free hash of the endpoint used by the
attempt. Reconcile fails closed if the recorded remote name now resolves to a
different push endpoint, rather than inspecting the fetch URL or a newly
configured `pushurl`. Markers created before v2.3.1 have no provable endpoint
hash and remain parked after migration; reconcile reports the legacy marker
without contacting whichever endpoint happens to be configured now. Resolve
such an interrupted v2.3.0 deploy before upgrading, or preserve its evidence
for explicit operator inspection.

Use `mergetrain logs <job-id> --tail 200` for the raw git diagnostics either way.

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
`ok=true`, human output names both outcomes, the final completion event remains
a warning, and status plus every dashboard/Hub view keep the job in Attention.
A later deployment does not dismiss the failure. Run the exact
`status.next_action.command` to recheck it; one `verify --job` invocation runs
the recorded policy once and resolves every member of that deployment. If the
policy is missing or has changed, mergetrain refuses to infer success and
requires explicit review through `verify --job <id> --ack succeeded|failed`.
Legacy rows without a provable deployment/policy identity can be acknowledged
but are not automatically grouped or re-run.

## Queue database contention

SQLite allows one writer at a time. If another process holds the write lock past
`busy_timeout` (5s), a queue write raises `error.code: queue_busy`,
`retryable: true` — never `failed`. `failed` means *the branch is at fault, fix it
and enqueue a fresh commit*, and contention says nothing about the branch.

The runner then writes **nothing**, with one exception: if it pushed and saw the
refs land, it still finalizes `deployed` (with the contention recorded as a
warning), because that is the one thing it knows for certain.

Everything else is left exactly as the last durable write left it, which is
indistinguishable from a crash at the same instant — deliberately, because that
is a state the recovery machinery already understands. What the row looks like
depends only on how far the deploy got before the contention:

| Contention hit | Row is left | `reconcile --apply` / the next claim resolves it to |
|---|---|---|
| before the write-ahead marker | `in_progress`, no marker, remote untouched | `queued` |
| after the marker, push outcome unknown | `in_progress` with its marker and pin ref | `needs_reconcile`, then the remote decides |
| after the refs landed | finalized `deployed` with a warning | nothing to do |

The split is made by `store.recover_orphans` from **durable evidence** (is there
a marker?), never from what the failing run believed — an in-memory push status
can belong to a different frame, or describe a marker write that never committed.

Two consequences worth knowing:

- `queue_busy` does **not** mean "nothing was pushed". It means "a queue write
  did not happen". The refs may well be on the remote; read `status --json` and,
  if the row carries a marker, run `reconcile`.
- A row left `in_progress` with no runner lock is a stranded claim.
  `status` reports `next_action.code: reconcile_stranded_claim`;
  `mergetrain reconcile --apply` clears it.

  Recovering it **dissolves any validated-train identity it carried**, on
  purpose: a requeued row asserting a validation it no longer holds would
  collateral-block unrelated auto deploys. A later `deploy` validates the
  current queued set and presents a new exact plan, so the dissolved identity
  cannot silently inherit approval. The requeue also records the dissolution
  and prior train in the job note.

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
mergetrain status --diagnose --json
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
group and records `canceled`. Once the durable marker exists, cancellation no
longer overrides remote truth: the runner continues to renew ownership without
interrupting the irreversible remote update. If the push outcome is ambiguous,
the job remains `needs_reconcile` with the cancellation request preserved;
reconcile records `deployed` when every ref landed or `canceled` when none did.
Calling `cancel` directly on a `needs_reconcile` job is refused until that
remote check is applied.

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
instead. A successful deploy or an unambiguous policy rejection clears both the
DB marker and pending pin; an ambiguous outcome and a reconcile conflict retain
them as recovery evidence. Each successful atomic push also retains
`refs/mergetrain/deploys/<sha>` remotely. If every payload ref later loses that
SHA but the audit ref remains, reconcile knows the deploy landed before the
rewrite and blocks instead of replaying it. Deployments made before this audit
ref existed retain the legacy ambiguity and are classified from payload
ancestry alone.
