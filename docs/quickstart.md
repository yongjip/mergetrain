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

A successful validation marks merged jobs as `validated` and does not push.

## 5. Deploy

After explicit approval:

```sh
mergetrain run-batch --deploy
```

Deploy mode runs gates first, then performs an atomic push to configured
`git.push_refs`, then runs `deploy.verify` hooks.

## 6. Auto-only daemon

Use `--auto` only when unattended deploy is explicitly approved:

```sh
mergetrain enqueue --task "safe fix" --branch codex/safe-fix --capture-sha --auto
mergetrain daemon --interval 15
```

The daemon ignores manual queued jobs.
