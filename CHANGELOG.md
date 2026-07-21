# Changelog

## Unreleased

- Escalate joint-failure isolation from linear to bisect (issue #38): when a
  train of more than 3 jobs fails its gates, the runner now bisects subsets
  (O(log n) gate runs instead of O(n)) to pin the failure. Jobs that fail
  alone finish `failed`; jobs that pass alone but fail together finish
  `blocked` as a named **semantic conflict**, with partner SHAs in the note
  and a new machine-readable `conflict_with` column (schema v7) listing the
  partner job IDs. Surviving jobs are re-run as a fresh train — bisection
  only removes jobs, so nothing ships without a full gate pass over the
  exact final combination. Trains of ≤3 jobs keep the existing one-by-one
  isolation.

- Add `hub daemon --notify` (issue #32 Stage 0): desktop notifications for
  landed trains, sweep errors, and reconcile pauses, deduplicated to state
  transitions so a persistently broken repo notifies once. macOS
  `osascript` only, zero new dependencies; silent no-op elsewhere.

## 0.4.0 - 2026-07-21

- Add a per-repo hub-daemon opt-out: `hub add REPO --no-daemon` keeps a repo
  on the dashboard but excludes it from every `hub daemon` sweep (policy-level
  guarantee for repos that must never see unattended deploys); re-run with
  `--daemon` to re-enable. Excluded repos report the `excluded` outcome and
  show a "daemon off" chip on their card.
- Cache hub snapshots by file fingerprint: a repo's dashboard entry is reused
  while its config and queue database (including the SQLite `-wal`) have
  unchanged mtime/size, replacing a YAML parse plus a database open per repo
  per second with a few `stat` calls. Registry-derived fields (the daemon
  flag) bypass the cache, and error entries are never cached.
- Harden the hub for release: a corrupt or unreadable registry file degrades
  to a visible `registry_error` banner on a live page instead of killing the
  snapshot endpoint and freezing the dashboard; the drill-down hash routes by
  repo path instead of roster index, so removing a repo can no longer switch
  the view to a different repo silently; `llms.txt`/`llms-full.txt` document
  the hub commands and the 0.3.0 `needs_reconcile` recovery contract.
- Add `mergetrain hub status` (RFC #23 Phase 2): one machine-wide read of
  every registered repo's queue — per-repo lines for humans, the hub
  dashboard's aggregate payload with `--json` for coordinator agents.
- Add `mergetrain hub daemon` (RFC #23 Phase 1): the auto-only daemon across
  every registered repo, scheduled machine-wide. Each repo runs through the
  same per-tick policy as the single-repo daemon (only `--auto` jobs, that
  repo's own lock, gates, and reconcile pauses); `--concurrency` caps how
  many repos may run gates simultaneously (default 1, strictly serial), and
  per-repo failures are isolated so a sweep never stops at a broken repo.
- Add `mergetrain hub` (RFC #23 Phase 0): a machine-level repo registry
  (`hub add`/`remove`/`list`) and one read-only multi-repo dashboard with
  per-repo drill-down. The hub owns no queue state — every repo entry is
  read from that repo's own config and SQLite database.
- Add a read-only observer path to queue access (`connect(read_only=True)`):
  no directory creation, no database creation, no schema migration. The hub
  renders a repo with no queue yet as idle and a schema-mismatched or broken
  repo as an isolated error card.

## 0.3.0 - 2026-07-20

- Crash-safe reconciliation and recovery: after any crash, reconcile local queue
  state against the real remote git state and never mislabel a deploy. A durable
  per-job pending-deploy marker (committed `synchronous=FULL` before every push)
  plus a `refs/mergetrain/pending/<id>` pin ref let recovery ask the remote for
  truth — never marking `deployed` unless a push ref carries the sha, never
  re-pushing a landed deploy, never guessing when the remote is unreachable.
- Add `reconcile`, `recover`, and `unlock` commands with a typed exit-code
  contract; a marker-aware orphan split parks a possibly-landed push in the new
  `needs_reconcile` state instead of blindly re-deploying it.
- Hard-block deploy (`run-batch`, `run-next`, and the daemon) while any job
  awaits reconcile; add DB-only `doctor` `next_action` guidance for the new
  states and sweep stale `refs/mergetrain/pending/*` pins during `gc --apply`.

## 0.2.0 - 2026-07-20

- Add opt-in `integrate`/`push` human vocabulary and CLI aliases while keeping
  the `--deploy`, `deployed`, `deploy_sha`, database, and JSON contracts stable.
- Show exact atomic push refspecs in previews and distinguish Git completion
  from downstream provider verification or release.
- Add resumable `events --follow --jsonl` progress with heartbeat and terminal frames.
- Add structured job/train `inspect --json` outcomes and confined `logs --follow` access.
- Keep subprocess output out of structured events while publishing active log paths early.

## 0.1.0 - 2026-07-17

First public alpha release.

- Preserve exact validated train identity for a later approval-gated deploy.
- Rebuild validated trains on the current integration ref and reject changed task HEADs.
- Exclude validated-but-not-deployed branches from destructive GC.
- Fence batch claims and state transitions with unique lease tokens.
- Heartbeat, cancel, and time out long-running Git and shell subprocesses.
- Reject explicitly empty deploy refs and invalid queue timing at config load.
- Return truthful JSON outcomes and non-zero exit codes for blocked/failed jobs.
- Version SQLite migrations with `PRAGMA user_version`.
- Add a loopback-first, read-only live dashboard with SSE and polling fallback.
- Distinguish browser connectivity from runner activity and explain the current gate, command, scope, and Activity milestones.
- Record structured runner phases and explicit lock heartbeat timestamps.
- Redact lease tokens and local filesystem paths from dashboard payloads.
- SQLite-backed local deploy queue.
- Runner lock with PID liveness checks.
- Git worktree merge train execution.
- Configurable pre-push gates and post-push verify hooks.
- Atomic push refs.
- Auto-only daemon boundary.
- JSON-first agent contract, status, doctor, and GC output.
