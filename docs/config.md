# Config reference

Default config file name:

```text
.mergetrain.yaml
```

## Parser support

PyYAML is optional. Without it, mergetrain uses a dependency-free YAML subset
parser for the block-style shape emitted by `mergetrain init`. Empty `[]` and
`{}` values are supported, but non-empty flow-style collections such as
`[main, release]` or `{name: tests}` require PyYAML; the fallback rejects them
with `config_error` instead of guessing. Invalid mapping, list, string,
boolean, path, and positive-integer values also fail closed during config
validation. See [installation](install.md#optional-yaml-dependency) to enable
full YAML parsing.

## `version`

```yaml
version: 1
```

The config schema version. `mergetrain init` writes the current version (`1`);
an omitted `version:` is treated as `1`. A file whose `version:` is **newer**
than this binary understands is recorded — not rejected — at load, so recovery
still works on an older binary. Command-scoped enforcement surfaces the mismatch
instead: `doctor` reports `config_version_supported` (the highest version this
binary understands) and a `next_action` of `upgrade_mergetrain`. Upgrade
mergetrain before deploying.

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
  validation_workspace:
    mode: ephemeral
    cache_key: ""
    cache_paths: []
```

Relative paths are resolved from the repository root.

Validation worktrees are disposable by default. Projects with path-sensitive
generated caches may opt into one runner-owned stable validation path:

```yaml
state:
  validation_workspace:
    mode: persistent
    cache_key: unity-library-v1
    cache_paths:
      - unity/Teratorn/Library
```

Persistent mode derives
`{worktree_root}/{project.name}-validation-workspace`; it does not accept an
arbitrary path. Only validate runs use that path. Deploy reassembly and bisect
probes remain in isolated, disposable worktrees, and the existing runner lock
serializes all access.

Each `cache_paths` entry must name a normalized, repository-relative directory
that is ignored by Git and contains no tracked files. Globs, absolute paths,
backslashes, `.git`, and `.`/`..` segments are rejected. Before every validation,
mergetrain hard-resets tracked inputs to the fetched integration ref and removes
all ignored and untracked content except those declared directories. A symlink,
foreign worktree, tracked cache path, non-ignored cache path, or workspace that
cannot be restored cleanly blocks validation.

The cache is retained only while `cache_key`, the gate/fingerprint policy, and
the configured environment fingerprint outputs match its marker. Change
`cache_key` whenever the build cache schema or an un-fingerprinted toolchain
changes. Marker corruption or an identity mismatch clears only the declared
cache directories before gates run. Gate tree-integrity checks, SHA-pinned train
identity, and push behavior are unchanged; generated cache files are never added
or pushed by mergetrain.

`doctor --json` reports the mode, derived path, existence, initialization state,
key, and declared cache paths. `gc` protects the workspace while persistent mode
is configured. To remove it, switch back to `mode: ephemeral`, inspect
`gc --json`, then explicitly run `gc --apply`.

Keep ephemeral mode unless measurements show that a project-local cold build is
the dominant gate cost. The [efficient-operation guide](best-practices.md)
explains the cache decision, rollout measurements, and project-specific
tradeoffs.

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

## `terminology`

```yaml
terminology:
  git_operation: integrate
```

`git_operation` controls human-facing vocabulary for the atomic Git push. Its
allowed values are `deploy` (default), `integrate`, and `push`. For example,
`integrate` makes the CLI, dashboard, guarded wrapper, runner events, and
generated agent contract say `integrate` / `integrating` / `integrated`.
`push` similarly selects `push` / `pushing` / `pushed`. The `--integrate` and
`--push` aliases are always accepted; this setting selects the preferred words.

This setting does not rename machine contracts. Existing `--deploy` commands,
`status=deployed`, `deploy_sha`, SQLite databases, `deploy.*` config keys, and
JSON `next_action` values remain stable. The configured word names the atomic
Git ref update. `deploy.verify` records an independent post-push outcome, while
a provider release is a separate action that Git completion does not imply.

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

These fields document expected agent behavior only — they are parsed but have no
runtime effect, so toggling them (e.g. `require_explicit_auto_approval: false`)
changes nothing. Actual readiness gating is enforced by `enqueue` options
(`--allow-dirty`, `--allow-branch-mismatch`, `--no-ready-check`, `--auto`), not by
these keys.

## `notify`

```yaml
notify:
  webhook_url: "https://notify.example.invalid/hook/secret-token"
  transitions:
    - landed
    - blocked
    - needs_reconcile
    - daemon_paused
  timeout_seconds: 10
```

`daemon --notify` and `hub daemon --notify` send transition-deduplicated
notifications through a provider-neutral JSON webhook and the existing macOS
desktop backend. The webhook receives an HTTP `POST` with
`{"title":"...","message":"..."}` and `Content-Type: application/json`.
Slack/Discord-specific message shaping belongs in an adapter or relay; core does
not embed provider credentials or schemas.

`transitions` selects `landed`, `blocked`/partial, `needs_reconcile`, and daemon
error/pause messages. A disabled transition is recorded as settled so enabling
it later does not replay old history. `timeout_seconds` must be positive, and
the URL must use HTTP(S). Treat `webhook_url` as a secret: doctor/config JSON
reports only `webhook_configured`, never the URL. Delivery errors likewise omit
the credential-bearing URL.

## `gates`

```yaml
gates:
  - name: diff-check
    run: git diff --check ${integration_ref}..HEAD
  - name: tests
    run: python -m unittest discover -s tests
    paths:
      - src/**
      - tests/**
      - pyproject.toml
  - name: deploy-policy
    run: ./scripts/check-deploy-policy
    always_rerun_on_deploy: true
```

Gates run before push in the temporary integration worktree. The optional
`always_rerun_on_deploy` flag matters only when validated-gate reuse is accepted;
that gate still runs against the exact restored validation commit.

An optional non-empty `paths` list scopes a pre-push gate to the assembled
train's changed paths. A scoped gate runs when any changed path matches any
pattern; otherwise it emits a structured `skipped` gate event and keeps its
normal gate index. Gates without `paths` always run, as does mergetrain's
built-in `diff-check`.

Patterns are repository-relative POSIX globs on every platform. `*`, `?`, and
character classes match within one path segment; a segment containing only
`**` matches zero or more complete segments. Absolute paths, `.`/`..` segments,
backslashes, empty segments, duplicate patterns, and `**` embedded inside
another segment are rejected. `paths` is supported only for top-level
pre-push `gates`, not `deploy.verify` or reuse fingerprints.

mergetrain computes the path set once from the captured integration-base commit
to the exact assembled train commit. Rename and copy records include both the
old and new path; deletions remain visible. If Git cannot produce or mergetrain
cannot parse the path set, every scoped gate runs. A path-selection failure can
therefore cost time but can never silently weaken validation.

Every `run` string is executed by a **POSIX `sh`, on every platform** — mergetrain
never falls back to `cmd.exe`. On Windows it uses `sh` from `PATH` or the one Git
for Windows ships (`sh.exe` under the Git installation), and refuses to run gates
at all when no POSIX shell can be found, rather than running them under a shell
with different quoting rules. So one gate command works everywhere: write POSIX
shell syntax, not `cmd` syntax, and expect POSIX quoting and expansion.

## `deploy.reuse`

```yaml
deploy:
  reuse:
    enabled: false
    max_age_minutes: 60
    on_mismatch: rerun # rerun | fail
    fingerprints:
      - name: toolchain
        run: ./scripts/toolchain-fingerprint
```

Validated-gate reuse is opt-in. Set `enabled: true` for configuration-level
authorization or pass `run-batch --deploy --reuse-validated` for one deploy.
Reuse requires the recorded integration base, task heads, train membership,
validation commit/tree, gate policy, environment fingerprints, and validation
age to match. `on_mismatch: rerun` performs the normal full reassembly and gate
run; `fail` blocks before push. The default remains full gate rerun.

Each fingerprint command must print one stable, opaque, non-empty line of at
most 512 characters. mergetrain hashes the values instead of storing them.
Adapters can use these commands to identify a compiler, SDK, container image,
or other environment-sensitive input. Post-push `deploy.verify` hooks always
run, including after gate reuse.

## `deploy.verify`

```yaml
deploy:
  verify:
    - name: live-health
      run: curl -fsS https://example.invalid/health
```

Verify hooks run after push. A verify failure means the remote ref was already
updated, so mergetrain keeps `status=deployed` while recording
`push_status=succeeded`, `verify_status=failed`, and a warning note. Runs with no
hooks record `verify_status=not_configured`; configured hooks that all pass record
`verify_status=succeeded`.

## Placeholders and environment

Placeholders available in `gates`, `deploy.reuse.fingerprints`, and
`deploy.verify`:

```text
${integration_ref}
${project}
${repo}
${worktree}
```

`${repo}` and `${worktree}` are escaped for their surrounding shell quote
context, so each expands to exactly one path argument even when the path contains
spaces or shell metacharacters. They may be used unquoted or inside matching
single or double quotes.

That escaping is POSIX, on every platform, because [the shell always is](#gates) —
including for Windows paths, whose backslashes survive the expansion. A gate that
embeds a path inside a *Python* string literal is a separate concern: `python -c
"...'${worktree}'..."` would read `C:\Users` as an escape sequence, so pass such
paths as arguments (`python script.py "${worktree}"`) or read them from
`MERGETRAIN_WORKTREE` instead.

Equivalent environment variables:

```text
MERGETRAIN_PROJECT
MERGETRAIN_INTEGRATION_REF
MERGETRAIN_REPO
MERGETRAIN_RUNNER_PYTHON
MERGETRAIN_WORKTREE
```

mergetrain prepends the directory containing `MERGETRAIN_RUNNER_PYTHON` to
`PATH` for gates, reuse fingerprints, and verify hooks. Invoking mergetrain
through a virtualenv or pipx interpreter therefore makes sibling tools such as
`ruff`, `mypy`, and versioned Python launchers available without activating that
environment in the parent shell. Existing `PATH` entries retain their order
after the runner's tool directory.

Commands are executed through `/bin/sh`; treat config files as trusted code.
