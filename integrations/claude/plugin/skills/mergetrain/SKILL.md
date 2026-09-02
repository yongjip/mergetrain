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
3. Do not push configured Git refs directly. For ordinary handoff, run mergetrain enqueue with the task, branch, and optional worktree only; do not copy --base-sha, --head-sha, or --capture-sha because the CLI captures and validates the exact commits by default. Stop after the successful enqueue unless separately authorized as the runner.
4. Read doctor --json first. Use status --json --limit 10 only when job or train details are needed, and read attention_jobs before recent history.
5. Use --auto only after explicit unattended-deployment approval from the user/operator. A bounded instruction to QA, deploy, verify, and finish end-to-end is unattended-deployment approval for that task scope; continue without repeated train-ID prompts unless the scope, destination, execution policy, or recovery authority changes.
6. An auto job is bound to the approved Git destination and execution policy. If the remote, push refs, gates, validation-reuse policy, command timeout, or verify hooks change, mergetrain blocks before claim, gates, or push; review the change before enqueueing with --auto again.
7. Reuse validated gates only after explicit deploy.reuse configuration or --reuse-validated authorization.
8. Let one separately authorized runner or daemon own merge, test, push, and verify; ordinary task, merge, integration, or enqueue intent is not deploy approval unless the user explicitly authorizes bounded end-to-end deployment.
9. Fix blocked or failed work in the owning branch and commit a clean result, then run mergetrain retry <id> to dismiss the old outcome and enqueue a fresh SHA-pinned job.
10. Replace a validated train only with mergetrain supersede; the replacement is a new SHA-pinned train that requires fresh validation. One-shot train approval does not carry over; bounded unattended-deployment approval carries only while task scope, destination, and execution policy remain unchanged.
11. After a crash, run reconcile/recover to resolve needs_reconcile jobs against the remote before deploying; run reconcile before any manual force-push.
12. Do not delete or rewrite refs/mergetrain/deploys/*; they are permanent remote recovery evidence.

### Safety boundary

- Git deployment requires either explicit approval after a human-readable exact-train summary or prior explicit bounded unattended-deployment approval. An opaque train ID binds the operation internally; never make the user repeat it.
- A task agent uses ordinary `enqueue` without manually copied SHA options and stops after it succeeds. Only a separately authorized runner validates with `run-next --validate-only`, `run-batch --validate-only`, or `daemon --validate-only`.
- A validated train is deployed as one exact identity by `run-batch --deploy`; summarize changes, destination refs, gates, blocked or failed work, and reassembly risk in human terms.
- Validated-gate reuse is disabled unless config or `--reuse-validated` explicitly authorizes it.
- `supersede` atomically retires a validated train and enqueues exact replacement SHAs; validation, reuse identity, and one-shot train approval never carry over. Bounded unattended authorization continues only while task scope, destination, and execution policy remain unchanged.
- `events`, `inspect`, and `logs` are read-only observation commands; event JSONL resumes by ID.
- The default daemon deploys only jobs enqueued with `--auto`. `daemon --validate-only` processes only manual queued jobs, never pushes, and pauses while any validated train exists.
- Auto approval is bound to the Git remote, integration ref, push refs, gates, command timeout, validation-reuse policy, and verify hooks recorded at enqueue. A destination or execution-policy change blocks before claim, gates, or push and requires renewed approval.
- The hub dashboard is a read-only aggregate; every repo keeps its own queue, lock, and recovery state.
- The hub daemon also processes only `--auto` jobs, across registered repos, through each repo's own runner and lock; `--concurrency` caps simultaneous repos machine-wide.
- Destructive cleanup requires `gc --apply`; branch deletion also requires `--delete-branches`.
- After a crash, `reconcile`/`recover` resolve `needs_reconcile` jobs against the remote; `run-batch --deploy` is refused while any job is `needs_reconcile`. `unlock --force` clears a wedged lock (remote-reachable first).

### Stable machine contract

- Human output and JSON/SQLite use `deploy`, `deploying`, `status=deployed`, `deploy_sha`, `push_status`, and `verify_status`.
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
   recovery mutations, force unlock, or destructive cleanup. An explicit
   bounded request to QA, deploy, verify, and finish is unattended approval for
   that unchanged task scope and destination.
7. The MCP deploy tool always uses its client-rendered human confirmation. For
   one-shot MCP deployment ask the user to invoke `/mergetrain:deploy`; do not
   ask them to copy an opaque train ID.

The plugin requires the `mergetrain` executable with its MCP extra installed:
`uv tool install 'mergetrain[mcp]'`.
