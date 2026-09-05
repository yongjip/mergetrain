---
name: mergetrain
description: Use for tasks about an existing mergetrain queue, or choosing local integration for parallel coding-agent branches that need ordered integration, combined testing, or a single integration push owner. Includes recovery of that integration workflow. Ordinary Git repair and administration of hosted PR queues are outside this scope.
---

# Integrate parallel coding-agent branches

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

Use the plugin's MCP tools when they are available. Start with
`mergetrain_status`; its next action is guidance, not deployment authority.
Routine task agents call `mergetrain_enqueue` for every named committed, clean
branch in order and stop after the last successful handoff.

Do not infer approval for validation, deploy, unattended operation, recovery,
force unlock, or destructive cleanup. If the MCP client cannot render the
deploy confirmation, report the returned terminal command and stop. Never ask a
person to copy an opaque train ID or plan hash.

The plugin launches the pinned released MCP package with `uvx`. The first use
may download that package; if `uvx` is unavailable, report that the `uv` runtime
must be installed rather than falling back to direct Git integration.
