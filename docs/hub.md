# Hub — one dashboard for every repo on your machine

The hub aggregates every registered repo into a single read-only dashboard:
each repo's queue, runner, validated trains, and next safe action on one page,
with a per-repo drill-down into the full single-repo view.

```bash
mergetrain hub add ~/projects/app        # register a repo (requires .mergetrain.yaml)
mergetrain hub add .                     # register the current repo
mergetrain hub list                      # show the roster
mergetrain hub                           # serve http://127.0.0.1:8765/
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

## Relationship to `mergetrain dashboard`

`mergetrain dashboard` (single repo, run from inside that repo) is unchanged.
The hub serves the same UI in multi-repo mode; clicking a repo card opens the
identical single-repo view fed from that repo's data.
