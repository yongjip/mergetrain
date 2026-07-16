# Config reference

Default config file name:

```text
.mergetrain.yaml
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
  db: .mergetrain/queue.sqlite
  logs: .mergetrain/logs
  worktree_root: .mergetrain/worktrees
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

If `push_refs` is omitted it defaults to `integration_branch`. An explicitly
empty list, null value, blank ref, or duplicate ref is a configuration error;
deploy targets never fail open to `main`.

## `queue`

```yaml
queue:
  lock_ttl_minutes: 30
  daemon_interval_seconds: 15
  heartbeat_interval_seconds: 10
  command_timeout_seconds: 3600
```

`lock_ttl_minutes` controls runner lock expiry. Managed Git, gate, and verify
commands renew the lease every `heartbeat_interval_seconds`; the heartbeat must
be shorter than the TTL. `command_timeout_seconds` terminates a command and
marks the affected job failed. All queue timing values must be positive.

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
updated; mergetrain marks jobs as `deployed` and records a warning note.

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
MERGETRAIN_PROJECT
MERGETRAIN_INTEGRATION_REF
MERGETRAIN_REPO
MERGETRAIN_WORKTREE
```

Commands are executed through `/bin/sh`; treat config files as trusted code.
