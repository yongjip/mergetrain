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
trainyard enqueue --task "rebased task" --branch <blocked-branch> --capture-sha
```

## Gate failure

Gate failures are pre-push failures. The deploy ref is not updated. In batch
mode, trainyard attempts to isolate merged jobs one-by-one so unrelated jobs can
still validate/deploy.

## Push failure

Push failures mark jobs as `failed`. Inspect the job log path from
`trainyard status --json`.

## Post-push verify failure

Verify hooks run after push. The remote ref may already be updated, so jobs are
marked `deployed` with a warning note instead of `failed`.

## Stale lock

The runner lock records an owner and expiry. A dead PID can be reclaimed. A live
PID is not stolen. An unknown owner with in-progress work is not automatically
reclaimed.

Inspect with:

```sh
trainyard doctor --json
trainyard status --json
```

## Orphan `in_progress`

If the runner lock is gone and `in_progress` jobs remain, the next lock claim
re-queues them with this note:

```text
re-queued by trainyard (previous runner gone)
```

## Temporary worktrees

Dry-run cleanup:

```sh
trainyard gc --json
```

Apply cleanup:

```sh
trainyard gc --apply --json
```

Delete terminal local branches as well:

```sh
trainyard gc --apply --delete-branches --json
```
