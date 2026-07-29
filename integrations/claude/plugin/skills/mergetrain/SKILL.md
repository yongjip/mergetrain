---
name: mergetrain
description: Operate a local mergetrain queue for coding-agent branches. Use when inspecting queue health, enqueueing committed work, validating a train, following progress, or recovering from blocked and crashed runs.
---

# Operate mergetrain

<!-- BEGIN GENERATED: mergetrain-agent-protocol -->
Purpose: Serialize committed local task branches through one merge/test/push/verify runner.

### Rules

1. Work on a task-specific branch and worktree.
2. Commit all changes before enqueueing.
3. Do not push configured Git refs directly; enqueue the branch instead.
4. Read doctor --json or status --json before deciding the next action.
5. Use --auto only after explicit unattended-deployment approval from the user/operator.
6. Reuse validated gates only after explicit deploy.reuse configuration or --reuse-validated authorization.
7. Let one runner or daemon own merge, test, push, and verify.
8. Fix blocked or failed work in the owning branch and commit a clean result, then run mergetrain retry <id> to dismiss the old outcome and enqueue a fresh SHA-pinned job.
9. Replace a validated train only with mergetrain supersede; the replacement is a new SHA-pinned train that requires fresh validation and deploy approval.
10. After a crash, run reconcile/recover to resolve needs_reconcile jobs against the remote before deploying; run reconcile before any manual force-push.

### Safety boundary

- Git deployment requires `run-next --deploy` or `run-batch --deploy`; `--deploy` remains the canonical compatibility flag.
- Validation requires `run-next --validate-only` or `run-batch --validate-only`.
- A validated train is deployed as one exact identity by `run-batch --deploy`.
- Validated-gate reuse is disabled unless config or `--reuse-validated` explicitly authorizes it.
- `supersede` atomically retires a validated train and enqueues exact replacement SHAs; validation, reuse identity, and deploy approval never carry over.
- `events`, `inspect`, and `logs` are read-only observation commands; event JSONL resumes by ID.
- The daemon processes only jobs enqueued with `--auto`.
- The hub dashboard is a read-only aggregate; every repo keeps its own queue, lock, and recovery state.
- The hub daemon also processes only `--auto` jobs, across registered repos, through each repo's own runner and lock; `--concurrency` caps simultaneous repos machine-wide.
- Destructive cleanup requires `gc --apply`; branch deletion also requires `--delete-branches`.
- After a crash, `reconcile`/`recover` resolve `needs_reconcile` jobs against the remote; `run-batch --deploy` is refused while any job is `needs_reconcile`. `unlock --force` clears a wedged lock (remote-reachable first).

### Stable machine contract

- Human output says `deploy`, `deploying`, and `deployed`.
- JSON/SQLite continue to use `status=deployed`, `deploy_sha`, `push_status`, and `verify_status`.
- This operation is an atomic Git ref push. Configured `deploy.verify` hooks report an independent post-push outcome; a provider release is separate and requires its own authorization.
<!-- END GENERATED: mergetrain-agent-protocol -->

Read [the next-action and error reference](reference.md) before taking a queue
action.

## MCP workflow

1. Use the plugin's MCP tools instead of shell commands when the needed tool is
   exposed.
2. Start with `mergetrain_doctor` and `mergetrain_status`. Treat
   `next_action` as guidance, not authorization.
3. Read `health`, `result`, and `error.code`; `ok` only says the command
   produced a contract response.
4. Enqueue only a committed, clean task branch. Validation may run without
   deploy approval.
5. Observe a running job with bounded events, inspection, or logs. Keep raw
   logs out of summaries unless the user needs their contents.
6. Never infer approval for deploy, unattended `--auto`, validated-gate reuse,
   recovery mutations, force unlock, or destructive cleanup.
7. For deploy, ask the user to invoke `/mergetrain:deploy`; do not invoke the
   deploy tool automatically.

The plugin requires the `mergetrain` executable with its MCP extra installed:
`uv tool install 'mergetrain[mcp]'`.
