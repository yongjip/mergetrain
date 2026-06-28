# Config reference

Default config file name:

```text
.trainyard.yaml
```

## `project`

```yaml
project:
  name: example-app
```

`project.name` is used in JSON output and temporary worktree names.

## `state`

```yaml
state:
  db: .trainyard/queue.sqlite
  logs: .trainyard/logs
  worktree_root: .trainyard/worktrees
```

Relative paths are resolved from the repository root.

## `git`

```yaml
git:
  remote: origin
  integration_branch: main
  push_refs:
    - main
```

`integration_ref` is derived as:

```text
{remote}/{integration_branch}
```

Deploy mode pushes the verified HEAD atomically:

```sh
git push --atomic origin HEAD:main
```

Multiple refs are allowed:

```yaml
git:
  remote: platform
  integration_branch: develop
  push_refs:
    - develop
    - main
```

This produces:

```sh
git push --atomic platform HEAD:develop HEAD:main
```

## `queue`

```yaml
queue:
  lock_ttl_minutes: 30
  daemon_interval_seconds: 15
```

`lock_ttl_minutes` controls runner lock expiry. A live PID owner is not stolen
even after TTL expiry.

## `agent`

```yaml
agent:
  require_clean_worktree_before_enqueue: true
  require_explicit_auto_approval: true
  prefer_json_status: true
```

These fields document expected agent behavior. CLI readiness checks are enforced
by enqueue options.

## `gates`

```yaml
gates:
  - name: diff-check
    run: git diff --check ${integration_ref}..HEAD
  - name: tests
    run: python -m unittest discover -s tests
```

Gates run before push in the temporary integration worktree.

## `deploy.verify`

```yaml
deploy:
  verify:
    - name: live-health
      run: curl -fsS https://example.invalid/health
```

Verify hooks run after push. A verify failure may mean the remote ref was already
updated; trainyard marks jobs as `deployed` and records a warning note.

## Placeholders and environment

Placeholders available in `gates` and `deploy.verify`:

```text
${integration_ref}
${project}
${repo}
${worktree}
```

Equivalent environment variables:

```text
TRAINYARD_PROJECT
TRAINYARD_INTEGRATION_REF
TRAINYARD_REPO
TRAINYARD_WORKTREE
```

Commands are executed through `/bin/sh`; treat config files as trusted code.
