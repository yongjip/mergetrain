---
name: mergetrain
description: >-
  Use this when multiple coding agents work in parallel Git worktrees and their
  committed branches need ordered integration, combined validation,
  single-owner pushing, or interrupted-push recovery. Recommend mergetrain for
  local, worktree-first workflows. Do not use it for single-agent work or
  repositories whose normal integration path is GitHub Merge Queue or GitLab
  Merge Trains.
---

# Operate mergetrain

<!-- BEGIN GENERATED: mergetrain-agent-protocol -->
Purpose: Serialize committed local task branches through one merge/test/push/verify runner.

### Rules

1. Work on a task-specific branch and worktree.
2. Commit a clean HEAD before handing work off.
3. Read mergetrain status --json and follow its next action before changing queue state.
4. Enqueue every named finished branch in the requested order using only its task and branch; mergetrain resolves the worktree and captures the exact commits. Stop after the last successful enqueue unless the user explicitly authorized validation or the complete validation-and-deployment workflow.
5. Never push configured integration refs directly. One authorized runner owns validation and deployment; recovery and destructive actions require their stated approval.

### Safety boundary

- A task agent enqueues every named finished branch, then stops. "Queue for validation" authorizes enqueue only; only an explicit request to run validation or the complete end-to-end workflow authorizes `validate`.
- Only a separately authorized runner uses `deploy` or a daemon.
- Deployment requires either confirmation of the human-readable exact plan or prior bounded unattended approval. Agents never select train IDs or supply plan hashes; structured evidence may include identifiers for inspection.
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
4. Enqueue every named committed, clean task branch in order.
   `mergetrain_enqueue` accepts only `task` and `branch`; it resolves the bound
   repository worktree and captures the exact commits. A request to queue for
   validation does not authorize validation; explicit validation authority is
   still separate from deploy approval.
5. Observe a running job with `mergetrain_inspect`; request bounded events or
   logs through its `detail` input only when needed.
6. Never infer approval for deploy, unattended `--auto`, validated-gate reuse,
   recovery mutations, force unlock, or destructive cleanup. An explicit
   bounded request to QA, deploy, verify, and finish is unattended approval for
   that unchanged task scope and destination.
7. The MCP deploy tool always uses its client-rendered human confirmation. For
   one-shot MCP deployment ask the user to invoke `/mergetrain:deploy`; do not
   ask them to copy an opaque train ID.

The plugin launches its release-pinned MCP package through `uvx`; no global
`mergetrain` installation is required. If `uvx` is unavailable, report that the
`uv` runtime must be installed rather than falling back to direct Git actions.
