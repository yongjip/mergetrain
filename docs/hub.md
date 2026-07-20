# Hub — one dashboard for every repo on your machine

The hub aggregates every registered repo into a single read-only dashboard:
each repo's queue, runner, validated trains, and next safe action on one page,
with a per-repo drill-down into the full single-repo view.

```bash
mergetrain hub add ~/projects/app        # register a repo (requires .mergetrain.yaml)
mergetrain hub add .                     # register the current repo
mergetrain hub list                      # show the roster
mergetrain hub                           # serve http://127.0.0.1:8765/
mergetrain hub status [--json]           # same aggregate, for terminals and agents
mergetrain hub remove ~/projects/app     # deregister (repo state untouched)
```

`hub add`/`hub remove` edit the roster only; they never touch the repo itself.
The registry lives at `$XDG_CONFIG_HOME/mergetrain/repos.json` (default
`~/.config/mergetrain/repos.json`; override with `MERGETRAIN_HUB_REGISTRY` or
`--registry`). It is re-read on every snapshot, so adding or removing a repo
shows up live without restarting the server.

## The contract: sovereign repos, stateless hub

The hub owns no correctness-critical state
([RFC #23](https://github.com/yongjip/mergetrain/issues/23)):

- Every repo entry is built by loading **that repo's own config** and opening
  **that repo's own SQLite database read-only**. Observing a repo never
  creates directories, never creates the queue database, and never migrates
  its schema — a registered repo with no queue yet renders as an idle card.
- A repo that is missing, unreadable, or on a different schema version
  becomes an isolated error card. One broken repo never breaks the page.
- Killing the hub at any moment loses a view, never data integrity. Queue
  state, runner locks, and the crash-recovery markers stay per-repo, exactly
  as without the hub.

## Security model

Same as the single-repo dashboard: loopback-only by default
(`--allow-remote` to override), no mutation endpoints (every non-GET request
returns 405), and the same payload redaction. One deliberate difference: the
hub shows each repo's home-relative path, because identifying repos is the
page's purpose.

Deploys, recovery, and cleanup remain explicit CLI actions inside each repo.
The hub cannot ship anything.

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

### Per-repo opt-out

Some repos must never see unattended deploys as a matter of policy, not just
because no `--auto` job happens to exist. Register them with
`mergetrain hub add REPO --no-daemon`: they stay on the dashboard (marked
"daemon off") but every `hub daemon` sweep reports them `excluded` without
claiming anything. Re-run `mergetrain hub add REPO --daemon` to re-enable.
The flag lives in the registry, not the repo.

## Snapshot caching

The dashboard rebuilds the hub payload once per second per connected client.
To keep that cheap, the server reuses a repo's entry while its config file
and queue database (including the SQLite `-wal`, which every commit touches)
have unchanged mtime/size fingerprints — a handful of `stat` calls instead
of a YAML parse and a database open. Any queue write, config edit, or
`hub add`/`hub remove`/flag flip is visible on the next snapshot; error
entries are never cached.

## Relationship to `mergetrain dashboard`

`mergetrain dashboard` (single repo, run from inside that repo) is unchanged.
The hub serves the same UI in multi-repo mode; clicking a repo card opens the
identical single-repo view fed from that repo's data.
