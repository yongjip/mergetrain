# CLAUDE.md — operating mergetrain from Dispatch

This repository ships **mergetrain**, a local deploy train for coding-agent worktrees (see [README.md](./README.md) and [docs/](./docs/)). This file tells you, the agent, how to **operate the mergetrain queue** when a task is dispatched from the phone (Cowork Dispatch). Optimize for short, reliable, phone-readable results. Setup and the phone phrasebook live in [docs/mobile.md](./docs/mobile.md).

## Ground rules

- mergetrain runs locally on this machine's git repo. Run commands from the repo root that contains `.mergetrain.yaml`, or pass `--repo <path>` to operate another service repo.
- **Always read state first:** run `mergetrain doctor --json` and `mergetrain status --json` before acting, and decide from that JSON — never from assumptions.
- Every command is non-interactive and JSON-first. Prefer `--json`, then summarize. Don't paste raw JSON unless asked.

## You may do these without asking

- `mergetrain status --json` and `mergetrain doctor --json` — inspect the queue, lock, and `next_action`.
- `mergetrain gc --json` — dry-run cleanup preview (does **not** delete anything).
- `mergetrain run-batch --validate-only` — validate the queued train; this never pushes.
- `mergetrain enqueue --task "<t>" --branch <b> --capture-sha` — only for a branch that is already committed and on a clean worktree.

## Deploy policy — confirm, then deploy

A deploy ships code. **Never deploy as a side effect of another request.** Before any deploy:

1. Run `doctor --json` and `status --json`.
2. Post a short summary of exactly what will ship: the queued job IDs and branches, the integration ref, the doctor `next_action`, and anything `blocked`/`failed`.
3. **Wait for the user's explicit confirmation in the thread** (e.g. "deploy", "yes ship it", "go"). A vague or general instruction is not confirmation.
4. Only then run `mergetrain run-batch --deploy` (or `scripts/ty-deploy.sh --confirm`).
5. Report the outcome: which jobs are now `deployed`, the `deploy_sha`, and any post-push verify warning recorded in the note.

## Do NOT do these unless explicitly told

- `mergetrain enqueue ... --auto` or `mergetrain daemon` — these bypass the confirm-then-deploy step (unattended deploy).
- Destructive cleanup: `mergetrain gc --apply`, `gc --delete-branches`, or `mergetrain cancel <id>`.

## Blocked / failed jobs

- Summarize the cause from the job `note` (read the `log_path` only if you need detail).
- Recommend the fix — rebase the branch on the integration branch, commit a clean result, enqueue a **new** job — but don't perform git surgery unless asked.

## Convenience scripts

- `scripts/ty-status.sh` — one-glance status + doctor summary.
- `scripts/ty-validate.sh` — validate the queued train (no push).
- `scripts/ty-deploy.sh` — guarded deploy; prints what will ship and only deploys with `--confirm`.

## Reporting style (phone)

Lead with the answer, keep it to a few lines. Example:

> Queue: 3 queued (agent/a, agent/b, agent/c). No runner active. doctor next_action = run_batch_validate. Want me to validate?
