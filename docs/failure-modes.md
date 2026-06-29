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

Push failures mark jobs as `failed`. Inspect the job log path from
`mergetrain status --json`.

## Post-push verify failure

Verify hooks run after push. The remote ref may already be updated, so jobs are
marked `deployed` with a warning note instead of `failed`.

## Stale lock

The runner lock records an owner and a lease expiry. A running runner refreshes
its lease throughout a job, so an active runner's lease is always valid.

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
