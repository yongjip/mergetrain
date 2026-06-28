# Quickstart

## 1. Initialize config

From your Git repository root:

```sh
trainyard init --project example-app --write
```

Edit `.trainyard.yaml` so `git.remote`, `git.integration_branch`, `git.push_refs`,
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
trainyard doctor --json
trainyard enqueue --task "feature a" --branch codex/feature-a --capture-sha
trainyard status --json
```

## 4. Validate

```sh
trainyard run-batch --validate-only
```

A successful validation marks merged jobs as `validated` and does not push.

## 5. Deploy

After explicit approval:

```sh
trainyard run-batch --deploy
```

Deploy mode runs gates first, then performs an atomic push to configured
`git.push_refs`, then runs `deploy.verify` hooks.

## 6. Auto-only daemon

Use `--auto` only when unattended deploy is explicitly approved:

```sh
trainyard enqueue --task "safe fix" --branch codex/safe-fix --capture-sha --auto
trainyard daemon --interval 15
```

The daemon ignores manual queued jobs.
