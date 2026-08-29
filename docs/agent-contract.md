# Agent contract

Agents interacting with mergetrain must follow this contract.

## Rules

1. Work on a task-specific branch and worktree.
2. Commit all changes before enqueueing.
3. Do not push configured Git refs directly. Task agents hand off by enqueueing
   the exact committed HEAD, then stop unless separately authorized as the
   runner.
4. Read `mergetrain doctor --json` or `mergetrain status --json` before deciding
   the next action.
5. Use `--auto` only after explicit unattended-deploy approval.
6. Reuse validated gates only after explicit config or `--reuse-validated`
   authorization.
7. Let one separately authorized runner or daemon own merge, test, push, and
   verify. A task, merge, integration, or enqueue request is not deploy
   approval.
8. Fix blocked or failed work in the owning branch, commit a clean result, then
   run `mergetrain retry <job-id>` to enqueue a fresh SHA-pinned job.
9. Replace a validated train only with `mergetrain supersede`; validate and
   approve the replacement as a new SHA-pinned train.
10. Do not delete or rewrite remote `refs/mergetrain/deploys/*`; they are
    permanent recovery evidence.

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
- `recover_stranded_claim`
- `initialize_config`
- `gc_available`
- `enqueue_clean_branch`

`next_action` is advisory. It does not replace user approval for deploy,
unattended auto deploy, or destructive cleanup.

For a task agent, a successful exact-SHA enqueue is the terminal handoff.
`run_batch_validate` and deploy-oriented next actions are runner/operator
guidance, not permission to continue after enqueueing. The same agent may act as
the runner only when that role is separately authorized; deploy still requires
approval for the displayed exact validated train.

After a crash or ambiguous push response, `reconcile`/`recover` resolve
`needs_reconcile` jobs against the remote (never re-pushing a landed deploy);
`run-batch --deploy` is refused while any job is `needs_reconcile`. See the
[failure modes guide](failure-modes.md).

When `validated_trains` is non-empty, approval applies to the displayed train
identity and member HEADs. A later deploy must not silently include newer
queued jobs. Validated-but-not-deployed branches are not GC deletion candidates.
Deploy approval by itself does not authorize gate reuse; that is a separate,
explicit policy decision.

Changing an approved train also invalidates approval. `supersede` records the
old/new relationship atomically for audit, but the replacement never inherits
validation, gate-reuse identity, or deploy authorization.

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
