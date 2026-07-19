# Quickstart

## 1. Initialize config

From your Git repository root:

```sh
mergetrain init --project example-app --write
```

Edit `.mergetrain.yaml` so `git.remote`, `git.integration_branch`, `git.push_refs`,
`gates`, and `deploy.verify` match your service.

## 2. Create a task branch

```sh
git switch -c codex/feature-a
# edit files
git add .
git commit -m "feature a"
```

## 3. Enqueue

```sh
mergetrain doctor --json
mergetrain enqueue --task "feature a" --branch codex/feature-a --capture-sha
mergetrain status --json
```

## 4. Validate

```sh
mergetrain run-batch --validate-only
```

A successful validation marks merged jobs as `validated`, records a shared
`train_id` plus the integration and task SHAs, and does not push. Inspect
`status --json` before approval to see the exact deployable train.

## 5. Observe a long run

Use the job ID returned by `enqueue` to inspect or follow without parsing OS
processes:

```sh
mergetrain inspect <job-id> --json
mergetrain events --job <job-id> --after 0 --follow --jsonl
mergetrain logs <job-id> --follow --tail 20
```

Reconnect `events` with the last event ID already processed. Structured events
and heartbeat frames never contain command output; request `logs` only when raw
local output is appropriate for the destination.

## 6. Deploy

After explicit approval:

```sh
mergetrain run-batch --deploy
```

Deploy mode runs gates first, then performs an atomic push to configured
`git.push_refs`, then runs `deploy.verify` hooks. If a validated train is
pending, only that exact train is rebuilt and deployed; newly queued jobs wait
for a later validation. Integration-ref movement is allowed because the train
is rebuilt and gated again, but a changed task branch is blocked and must be
enqueued fresh.

For an explicitly configured validated-gate reuse policy, preview the decision
before deploying:

```sh
mergetrain run-batch --deploy --train-id <id> --reuse-validated --preview --json
mergetrain run-batch --deploy --train-id <id> --reuse-validated
```

Only an unchanged safety identity reuses the exact validation SHA. Otherwise the
normal full gate path runs (or the policy fails closed), and post-push verify
hooks still run.

## 7. Auto-only daemon

Use `--auto` only when unattended deploy is explicitly approved:

```sh
mergetrain enqueue --task "safe fix" --branch codex/safe-fix --capture-sha --auto
mergetrain daemon --interval 15
```

The daemon ignores manual queued jobs.
