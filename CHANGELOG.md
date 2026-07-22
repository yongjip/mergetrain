# Changelog

## Unreleased

## 0.8.1 - 2026-07-23

- Make README images and repository links use absolute GitHub URLs so the
  project description renders correctly on PyPI as well as GitHub.

## 0.8.0 - 2026-07-23

- Reframe the documentation around the integration requirement behind parallel
  agent coding: worktrees provide parallel execution lanes, while one train
  provides the serialized boundary that assembles, proves, and ships their
  combined result. Clarify that this layer preserves end-to-end throughput but
  does not replace task design, meaningful gates, or human-review policy.

- Add a balanced PR-first comparison guide: explain why committed agent
  branches are integration units rather than automatic review units, document
  mergetrain's throughput and combined-validation advantages alongside its
  local-runner/review/governance costs, and describe direct, one-PR, split, and
  validation-only hybrid workflows.

- Preserve remote truth across push and cancellation races (#94). Any
  non-policy push failure after the durable marker is written now parks the
  job in `needs_reconcile` instead of terminal `failed`, because the remote may
  already have accepted the atomic update. A concurrent cancellation request
  is retained for reconcile to honor when no ref landed. An unambiguous remote
  policy/permission rejection still becomes `blocked`, but now clears its DB
  marker and pending pin; successful deploys clear their pins as well. These
  transitions are claim-token/CAS fenced so a stale runner cannot erase newer
  recovery evidence.

- Harden dependency-free config parsing and secret redaction (#95). The built-in
  YAML subset parser rejects unsupported non-empty flow-style collections
  instead of silently treating them as strings, and invalid scalar/container
  types consistently produce `config_error`. Expected errors, persisted job
  notes, `doctor` remote URLs, status JSON, and dashboard snapshots now mask
  passwords embedded in URL userinfo in addition to sensitive assignments and
  command options.

- Expand fail-closed regression coverage for reconcile, recovery/GC, daemon
  TOCTOU guards, and dirty integration cleanup (#96). Consolidate the duplicated
  single/batch marker → push classification → post-push verification sequence
  into one safety path (#97). `status`, `doctor`, and the dashboard now use the
  same config-aware `next_action`, and `dismiss --all` processes every eligible
  blocked/failed row rather than a display-limited subset.

- Close the two post-0.7 adversarial hardening passes across the process, SQLite,
  and Git boundaries (#104–#118, #135–#146). Queue mutations and schema migration
  are fenced against stale owners; runner heartbeats preserve lock identity;
  replaced claims surface as typed lost leases; and registry edits preserve
  forward-compatible fields instead of rewriting data lossily.

- Make ambiguous pushes and crash recovery fail closed. Managed subprocesses
  receive bounded execution context, failed validation fingerprints cannot leak
  side effects into deploys, joint-failure isolation stops after uncertain remote
  state, definitive policy rejections remain distinguishable from ambiguous
  transport failures, and recovery verifies the expected ref pin before pushing.

- Harden every newly audited input boundary: gate commands run through the
  documented POSIX `/bin/sh`; substituted paths are shell-quoted; global CLI
  options honor `--`; status limits reject non-positive values; `init` detects
  scaffold collisions before writing; malformed lock timestamps fail soft; and
  file URLs are decoded exactly once.

- Keep machine-readable observation truthful under failure. Stream consumers now
  receive terminal error frames, pending reconcile ends inspection streams,
  dashboard request parsing rejects malformed and traversal-shaped input, and
  the hub cache preserves config-aware `next_action` state across snapshots.

- Apply one redaction and error taxonomy across CLI output, persisted notes,
  dashboards, and streams. Credential-bearing URL variants are masked, expected
  failures use stable machine codes, and branch resolution accepts qualified refs
  without silently selecting an ambiguous name.

- Raise the release-quality baseline with Ruff, mypy, coverage reporting,
  dedicated race/conflict state-machine tests, installed-wheel smoke tests,
  pinned publishing actions, Dependabot, `SECURITY.md`, and contributor guidance.
  CI continues to block on macOS, Linux, and Windows.

- Add an animated merge-train explainer, a static fallback, and updated recovery,
  configuration, agent-contract, and workflow documentation so the shipped safety
  model and its PR-first tradeoffs are visible before unattended use.

## 0.7.0 - 2026-07-21

- Never gc a live runner's worktree (0.9.0-prep). `gc --apply` listed and
  force-removed **every** mergetrain-prefixed worktree, including the
  integration worktree a running deploy was merging and gating inside, killing
  the run mid-deploy. `gc`, `doctor`, and `recover` now read the live runner
  lock's `worktree_path` and protect it (reported as `active runner worktree,
  skipped`, never removed).

- Unblock the documented first run (0.9.0-prep). mergetrain's own in-repo
  `.mergetrain/` state directory (queue DB, logs, worktrees) was left
  untracked, so the command that created it made the *next* `enqueue` fail the
  clean-worktree check — permanently. The state directory now self-ignores (a
  `.gitignore` of `*` written on first DB open), the dirty-worktree error names
  the offending paths, and `init --write` reports a `next_step` to commit the
  scaffold. A branch that already has a blocked/failed job now refuses
  re-enqueue with a typed `DuplicateActiveBranch` (`error.code:
  duplicate_active_branch`) whose message names the escapes, instead of a
  generic `queue_error` dead-end.

- Add `mergetrain dismiss` so a superseded blocked/failed job can be cleared
  non-destructively (0.9.0-prep). A blocked/failed job never lands and never
  self-clears, so it pinned `doctor`'s `next_action` to `fix_blocked_job`
  forever — hiding a ready validated train — and blocked re-enqueue of its
  branch; the only escape was `cancel`, which the operator docs classify as
  destructive. `dismiss <id>` (or `--all`) moves a blocked/failed job to the
  terminal `canceled` state, and by construction only ever touches
  already-failed outcomes — never queued or in-progress work — so an agent can
  run it unattended. The agent contract, the duplicate-branch error, the
  blocked-job notes, and the failure-modes recipe now point to it. The new
  `--json` surface is fingerprinted.

- Classify a policy-rejected push as `blocked`, not `failed` (0.9.0-prep). When
  the remote refuses the deploy push for a protected branch, a required pull
  request, a denied ref update, or a declined pre-receive hook, the job used to
  land `failed` — which tells an agent "the code is bad, rebase and re-enqueue",
  a wrong and self-perpetuating signal. It now parks `blocked` (a repo-config
  action, not a code fix), and `inspect --json` reports the stable
  `push_rejected` category so agents branch on that instead of regexing the
  note. Transient/infrastructure push failures still mark `failed`
  (`push_failed`). See [failure modes](./docs/failure-modes.md).

- Add `mergetrain verify` to discharge a crash-orphaned post-push verify
  (0.9.0-prep). A crash in the verify window finalizes the job `deployed` with
  `verify_status='unknown'`, and `doctor`'s `next_action` became a **permanent**
  `verify_reconciled_deploy` — no command could clear it, so it masked every
  lower-priority action forever. `mergetrain verify` re-runs the configured
  `deploy.verify` hooks against the recorded `deploy_sha` (assembled in a
  throwaway worktree) and records `succeeded`/`failed`, or takes
  `--ack succeeded|failed` for hooks that can't be re-run. `--job` targets one;
  the default resolves all unresolved. The new `--json` surface is fingerprinted.

- Make the auto-daemon report what actually **landed**, not merely what ran
  (0.9.0-prep). A sweep whose every job blocked on a conflict or failed its
  gates was indistinguishable from a green deploy — `daemon_tick` returned
  `processed:<n>` ("n ran") and the macOS notifier read it as "Train landed
  (n jobs)". Ticks are now graded by outcome: `landed:<n>` (all deployed),
  `partial:<d>/<n>` (some), or `no_landing:<n>` (nothing deployed —
  blocked/failed), and the notifications say so; a repo that keeps landing
  nothing notifies once, like a persistent error, instead of every tick.

- Make the recovery commands honor the contract-1 envelope and widen the
  fingerprint gate (0.9.0-prep). `reconcile`/`recover`/`unlock` returned
  `ok:false` with **no** `error` object when they ran to completion but found
  conflicts (exit 10), no lock (exit 5), or a refused force (exit 4) — using
  `ok` as an outcome grade, the exact thing contract 1 forbids. They now return
  `ok:true` (the command ran; the exit code carries the machine signal),
  `reconcile`/`recover` gain a graded `result` (`success`/`conflict`), and
  `unlock`'s `cleared` bool carries found-or-not — matching the `hub remove`
  precedent. Genuine errors (lock held, remote unreachable, bad config) still
  use the `{ok:false, error:{…}}` envelope. The golden fingerprint gate now
  also watches `recover`, `unlock`, `cancel`, and `hub status`, so their shapes
  can't drift silently before the freeze.

## 0.6.0 - 2026-07-21

- Document the machine contract (#44, Phase 4 — completes #44). New
  `docs/contract.md` enumerates every versioned surface, where
  `contract_version` lives, the contract-1 envelope (`ok`/`result`/`health`/
  the single failure shape), the additive-vs-breaking policy, the too-new
  config handling, and the 0.9.0 freeze linkage. The `agent-contract` payload
  gains a `machine_contract` boundary pointer, and `README`/`llms.txt`/
  `CLAUDE.md` point agents at it.

- Enforce the contract, two ways (#44, Phase 3 — the forcing function that
  makes the 0.9.0 freeze real). A checked-in golden **key-set fingerprint gate**
  (`tests/test_contract_fingerprints.py` + `contract_fingerprints.json`)
  captures the recursive key set of every agent-facing `--json` surface and
  each JSONL frame and fails CI on any un-bumped shape change, classifying it
  as additive (regenerate the golden) or breaking (bump `CONTRACT_VERSION`).
  And a **config preflight**: a `.mergetrain.yaml` whose `version:` is newer
  than this binary understands fails `enqueue`/`run-batch`/`run-next` closed
  with a `config_error` envelope, while `reconcile`/`recover`/`unlock` and all
  read-only commands stay permissive — so a rollback can never lock an operator
  out of crash recovery. `doctor` reports `next_action: upgrade_mergetrain` and
  `config_version_supported` in that state.

- Stamp `contract_version` on every machine-readable surface (#44, Phase 2).
  A single top-level integer (currently 1, from the new `mergetrain.contract`
  module) is injected at the one-shot JSON serializer (`dump_json`), at the
  HTTP `/api/snapshot` boundary (the dashboard-snapshot builder stays bare, so
  a hub payload's embedded per-repo snapshots carry no inner number), and as a
  new `stream_start` header re-emitted at the top of every `events --jsonl`
  stream (the existing `event`/`heartbeat`/`stream_end` frames are unchanged;
  dispatch JSONL on `type`). This is distinct from the product `__version__`,
  so a patch release never reads as a contract change.

- **Contract-1 JSON frame normalization (#44, Phase 1 — a deliberate breaking
  change to the `--json` envelope, made now because it is the last moment
  before the 0.9.0 API freeze).** `ok` now means exactly one thing on every
  command — "the command executed without raising an error envelope" — instead
  of four different things: `doctor`'s repo-health verdict moves to a new
  `health` field (`ok` is now always true when doctor runs); a completed run
  with a post-push verify warning is `ok:true, result:"warning"` (branch on
  `result`, never `ok`, for the outcome); `hub remove` is `ok:true` with the
  existing `removed` bool carrying found-or-not; `agent-contract --json` gains
  `ok:true`. `status --json` now carries `next_action`, so it and `doctor` are
  symmetric (CLAUDE.md tells agents to read either). All three failure shapes
  collapse into one envelope `{ok:false, error:{code,message,retryable},
  next_action?}` — the deploy-reconcile block now reports
  `error.code:"reconcile_pending_deploy"` instead of a bespoke
  `result:"blocked"`/`blocked_reason` shape. Exit codes are unchanged.

- Verify and support Windows (issue #33): the full suite now runs on
  `windows-latest` in CI as a **blocking** check. Fixes a real
  cross-platform bug — `owner_liveness` used `os.kill(pid, 0)`, which on
  Windows is `signal.CTRL_C_EVENT` and sent a real Ctrl-C to the probed
  process instead of checking existence (it would have disrupted the daemon,
  hub, and crash recovery); it now probes via `OpenProcess`/
  `GetExitCodeProcess`. A killed gate/command also returns promptly on
  Windows now (the stdout/stderr drain join is bounded instead of waiting up
  to 10s when `TerminateProcess` leaves a pipe read blocked). The rest of the
  work was test-fixture portability. `docs/install.md` now lists Windows as
  tested.

## 0.5.0 - 2026-07-21

- Harden the 0.4.0 hub after an adversarial review (issues #47–#51): make the
  `--no-daemon` opt-out a real guarantee (samefile registry identity, an
  advisory lock around registry mutations, fail-safe flag parsing, and a
  sweep-level exclusion for aliased duplicate entries); give both daemon loops
  a clean stop (a signal during the inter-sweep wait no longer triggers one
  more deploying sweep) and bound every git subprocess so one hung repo cannot
  starve a whole sweep; keep read-only observation honest (safe sqlite URI
  escaping, no schema migration on an idle sweep, documented WAL sidecar
  limit); stop the snapshot cache serving stale runner liveness / `next_action`
  or pinning stale entries; and fix `hub daemon --notify` dedup so a failed
  delivery is retried, dedup state persists across `--once`/cron runs, and a
  changed error re-notifies.

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
