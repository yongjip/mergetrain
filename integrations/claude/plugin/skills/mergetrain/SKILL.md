---
name: mergetrain
description: Operate a local mergetrain queue for coding-agent branches. Use when inspecting queue health, enqueueing committed work, validating a train, following progress, or recovering from blocked and crashed runs.
---

# Operate mergetrain

<!-- BEGIN GENERATED: mergetrain-agent-protocol -->
Purpose: Serialize committed local task branches through one merge/test/push/verify runner.

### Rules

1. Work on a task-specific branch and worktree.
2. Commit a clean HEAD before handing work off.
3. Read mergetrain status --json and follow its next action before changing queue state.
4. Enqueue with only the task, branch, and optional worktree; mergetrain captures the exact commits. Stop after a successful enqueue unless the user explicitly authorized end-to-end validation and deployment.
5. Never push configured integration refs directly. One authorized runner owns validation and deployment; recovery and destructive actions require their stated approval.

### Safety boundary

- A task agent stops after ordinary `enqueue`. Only a separately authorized runner uses `validate`, `deploy`, or a daemon.
- Deployment requires either confirmation of the human-readable exact plan or prior bounded unattended approval. Train IDs and hashes stay internal.
- Unattended approval is bound to the exact destination and execution policy. Any change blocks before push.
- Recovery and destructive cleanup require their stated approval. Follow `status.next_action`; never rewrite permanent deploy audit refs.

### Stable machine contract

- Every JSON payload carries `contract_version`; ignore unknown keys and fail closed on unknown safety actions.
- `deploy` means the atomic Git ref update plus configured verification. A downstream provider release is separate.
<!-- END GENERATED: mergetrain-agent-protocol -->

Read [the next-action and error reference](reference.md) before taking a queue
action.

## MCP workflow

1. Use the plugin's MCP tools instead of shell commands when the needed tool is
   exposed.
2. Start with `mergetrain_status`. Treat `next_action` as guidance, not
   authorization.
3. Read `health`, `result`, and `error.code`; `ok` only says the command
   produced a contract response.
4. Enqueue only a committed, clean task branch. Validation may run without
   deploy approval.
5. Observe a running job with `mergetrain_inspect`; request bounded events or
   logs through its `detail` input only when needed.
6. Never infer approval for deploy, unattended `--auto`, validated-gate reuse,
   recovery mutations, force unlock, or destructive cleanup. An explicit
   bounded request to QA, deploy, verify, and finish is unattended approval for
   that unchanged task scope and destination.
7. The MCP deploy tool always uses its client-rendered human confirmation. For
   one-shot MCP deployment ask the user to invoke `/mergetrain:deploy`; do not
   ask them to copy an opaque train ID.

The plugin requires the `mergetrain` executable with its MCP extra installed:
`uv tool install 'mergetrain[mcp]'`.
