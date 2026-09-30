# Hub — one read for every repo on your machine

`mergetrain hub status` aggregates every registered repo into a single
read-only report: each repo's queue, runner, validated trains, and next safe
action.

```bash
mergetrain hub add ~/projects/app        # register a repo (requires .mergetrain.yaml)
mergetrain hub add .                     # register the current repo
mergetrain hub status [--json]           # full aggregate with each repo's jobs and events
mergetrain hub status --summary --json   # compact agent/coordinator read
mergetrain hub remove ~/projects/app     # deregister (repo state untouched)
```

`hub add`/`hub remove` edit the roster only; they never touch the repo itself.
The registry lives at `$XDG_CONFIG_HOME/mergetrain/repos.json` (default
`~/.config/mergetrain/repos.json`; override with `MERGETRAIN_HUB_REGISTRY` or
`--registry`). Every `hub status` read and every `hub daemon` sweep re-reads
it, so adding or removing a repo takes effect immediately.

Routine coordinators should use `hub status --summary --json`. It reads only
queue counts, the public runner lock, validated-train identities, and the next
safe action for each repo. Full `hub status --json` adds each repo's recent
jobs and events.

## The contract: sovereign repos, stateless hub

The hub owns no correctness-critical state
([RFC #23](https://github.com/yongjip/mergetrain/issues/23)):

- Every repo entry is built by loading **that repo's own config** and opening
  **that repo's own SQLite database read-only**. Observing a repo never
  creates directories, never creates the queue database, never writes a row,
  and never migrates its schema — a registered repo with no queue yet is
  reported as empty. (Honest limit: a WAL-mode reader may create or refresh
  SQLite's sidecar `-shm`/`-wal` files next to an existing database; queue
  data is never touched.)
- `hub daemon` probes each repo's policy state on that same read-only path:
  an idle sweep never opens a writable connection, so it can never migrate a
  swept repo's schema. Creating or migrating a queue database is reserved
  for commands run inside the repo itself.
- A repo that is missing, unreadable, or on a different schema version
  becomes an isolated error entry. One broken repo never breaks the read.
- Interrupting a hub command at any moment never costs data integrity. Queue
  state, runner locks, and the crash-recovery markers stay per-repo, exactly
  as without the hub.

## Security model

The hub listens on no network port: `hub status` is a one-shot local read. Its
payload masks inline secrets and the worktree and checkout paths in job notes,
drops local worktree and log paths, and reports runner owners without the OS
username. One deliberate difference: hub entries show each repo's home-relative
path, because identifying repos is the report's purpose.

Deploys, recovery, and cleanup remain explicit CLI actions inside each repo.
`hub status` cannot ship anything; `hub daemon` ships only jobs enqueued with
`--auto`, as described below.

## Hub daemon — auto-only execution across repos

```bash
mergetrain hub daemon                      # sweep all registered repos every 15s
mergetrain hub daemon --concurrency 2      # allow two repos to run gates at once
mergetrain hub daemon --once --json        # one sweep, machine-readable outcomes
```

The hub daemon is the multi-repo form of `mergetrain daemon`, and it adds no
new execution semantics: every repo is processed by the same per-tick policy
as the single-repo daemon — **only jobs enqueued with `--auto`**, behind that
repo's own runner lock, gates, and crash-recovery pauses (a repo with jobs
pending reconcile stays paused). What the hub adds is *scheduling*:

- `--concurrency` caps how many repos may run gates at the same time on this
  machine. The default is **1** — strictly serial — so heavy gates (engine
  builds, full test suites) from different repos never stack up.
- The registry is re-read every sweep; a repo with no queue database is
  skipped without creating one; a broken repo becomes an isolated error
  outcome and the sweep continues.

The `--auto` flag remains the explicit unattended-deploy approval boundary,
exactly as with the single-repo daemon. The hub daemon never touches
manually enqueued jobs.

`--notify` sends landed, blocked, reconcile, and daemon-pause transitions to
each repo's configured JSON webhook. Delivery is persisted and
transition-deduplicated. Without a webhook configured for a repo, there is no
notification backend for it.

### Per-repo opt-out

Some repos must never see unattended deploys as a matter of policy, not just
because no `--auto` job happens to exist. Register them with
`mergetrain hub add REPO --no-daemon`: they stay in `hub status` (with
`"daemon": false`) but every `hub daemon` sweep reports them `excluded` without
claiming anything. Re-run `mergetrain hub add REPO --daemon` to re-enable.
The flag lives in the registry, not the repo. It follows the repo's queue
rather than the registered path: a linked worktree of an excluded repo, which
shares its queue, is excluded too. However many registered paths reach one
queue, a sweep gives it one turn and reports the others `skipped`.
