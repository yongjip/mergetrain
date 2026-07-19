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
mode, mergetrain attempts to isolate merged jobs one-by-one so unrelated jobs can
still validate/deploy.

## Push failure

Push failures mark jobs as `failed` with `push_status=failed` and
`verify_status=not_run`. Inspect the job log path from `mergetrain status --json`.

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
```

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
