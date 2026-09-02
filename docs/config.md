# Config reference

Default config file name:

```text
.mergetrain.yaml
```

## Parser support

PyYAML is a required dependency, and mergetrain always uses `safe_load` for
`.mergetrain.yaml`. Existing block-style files remain unchanged, while standard
flow-style collections such as `[main, release]` and `{name: tests}` now work
consistently on every installation. Legacy YAML booleans (`yes`, `no`, `on`,
`off`), prefixed or leading-zero integers, and tab indentation are rejected so
operator policy cannot change meaning through implicit scalar conversion.
Invalid mapping, list, string, boolean, path, and positive-integer values also
fail closed during typed config validation.

YAML remains the primary format to preserve committed project configuration and
the existing `init`/documentation contract. Moving new files to TOML would not
remove YAML loading for existing repositories, and Python 3.10 would require a
second parser dependency because `tomllib` arrived in Python 3.11. Requiring one
standards-based YAML implementation removes the custom-parser burden without
introducing dual-format discovery, precedence, or migration rules.

## `version`

```yaml
version: 2
```

The config schema version. `mergetrain init` writes the current version (`2`);
an omitted `version:` is treated as version 1 and migrated in memory. Version-1
files may still contain the removed no-op `agent` block or presentation-only
`terminology` block; both are ignored during migration. A version-2 file that
declares either removed key is rejected so stale policy cannot appear active.
A file whose `version:` is **newer**
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

Relative runtime-state paths are resolved from one shared control root. In a
standard Git linked worktree, mergetrain follows Git's `.git`/`commondir`
metadata to the control checkout, so the control checkout and all task
worktrees use the same queue DB, logs, runner lock, and integration-worktree
directory. In an ordinary checkout, a non-Git directory, a submodule, or
malformed/nonstandard worktree metadata, mergetrain keeps the historical
repository-root resolution. Absolute paths remain unchanged, and a relative
global `--db` override remains relative to the explicitly selected `--repo`.

`validation_workspace.cache_paths` are different: they name directories inside
the validation checkout and therefore remain normalized repository-relative
paths rather than runtime-state locations.

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

`remote` names the fetch remote used to assemble the train. Before approval,
mergetrain separately resolves its effective push endpoint with
`git remote get-url --push --all` from the control checkout. A split setup such
as `origin.url = fetch-A` plus `origin.pushurl = push-B` is supported and binds
approval to `push-B`. Exactly one effective push URL is required: multiple
`pushurl` values fail closed because they cannot be one remote atomic
transaction.

Relative filesystem push URLs (for example `../remote.git`) are rejected;
their meaning changes with the integration worktree's current directory. Use
an absolute path or a `file://` URL. Absolute local paths and local file URLs
are resolved to their canonical target before approval, so changing a symlink
later cannot redirect the push. Network URLs and Git's SCP-like SSH form remain
supported.

Deploy mode pushes the verified commit and its content-addressed recovery audit
ref atomically:

```sh
git push --atomic origin \
  <sha>:main \
  <sha>:refs/mergetrain/deploys/<sha>
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
git push --atomic platform \
  <sha>:develop \
  <sha>:main \
  <sha>:refs/mergetrain/deploys/<sha>
```

The actual command also protects the audit ref with `--force-with-lease` so it
can only be created or retain the identical value. The configured remote must
permit creation under `refs/mergetrain/deploys/`; these refs are permanent
recovery evidence and are not payload targets configurable through
`push_refs`.

Approval summary, destination hash, audit lookup, pending marker, atomic push,
and reconcile all use the same resolved push endpoint identity. The live raw
URL is held only in memory and supplied to Git through a fresh random sentinel
and transient remote alias; endpoint-matching URL rewrite rules are not
re-applied. Credentials are not written to the queue database or command line.
If the endpoint changes after gates or after an ambiguous push, mergetrain
blocks instead of consulting a different repository.

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
notifications through the provider-neutral JSON webhook. The webhook receives an HTTP `POST` with
`{"title":"...","message":"..."}` and `Content-Type: application/json`.
Slack/Discord-specific message shaping belongs in an adapter or relay; core does
not embed provider credentials or schemas.

If `--notify` is supplied without `webhook_url`, the single-repo daemon warns at
startup and Hub warns once per affected repository when a transition needs
delivery. This avoids treating an intentionally empty backend chain as a
successful headless notification.

