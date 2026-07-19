# Agent contract

Agents interacting with mergetrain must follow this contract.

## Rules

1. Work on a task-specific branch and worktree.
2. Commit all changes before enqueueing.
3. Do not push configured Git refs directly.
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

The JSON payload includes `name`, `purpose`, `rules`, `boundary`, and
`human_vocabulary`. The latter reflects `terminology.git_operation` while
documenting the stable `deployed`/`deploy_sha` machine contract.

With `terminology.git_operation: integrate`, generated
`AGENTS.mergetrain.md`/`CLAUDE.mergetrain.md`, the dashboard, and human CLI
output call the atomic Git ref update “integration.” That approval does not
authorize a downstream provider release; provider verification/release remains
a separate action.

## Next-action guidance

`mergetrain doctor --json` returns `next_action` values:

- `unlock_wedged_runner`
- `wait_for_runner`
- `reconcile_pending_deploy`
- `reconcile_conflict_manual`
- `fix_blocked_job`
- `verify_reconciled_deploy`
- `deploy_validated_train_when_approved`
- `cancel_and_reenqueue_legacy_validated_jobs`
- `run_daemon_or_run_batch_deploy_when_approved`
- `run_batch_validate`
- `gc_available`
- `enqueue_clean_branch`

`next_action` is advisory. It does not replace user approval for deploy,
unattended auto deploy, or destructive cleanup.

After a crash, `reconcile`/`recover` resolve `needs_reconcile` jobs against the
remote (never re-pushing a landed deploy); `run-batch --deploy` is refused while
any job is `needs_reconcile`. See the [failure modes guide](failure-modes.md).

When `validated_trains` is non-empty, approval applies to the displayed train
identity and member HEADs. A later deploy must not silently include newer
queued jobs. Validated-but-not-deployed branches are not GC deletion candidates.
Deploy approval by itself does not authorize gate reuse; that is a separate,
explicit policy decision.

When a runner is active, observe it with read-only commands instead of inspecting
the process tree:

```sh
mergetrain inspect <job-id> --json
mergetrain events --job <job-id> --after <last-event-id> --follow --jsonl
mergetrain logs <job-id> --follow --tail 20
```

Only persisted `type=event` IDs are resume cursors. `type=heartbeat` is ephemeral,
and `type=stream_end` states why a scoped follower stopped. Treat `logs` as raw
command output that may be sensitive; structured events do not include that
output.
