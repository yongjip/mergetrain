# Agent contract

Agents interacting with mergetrain must follow this contract.

## Rules

1. Work on a task-specific branch and worktree.
2. Commit all changes before enqueueing.
3. Do not push deploy refs directly.
4. Read `mergetrain doctor --json` or `mergetrain status --json` before deciding
   the next action.
5. Use `--auto` only after explicit unattended-deploy approval.
6. Reuse validated gates only after explicit config or `--reuse-validated`
   authorization.
7. Let one runner or daemon own merge, test, push, and verify.
8. Fix blocked or failed work in the owning branch, commit a clean result, then
   enqueue a new job.

## Machine-readable contract

```sh
mergetrain agent-contract --json
```

The JSON payload includes `name`, `purpose`, `rules`, and `boundary`.

## Next-action guidance

`mergetrain doctor --json` returns `next_action` values:

- `wait_for_runner`
- `deploy_validated_train_when_approved`
- `cancel_and_reenqueue_legacy_validated_jobs`
- `run_daemon_or_run_batch_deploy_when_approved`
- `run_batch_validate`
- `fix_blocked_job`
- `gc_available`
- `enqueue_clean_branch`

`next_action` is advisory. It does not replace user approval for deploy,
unattended auto deploy, or destructive cleanup.

When `validated_trains` is non-empty, approval applies to the displayed train
identity and member HEADs. A later deploy must not silently include newer
queued jobs. Validated-but-not-deployed branches are not GC deletion candidates.
Deploy approval by itself does not authorize gate reuse; that is a separate,
explicit policy decision.