Interactive desktop alerts are configured in the open `dashboard` or `hub`
page instead. Click **Enable notifications** once to grant the browser permission
and remember the preference for that dashboard origin. Browser alerts work on
supported macOS and Windows browsers without a platform helper, stop when the
page closes, and focus the relevant dashboard (including the affected Hub repo)
when clicked. They do not require `--notify` or a webhook.
Embedded or in-app browsers may intentionally deny site notifications; in that
case, open the same loopback dashboard URL in Safari, Chrome, Edge, or another
desktop browser that exposes notification permission.
Loopback HTTP is treated as a secure browser context; a remotely exposed
dashboard needs HTTPS before browsers will offer notification permission.

For headless webhook delivery, `transitions` selects `landed`,
`blocked`/partial, `needs_reconcile`, and daemon error/pause messages. A disabled
transition is recorded as settled so enabling it later does not replay old
history. `timeout_seconds` must be positive, and the URL must use HTTP(S).
Treat `webhook_url` as a secret: doctor/config JSON reports only
`webhook_configured`, never the URL. Delivery errors likewise omit the
credential-bearing URL.

## `gates`

```yaml
gate_parallelism:
  # The default is 1, which preserves strictly sequential execution.
  max_workers: 4
  # Optional wall-clock ceiling for the complete configured gate plan.
  timeout_seconds: 1800

gates:
  - name: tests
    run: python -m unittest discover -s tests
    paths:
      - src/**
      - tests/**
      - pyproject.toml
  - name: deploy-policy
    run: ./scripts/check-deploy-policy
    always_rerun_on_deploy: true
  - name: ruff
    run: ruff check .
    parallel_group: quality
    needs:
      - tests
    workers: 1
    timeout_seconds: 120
  - name: mypy
    run: mypy
    parallel_group: quality
    needs:
      - tests
    workers: 2
```

Gates run before push in the temporary integration worktree. The optional
`always_rerun_on_deploy` flag matters only when validated-gate reuse is accepted;
that gate still runs against the exact restored validation commit.

mergetrain always runs one built-in `diff-check` before configured gates. Older
generated configs may still contain the same gate explicitly; that exact
default is ignored at runtime and `doctor` recommends removing it. A customized
gate is never discarded merely because it uses the same name.

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

Configured gates remain sequential unless they share the same non-empty,
contiguous `parallel_group`. `gate_parallelism.max_workers` is a resource-token
ceiling: a running gate consumes its positive `workers` value, and mergetrain
starts only a set whose total fits the ceiling. The weight describes the
command's expected CPU/worker cost; it does not rewrite an internal flag such as
`pytest -n`, so keep the declared weight consistent with the command.

Omitted `needs` preserves declaration order. A contiguous parallel group
defaults to the complete preceding stage; the following gate waits for every
member of that group. An explicit non-empty `needs` list may name only the
built-in `diff-check` or earlier configured gates. Group names cannot be
reopened later in the list, which makes the dependency graph acyclic and
unambiguous at load time.

`timeout_seconds` on a gate overrides `queue.command_timeout_seconds` for that
gate. `gate_parallelism.timeout_seconds` optionally bounds the whole configured
gate plan. The built-in integrity `diff-check` always completes before any
parallel group. If one parallel gate fails, times out, or the train is canceled,
mergetrain terminates every peer subprocess group. Per-gate logs and terminal
events are then committed in declaration order, so concurrent completion cannot
make JSON or logs nondeterministic. The same POSIX-shell resolution and process
tree cleanup apply on Linux, macOS, and Git for Windows.

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

`run-batch --deploy --preview --json` and the dashboard expose the same
structured reuse explanation: authorization, exact identity checks and mismatch
facts, the action for each gate (`reuse`, `rerun`, `skip`, or a conditional
preview state), and `estimated_savings`. Savings use up to 20 successful timing
samples per gate and report sample count, history coverage, and confidence.
They are an advisory sum of per-gate medians, not a promise of wall-clock time;
`estimated_savings.authorizes_reuse` is always `false`. Only explicit config or
`--reuse-validated`, followed by a matching exact identity check, can authorize
reuse. `always_rerun_on_deploy`, path-scoped skips, and fail-closed path
discovery are represented separately rather than counted as reused work.

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
