# CLAUDE.md — operating mergetrain from Dispatch

This repository ships **mergetrain**, a local deploy train for coding-agent worktrees (see [README.md](./README.md) and [docs/](./docs/)). This file tells you, the agent, how to **operate the mergetrain queue** when a task is dispatched from the phone (Cowork Dispatch). Optimize for short, reliable, phone-readable results. Setup and the phone phrasebook live in [docs/mobile.md](./docs/mobile.md).

## Shared operating protocol

<!-- BEGIN GENERATED: mergetrain-agent-protocol -->
Purpose: Serialize committed local task branches through one merge/test/push/verify runner.

### Rules

1. Work on a task-specific branch and worktree.
2. Commit all changes before enqueueing.
3. Do not push configured Git refs directly. Task agents hand off by enqueueing the exact committed HEAD, then stop unless separately authorized as the runner.
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
- A task agent stops after enqueueing. Only a separately authorized runner validates with `run-next --validate-only`, `run-batch --validate-only`, or `daemon --validate-only`.
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

- `mergetrain doctor --json` first, then `mergetrain status --json --limit 10` only when job or train details are needed; read `attention_jobs` before recent history.
- `mergetrain gc --json` — dry-run cleanup preview (does **not** delete anything).
- `mergetrain run-batch --validate-only` — validate the queued train; this never pushes.
- `mergetrain enqueue --task "<t>" --branch <b>` — exact SHAs are captured by default; use only a committed branch on a clean worktree.

## Deploy policy — scoped approval, then finish

A deploy ships code. Never infer deploy permission from an ordinary request to
implement, merge, integrate, land, or enqueue. There are two valid approval
modes:

1. A bounded instruction to QA, deploy, verify, and finish end-to-end grants
   unattended-deploy approval for that named task and destination. Enqueue with
   `--auto` when appropriate, let one runner complete validation, deploy, and
   verification, and do not stop for intermediate train-ID confirmations.
2. Without bounded unattended approval, run `doctor --json` and `status --json
   --limit 10`, then give one human-readable deploy packet: task intent and
   changes, branches and recorded HEADs, destination refs, gate evidence,
   blocked or failed work, and any reassembly risk. Wait for a simple explicit
   "deploy / yes / go"; keep the opaque `train_id` internal rather than asking
   the user to copy it.

In either mode, use the exact selected train internally and report deployed job
IDs, `deploy_sha`, push status, and post-push verification. Stop for new authority
only when task scope or destination changes, a product/business decision is
needed, or recovery crosses a destructive or reconcile boundary.

## Do NOT do these unless explicitly told

- `mergetrain enqueue ... --auto` or `mergetrain daemon` without bounded
  unattended-deploy approval.
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
