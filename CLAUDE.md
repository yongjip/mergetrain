# CLAUDE.md — operating mergetrain from Dispatch

This repository ships **mergetrain**, a local deploy train for coding-agent worktrees (see [README.md](./README.md) and [docs/](./docs/)). This file tells you, the agent, how to **operate the mergetrain queue** when a task is dispatched from the phone (Cowork Dispatch). Optimize for short, reliable, phone-readable results. Setup and the phone phrasebook live in [docs/mobile.md](./docs/mobile.md).

## Shared operating protocol

<!-- BEGIN GENERATED: mergetrain-agent-protocol -->
Purpose: Serialize committed local task branches through one merge/test/push/verify runner.

### Rules

1. Work on a task-specific branch and worktree.
2. Commit all changes before enqueueing.
3. Do not push configured Git refs directly. Task agents hand off by enqueueing the exact committed HEAD, then stop unless separately authorized as the runner.
4. Read doctor --json or status --json before deciding the next action.
5. Use --auto only after explicit unattended-deployment approval from the user/operator.
6. Reuse validated gates only after explicit deploy.reuse configuration or --reuse-validated authorization.
7. Let one separately authorized runner or daemon own merge, test, push, and verify; a task, merge, integration, or enqueue request is not deploy approval.
8. Fix blocked or failed work in the owning branch and commit a clean result, then run mergetrain retry <id> to dismiss the old outcome and enqueue a fresh SHA-pinned job.
9. Replace a validated train only with mergetrain supersede; the replacement is a new SHA-pinned train that requires fresh validation and deploy approval.
10. After a crash, run reconcile/recover to resolve needs_reconcile jobs against the remote before deploying; run reconcile before any manual force-push.
11. Do not delete or rewrite refs/mergetrain/deploys/*; they are permanent remote recovery evidence.

### Safety boundary

- Git deployment requires separate explicit user/operator approval for the displayed exact validated train, then `run-batch --deploy`; `run-next --deploy` is allowed only when no validated train is pending.
- A task agent stops after enqueueing. Only a separately authorized runner validates with `run-next --validate-only` or `run-batch --validate-only`.
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

- Human output and JSON/SQLite use `deploy`, `deploying`, `status=deployed`, `deploy_sha`, `push_status`, and `verify_status`.
- This operation is an atomic Git ref push. Configured `deploy.verify` hooks report an independent post-push outcome; a provider release is separate and requires its own authorization.
<!-- END GENERATED: mergetrain-agent-protocol -->

Repository-specific additions:

- **English only for everything that lands in the repo or the tracker**:
  issues, PRs, commit messages, comments, docs, release notes, and labels.
- Run commands from the repo root containing `.mergetrain.yaml`, or pass
  `--repo <path>`.
- Prefer machine-readable output and summarize it. `events` uses JSONL; `logs`
  is raw text and may contain sensitive command output.

## GitHub CLI authentication

- The Codex sandbox may not be able to read `gh` credentials stored in the
  macOS Keychain. Do not interpret a sandboxed `gh auth status` or `gh auth
  token` failure as proof that the user must log in again.
- Before requesting `gh auth login`, run the credential and API checks outside
  the sandbox: `gh auth token -h github.com >/dev/null` and `gh api user --jq
  .login`. Never display or log the token.
- Reuse the existing login when those external checks pass. Ask for a new login
  only when they also fail outside the sandbox.
- The Codex GitHub connector is authenticated separately from local `gh`.
  Prefer the connector when it supports the operation, and fall back to an
  externally run `gh` command if the connector lacks repository permission.

## You may do these without asking

- `mergetrain status --json` and `mergetrain doctor --json` — inspect the queue, lock, and `next_action`.
- `mergetrain gc --json` — dry-run cleanup preview (does **not** delete anything).
- `mergetrain run-batch --validate-only` — validate the queued train; this never pushes.
- `mergetrain enqueue --task "<t>" --branch <b> --capture-sha` — only for a branch that is already committed and on a clean worktree.

## Deploy policy — confirm, then deploy

A deploy ships code. **Never deploy as a side effect of another request.** Before any deploy:

1. Run `doctor --json` and `status --json`.
2. Post a short summary of exactly what will ship: the pending validated `train_id`, its job IDs, branches, recorded HEADs, the integration ref, the doctor `next_action`, and anything `blocked`/`failed`. If no validated train exists, summarize the queued jobs that a direct deploy would claim.
3. **Wait for the user's explicit confirmation in the thread** (e.g. "deploy", "yes ship it", "go"). A vague or general instruction is not confirmation.
4. Only then run `mergetrain run-batch --deploy` (or `scripts/mt-deploy.sh --confirm`). If multiple validated trains are pending, select the approved one with `--train-id`.
5. Report the outcome: which jobs are now `deployed`, the `deploy_sha`, and any post-push verify warning recorded in the note.

## Do NOT do these unless explicitly told

- `mergetrain enqueue ... --auto` or `mergetrain daemon` — these bypass the confirm-then-deploy step (unattended deploy).
- Destructive cleanup: `mergetrain gc --apply`, `gc --delete-branches`, or `mergetrain cancel <id>`.

## Blocked / failed jobs

- Summarize the cause from the job `note` (read the `log_path` only if you need detail).
- Recommend the fix — rebase the branch on the integration branch, commit a clean result, enqueue a **new** job — but don't perform git surgery unless asked.

## Convenience scripts

- `scripts/mt-status.sh` — one-glance status + doctor summary.
- `scripts/mt-validate.sh` — validate the queued train (no push).
- `scripts/mt-deploy.sh` — guarded deploy; prints what will ship and only deploys with `--confirm`.

## Reporting style (phone)

Lead with the answer, keep it to a few lines. Example:

> Queue: 3 queued (agent/a, agent/b, agent/c). No runner active. doctor next_action = run_batch_validate. Want me to validate?
