# CLAUDE.md — operating mergetrain from Dispatch

This repository ships mergetrain, a local deploy train for coding-agent
worktrees. Keep phone-facing results short and actionable. Setup and phrasebook
details live in [docs/mobile.md](./docs/mobile.md).

## Shared operating protocol

<!-- BEGIN GENERATED: mergetrain-agent-protocol -->
Purpose: Serialize committed local task branches through one merge/test/push/verify runner.

### Existing queues and explanations

- Existing mergetrain repositories keep status → enqueue → stop even for one branch. Queue counts alone do not establish health, runner ownership, or recovery needs; read `health`, `state`, and `next_action` together.
- For explanation-only requests, read the skill documentation when permitted and explain the procedure without Git or product commands. Distinguish hypothetical steps from observed state.

### Current command reference

- The v3 core commands are `init`, `status`, `enqueue`, `validate`, `deploy`, and `inspect`.
- Start with `mergetrain status --json`. Use `mergetrain status --diagnose --json` only for configuration, Git, runtime, or lock detail, and `mergetrain inspect JOB_ID --json` for job evidence. `doctor` is removed, not an alias for `status --diagnose`.
- Confirm uncertain syntax with the installed `mergetrain --version` and command-specific `--help` only when command execution is permitted. Otherwise use this reference and identify missing details; do not invent commands or copy older syntax from unversioned web results. Inspection and `next_action` do not authorize recovery or deployment.

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

## Repository-specific rules

- Use English for committed code, docs, issues, PRs, comments, release notes,
  labels, and commit messages.
- Run from the repository root or pass `--repo <path>`.
- Start with `mergetrain status --json`; use `status --diagnose` only for
  configuration, Git, runtime, or lock detail.
- Prefer structured output. Raw logs may contain sensitive command output.
- Keep the six-verb public grammar and product-scope ceiling intact.

## GitHub CLI authentication

The sandbox may not read credentials stored in the macOS Keychain. Before
requesting a new login, verify `gh auth token -h github.com >/dev/null` and
`gh api user --jq .login` outside the sandbox. Never print a token. The Codex
GitHub connector and local `gh` use separate credentials.

## Normal actions

These do not ship code:

```sh
mergetrain status --json
mergetrain inspect <job-id> --json
mergetrain validate
mergetrain enqueue --task "<task>" --branch <branch>
mergetrain gc --json
```

A task agent enqueues every named finished branch in the requested order, then
stops. Asking to queue for validation does not authorize running validation.

## Deploy policy

A deploy ships code. Do not infer permission from an ordinary request to edit,
merge, integrate, land, or enqueue.

With explicit bounded authority to QA, deploy, verify, and finish the named
task, enqueue with hidden `--auto` when unattended execution is appropriate and
let one runner finish without opaque train-ID prompts. Without that authority,
run `mergetrain deploy`, show its human-readable exact plan, and wait for the
interactive confirmation.

Stop for new authority when task scope or destination changes, a product
decision is needed, or recovery crosses a destructive/reconcile boundary.

## Exceptional state

Do not memorize repair commands. Read `status.next_action` and follow its exact
command. Never edit queue SQLite directly or delete/rewrite remote
`refs/mergetrain/deploys/*`.

Destructive cleanup (`gc --apply`, branch deletion), cancellation, forced
unlock, and reconciliation require the authority stated by status or the user.

## Convenience scripts

- `scripts/mt-status.sh` delegates to `mergetrain status`.
- `scripts/mt-validate.sh` delegates to `mergetrain validate`.
- `scripts/mt-deploy.sh` delegates to the interactive `mergetrain deploy`.

## Reporting style

Lead with state and the next decision. Example:

> READY: 3 jobs passed validation. Next: `mergetrain deploy` (deployment approval required).
