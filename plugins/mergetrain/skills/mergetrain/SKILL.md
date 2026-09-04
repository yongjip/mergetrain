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

# Integrate parallel coding-agent branches

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

Use the plugin's MCP tools when they are available. Start with
`mergetrain_status`; its next action is guidance, not deployment authority.
Routine task agents call `mergetrain_enqueue` only for a committed, clean branch
and stop after the successful handoff.

Do not infer approval for validation, deploy, unattended operation, recovery,
force unlock, or destructive cleanup. If the MCP client cannot render the
deploy confirmation, report the returned terminal command and stop. Never ask a
person to copy an opaque train ID or plan hash.

The plugin launches the pinned released MCP package with `uvx`. The first use
may download that package; if `uvx` is unavailable, report that the `uv` runtime
must be installed rather than falling back to direct Git integration.
