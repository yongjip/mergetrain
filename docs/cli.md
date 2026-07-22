# CLI reference

All commands share global options, which may appear **before or after** the subcommand (the parser normalizes their position, since agents often misplace them):

```sh
mergetrain --config <path> --repo <repo> --db <sqlite> <command>
mergetrain doctor --json --repo /path/to/repo      # equivalent to:
mergetrain --repo /path/to/repo doctor --json
```

| Global option | Meaning |
|---|---|
| `--config` | Path to `.mergetrain.yaml` (defaults to `<repo>/.mergetrain.yaml`). |
| `--repo` | Repository root or worktree path (defaults to the current directory). |
| `--db` | Override the SQLite DB path from config. |
| `--version` | Print the version and exit. |

`--version` keeps its stable one-line output (`mergetrain X.Y.Z`). Use the
`version` command when you also need to identify the code that was imported.

Command summary:

```text
mergetrain init [--project NAME] [--write] [--force]
mergetrain agent-contract [--json]
mergetrain version [--json]
mergetrain enqueue --task TASK --branch BRANCH [options]
mergetrain retry JOB_ID [--rebase] [--json]
mergetrain status [--json] [--limit N]
mergetrain events [--job ID | --train-id ID] [--after EVENT_ID] [--follow] [--jsonl]
mergetrain inspect JOB_ID [--event-limit N] [--json]
mergetrain history [--since TIMESTAMP] [--limit N] [--json]
mergetrain stats [--since TIMESTAMP] [--json]
mergetrain logs JOB_ID [--follow] [--tail N]
mergetrain doctor [--json]
mergetrain dashboard [--host HOST] [--port PORT] [--allow-remote] [--preview]
mergetrain run-next  (--validate-only | --deploy) [--keep-worktree] [--json]
mergetrain run-batch (--validate-only | --deploy) [--train-id ID] [--keep-worktree] [--json]
mergetrain daemon [--interval SECONDS] [--once] [--notify] [--keep-worktree]
mergetrain gc [--json] [--apply] [--delete-branches]
mergetrain reconcile [--apply] [--json]
mergetrain recover [--gc] [--json]
mergetrain unlock [--force] [--json]
mergetrain cancel JOB_ID [--note NOTE] [--json]
mergetrain dismiss [JOB_ID | --all] [--note NOTE] [--json]
mergetrain verify [--job ID] [--ack succeeded|failed] [--json]
mergetrain hub [add|remove|list|status|daemon] [--host HOST] [--port PORT] [--allow-remote] [--registry PATH]
```

## `init`

Print or write starter config and agent instructions.

```sh
mergetrain init --project demo            # print config to stdout
mergetrain init --project demo --write    # write files into the repo
```

`--write` creates `.mergetrain.yaml`, `AGENTS.mergetrain.md`, and `CLAUDE.mergetrain.md`. Existing files are not overwritten without `--force`. See [config reference](config.md).

## `agent-contract`

Print the short operating rules an agent must follow.

```sh
mergetrain agent-contract
mergetrain agent-contract --json
```

`--json` emits `name`, `purpose`, `rules`, and `boundary`. See [agent contract](agent-contract.md).

## `version`

Show the semantic version plus installed-package provenance:

```sh
mergetrain version
mergetrain version --json
```

The structured `runtime` object contains `distribution_version`, the actual
`package_path`, `install_mode` (`wheel`, `editable`, or `unknown`), optional
`source_path`, optional `source_commit`, and optional `source_dirty`. Editable
mode is identified from the installed distribution's PEP 610 `direct_url.json`;
paths are not classified by naming convention. A VCS install can report the
commit recorded by PEP 610 even when the installed wheel has no Git checkout.
Unavailable metadata is returned as `null` or `unknown`, never as a command
failure.

## `enqueue`

Add a task branch to the queue.

```sh
mergetrain enqueue --task feature-a --branch agent/feature-a --capture-sha
```

| Option | Meaning |
|---|---|
| `--task` | Human-readable job name (required). |
| `--branch` | Task branch to merge (required). |
| `--worktree` | Originating worktree path (defaults to cwd). |
| `--base-sha` / `--head-sha` | Record SHAs manually. |
| `--capture-sha` | Capture the integration ref and branch SHAs automatically. |
| `--note` | Free-text status note. |
| `--auto` | Mark the job eligible for the unattended daemon (requires prior approval). |
| `--allow-duplicate` | Allow a second active job for the same branch. |
| `--allow-dirty` | Allow enqueue from a dirty worktree. |
| `--allow-branch-mismatch` | Allow the worktree's current branch to differ from `--branch`. |
| `--no-ready-check` | Skip Git readiness checks and insert directly. |
| `--json` | Emit the created job as JSON. |

Defaults are safe: enqueue fails if the worktree is missing or dirty, if the current branch differs from `--branch`, or if the branch already has an active job.

## `retry`

Replace a fixed `blocked`/`failed` job with a fresh queue job without retyping
its metadata:

```sh
mergetrain retry 12 --json
mergetrain retry 12 --rebase
```

The replacement inherits the original task, note, worktree, branch, and `--auto`
eligibility, and always captures fresh integration/base and branch/head SHAs.
Dismissal and insertion are one SQLite transaction. With `--rebase`, Git fetch
and rebase run first; any fetch error or rebase conflict leaves the old queue row
untouched. The worktree must be clean and checked out on the job's branch.

## `status`

Print queue and lock state.

```sh
mergetrain status --json --limit 50
```

`--json` returns `ok`, `db`, `lock`, `jobs`, and `validated_trains`. `--limit` caps the job list (default 50).

## `events`

Read the retained structured runner events once, or follow a job/train without
polling process lists:

```sh
mergetrain events --job 42 --after 0 --follow --jsonl
mergetrain events --train-id <id> --after 183 --follow --jsonl
```

`--job` includes that job's own events plus shared batch phases such as fetch and
gating. `--train-id` includes the full train. Without a scope, one-shot mode
shows recent repository events and follow mode continues until interrupted.

The `--jsonl` framing contract is one compact JSON object followed by one newline:

- `type=stream_start` is the first frame on every connect or resume. It carries
  the stream's `contract_version` and does not consume an event ID.
- `type=event` is a persisted event. Its integer `id` is the resume cursor. It
  includes phase/state, optional job and gate index/name, elapsed seconds, and
  the latest lease heartbeat visible when read.
- `type=heartbeat` is an ephemeral follow-only liveness frame emitted when the
  persisted runner heartbeat advances during a long command. It does not consume
  an event ID.
- `type=stream_end` is the final scoped-follow frame. `reason` is `success`,
  `failure`, `canceled`, `lost_lease`, or `interrupted`.

`--after N` is exclusive. `--after 0` starts at the oldest retained event;
reconnect with the last `type=event.id` already processed. Events are bounded to
the newest 5,000 database rows. Without `--after`, the command returns the latest
`--limit` rows (default/max 200). Heartbeat frames are intentionally not replayed.

A scoped follower exits `0` on validation/deploy success, `1` on failure,
cancellation, or lost lease, and `130` after an interrupt. It drains persisted
events before the final frame. Existing `run-batch --json` remains a single final
JSON document; JSONL progress is a separate command and never shares that stdout.

## `inspect`

Return one stable snapshot for a job and its train:

```sh
mergetrain inspect 42 --json
```

The JSON contains `job`, `progress`, `outcome`, `train`, and recent `events`.
`progress` names the current phase, gate index/total/name, elapsed time, latest
heartbeat, lease liveness, and lost-lease state. `outcome` has a stable severity,
category, nullable `failure_category`, and `warning_categories`. A train snapshot
aggregates status counts and per-job failure/warning categories, so callers do
not need to classify free-text notes.

## `history`

Read recent durable train/job history without opening the queue for writes:

```sh
mergetrain history --limit 50 --json
mergetrain history --since 2026-07-01T00:00:00Z
```

Jobs sharing a non-empty `train_id` remain one complete item even at the limit;
legacy/single jobs use `job:<id>`. Each item includes status, queue wait,
duration, structured outcome, member jobs, and retained gate runs. `--since`
accepts ISO-8601 timestamps and is inclusive.

## `stats`

Aggregate the same read-only history:

```sh
mergetrain stats --since 2026-07-01T00:00:00Z --json
```

The payload reports landed/blocked/failed trains, land rate, median and p95
train duration, average queue wait, and per-gate run states and timing. Queue
rows are not automatically pruned, so their history is unbounded. Gate timing
is explicitly marked as covering the latest 5,000 retained runner events; it
never pretends an older truncated event tail is complete.

## `logs`

Read the explicit local runner log for one job:

```sh
mergetrain logs 42 --tail 200
mergetrain logs 42 --follow --tail 20
```

The default is the latest 200 existing lines. `--tail 0` prints no existing
lines before following. The runner stores `log_path` as soon as processing starts,
so a follower can attach during a long gate. Follow ends with the same success/
failure/cancellation/lost-lease exit policy as `events`. Log paths are confined
to configured `state.logs`; this command never reads an arbitrary path from a
modified queue database.

## `doctor`

Diagnose config, queue, Git remote, integration ref, GC candidates, and the next safe action.

```sh
mergetrain doctor --json
```

Key JSON fields: `ok`, `version`, `runtime`, `config`, `config_exists`, `db`, `db_existed_before`, `state.logs`, `state.worktree_root`, `git.repo_root`, `git.current_branch`, `git.worktree_clean`, `git.remote_url`, `git.remote_exists`, `git.integration_ref`, `git.integration_ref_exists`, `lock`, `counts`, `validated_trains`, `gc.worktree_candidates`, and `next_action`. `runtime` has the same provenance contract as `version --json`.

`next_action` is one of:

- `unlock_wedged_runner` — an expired lease is held by an owner that still looks alive with in-progress work; run `unlock --force` (0.3.0).
- `wait_for_runner` — a live runner already holds the lock.
- `reconcile_pending_deploy` — a crash or ambiguous push response left jobs
  `needs_reconcile` (or a marker-bearing orphan); resolve with `reconcile`
  before deploying (0.3.0).
- `reconcile_conflict_manual` — `reconcile` left a job `blocked` with its marker; git inspection is required (0.3.0).
- `fix_blocked_job` — there are `blocked`/`failed` jobs to resolve first.
- `verify_reconciled_deploy` — a reconciled deploy landed but its post-push verify could not be proven (`verify_status='unknown'`); run `mergetrain verify` to re-run the `deploy.verify` hooks against the recorded `deploy_sha` (or `mergetrain verify --ack succeeded|failed` for hooks that cannot be re-run). This clears the state.
- `deploy_validated_train_when_approved` — an exact validated train is waiting for explicit deploy approval.
- `cancel_and_reenqueue_legacy_validated_jobs` — pre-migration validated jobs lack safe train identity.
- `run_daemon_or_run_batch_deploy_when_approved` — auto-approved jobs are queued.
- `run_batch_validate` — manual jobs are queued; validate them.
- `gc_available` — only cleanup remains.
- `enqueue_clean_branch` — the queue is empty.
- `upgrade_mergetrain` — the config declares a `version:` newer than this binary supports; this **overrides** the action above, and `doctor --json` also sets `config_version_supported` to the highest version this binary understands. Upgrade mergetrain before acting.

`next_action` is **advisory**; it never substitutes for a destructive action or deploy consent.

The `counts` map also carries derived recovery signals — `needs_reconcile`,
`in_progress_with_marker`, `blocked_with_marker`, and `deployed_verify_unknown`
— all computed from local DB state (no remote call).

## `dashboard`

Serve the single-repository live status dashboard:

```sh
mergetrain dashboard
# http://127.0.0.1:8765/
```

The UI is deliberately read-only. It shows queue order, the active runner phase,
heartbeat and lease freshness, recent structured events, blocked history, and the
same advisory next action used by `doctor`. Server-sent events deliver live
snapshots; the client falls back to two-second polling if the stream is
interrupted. The connection indicator is distinct from runner ownership, and the
current-check panel explains the active gate, scope, command template, and elapsed
time.

| Option | Meaning |
|---|---|
| `--host` | Bind host (default `127.0.0.1`). |
| `--port` | Bind port (default `8765`; `0` selects an available port). |
| `--allow-remote` | Required acknowledgement when binding outside `127.0.0.1`, `localhost`, or `::1`. |
| `--preview` | Label the connected database as synthetic preview data. This does not generate fixtures. |

Remote binding expands access to queue metadata and status notes. Prefer the
loopback default; there are no authentication or TLS layers in this local tool.

## `hub`

Serve the same dashboard in multi-repo mode, aggregating every registered
repo on one read-only board with a per-repo drill-down:

```sh
mergetrain hub add ~/projects/app   # register (requires .mergetrain.yaml)
mergetrain hub add ~/projects/app --no-daemon   # keep on the board, never sweep
mergetrain hub list [--json]
mergetrain hub                      # serve http://127.0.0.1:8765/
mergetrain hub remove ~/projects/app
```

`--daemon/--no-daemon` upserts hub-daemon eligibility for the repo (re-run
`add` to flip an existing entry). Excluded repos stay on the dashboard and in
`hub status`, but every `hub daemon` sweep reports them `excluded`.

| Option | Meaning |
|---|---|
| `--host` / `--port` / `--allow-remote` | Same semantics as `dashboard`. |
| `--registry` | Override the roster file (default `$XDG_CONFIG_HOME/mergetrain/repos.json`, or the `MERGETRAIN_HUB_REGISTRY` environment variable). |

The hub owns no queue state: each repo entry is read from that repo's own
config and SQLite database, opened read-only — observing a repo never creates
or migrates anything inside it, and one broken repo renders as an isolated
error card. The registry is re-read on every snapshot, so `hub add`/`hub
remove` show up live. Full contract in [hub.md](./hub.md).

### `hub status`

One machine-wide read of every registered repo's queue — the coordinator-agent
counterpart of the hub dashboard:

```sh
mergetrain hub status [--json] [--registry PATH]
```

Human mode prints one line per repo (nonzero counts, runner liveness, and the
advisory next action); `--json` emits the same aggregate payload the hub
dashboard serves, per-repo errors isolated.

### `hub daemon`

Run the auto-only daemon across every registered repo:

```sh
mergetrain hub daemon [--interval 15] [--concurrency 1] [--notify] [--once [--json]] [--keep-worktree] [--registry PATH]
```

`--notify` sends each repo's configured transitions through its optional JSON
webhook and the macOS desktop backend. Without a webhook it remains a silent
desktop no-op off macOS. Delivery is persisted and transition-deduplicated.

Each repo is processed by the same per-tick policy as the single-repo
`daemon` — only `--auto` jobs, behind that repo's own lock, gates, and
reconcile pauses. `--concurrency` caps how many repos may run gates at the
same time machine-wide (default `1`: strictly serial). `--once` runs a
single sweep; with `--json` it prints one outcome per repo
(`processed:<n>` / `idle` / `skipped` / `reconcile_paused` / `error`).

## `run-next`

Process exactly one queued job. Requires `--validate-only` or one Git push mode:
`--deploy`, `--integrate`, or `--push`.

```sh
mergetrain run-next --validate-only
mergetrain run-next --deploy
mergetrain run-next --integrate
```

`--keep-worktree` leaves the temporary integration worktree in place for inspection. See [Design → Runner behavior](design.md#runner-behavior).
Validated jobs are deployed through `run-batch --deploy` so train identity is
preserved, including when the train contains only one job.

## `run-batch`

Validate all currently queued jobs as one merge train, or deploy an exact
validated train. Requires `--validate-only` or `--deploy`; `--integrate` and
`--push` are aliases for the identical atomic Git operation.

```sh
mergetrain run-batch --validate-only
mergetrain run-batch --deploy
mergetrain run-batch --integrate
mergetrain run-batch --deploy --train-id <id>
mergetrain run-batch --deploy --train-id <id> --reuse-validated --preview --json
mergetrain run-batch --deploy --train-id <id> --reuse-validated
```

After validation, plain `--deploy` selects the only pending validated train and
leaves newer queued jobs untouched. If more than one train is pending, deployment
fails safely until `--train-id` selects one. The runner verifies every validated
task HEAD, rebuilds on the current integration ref, and reruns gates before push.
Changed task HEADs block the whole validated train. During initial validation,
conflicts still block only the offending job and a train gate failure is
isolated per job — one-by-one for small trains, by bisection with
semantic-conflict reporting (`conflict_with`) for trains of more than 3
jobs. See [Design → Batch](design.md#batch--merge-train-run-batch).

Validated-gate reuse is disabled by default. `--reuse-validated` authorizes the
configured policy for that command; `deploy.reuse.enabled: true` is the persistent
alternative. `--preview` does not claim, gate, or push, but configured fingerprint
commands still run so the decision matches a real deploy.
Its JSON `reuse` object reports `authorized`, `eligible`, `action`, mismatch
`reasons`, `validation_sha`, and the exact `reused_validation_sha` when safe.
Deploy JSON also exposes `reused_validation_shas`, and every reused job retains
`reused_validation_sha`. A mismatch reruns all gates unless policy says `fail`.
Preview JSON includes `push_plan.remote` and each exact `HEAD:<ref>` refspec.
`terminology.git_operation` changes only human wording and the preferred alias;
machine JSON continues to report `mode=deploy` and `status=deployed`.

## `daemon`

Run an unattended, auto-only worker.

```sh
mergetrain daemon --interval 15
mergetrain daemon --once
mergetrain daemon --once --notify
```

Claims only jobs enqueued with `--auto`. `--notify` uses the same persisted
landed/blocked/reconcile/error transition dedup as `hub daemon`, plus the
provider-neutral webhook configured under `notify`. See the
[daemon guide](daemon.md) and [config reference](config.md#notify).

## `gc`

Clean up temporary worktrees and, optionally, terminal branches. Dry-run by default.

```sh
mergetrain gc --json                              # dry run
mergetrain gc --apply --json                      # remove temp worktrees
mergetrain gc --apply --delete-branches --json    # also delete terminal branches
```

Branch deletion only targets branches of `deployed`/`canceled` jobs and never
deletes a validated-but-not-deployed branch or a protected ref (`push_refs`, the
integration branch, or the currently checked-out branch).

`gc --apply` also sweeps stale `refs/mergetrain/pending/*` pin refs (0.3.0): a pin
whose owning job is terminal/failed/missing is deleted (reported under
`result.swept_pending_refs`), while a `blocked` (reconcile-conflict forensics) or
`needs_reconcile` job keeps its pin.

## `reconcile`

Resolve every `needs_reconcile` job against the **remote** after a crash or
ambiguous push response (0.3.0).
Reads the durable per-job `pending_deploy_sha` marker written before the push,
then asks the remote (`git fetch` + `ls-remote` + `merge-base --is-ancestor`)
whether the deploy actually landed. **Never pushes.** Dry-run by default.

```sh
mergetrain reconcile --json           # dry run: classify, write nothing
mergetrain reconcile --apply --json   # finalize the reconciled outcome
```

While any job is `needs_reconcile` (or a not-yet-split marker-bearing orphan
exists), **all** deploy paths refuse — `run-batch --deploy`, `run-next --deploy`,
and the `daemon` tick — since they target the same push refs.

Per job: the deploy sha present on **every** push ref → `deployed`
(`push_status=succeeded`, `verify_status=unknown` — the deploy is not re-pushed
and verify is not re-run); present on **none** → `queued` (or `canceled` if a
cancel had raced the push); present on **some** refs, or the sha is unresolvable
→ `blocked` (human git inspection). Exit codes: `0` resolved/nothing to do · `2`
usage/config · `3` lock held by a live runner (retryable) · `7` remote
unreachable (nothing changed) · `10` ≥1 job left `blocked`.

## `recover`

One-button restart heal: split a previous runner's orphaned `in_progress` jobs
(marker-aware) and then `reconcile --apply`. Never ships queued or validated work
— there is no deploy as a side effect. Same exit codes as `reconcile`.

```sh
mergetrain recover --json          # split orphans + reconcile
mergetrain recover --gc --json     # also remove crashed worktrees
```

## `unlock`

Clear a wedged runner lock. Without `--force` only a dead/absent owner's lock is
cleared. With `--force` the remote is confirmed reachable **first** (unreachable
→ abort, change nothing), then the lock is deleted and orphans are split; it
never itself writes `deployed`/`failed` and appends an append-only audit event.

```sh
mergetrain unlock --json           # clear a dead owner's lock
mergetrain unlock --force --json   # steal a live/unknown owner's lock
```

Exit codes: `0` cleared · `2` usage/config · `4` refused (owner alive, no
`--force`) · `5` no lock · `7` remote unreachable during the forced classify.

## `cancel`

Cancel a non-terminal queue item. Terminal items cannot be cancelled.

```sh
mergetrain cancel 12 --note "replaced by rebased branch"
```

Canceling one member of a validated train cancels every still-validated member
of that train so a partial copy of the approved train cannot later deploy. For
an `in_progress` train, cancel records `cancel_requested_at` for every job with
the same claim token. The runner notices the request during its heartbeat,
terminates the active subprocess group, and records the final `canceled` state.

## `dismiss`

Non-destructively clear a superseded `blocked`/`failed` job. A blocked/failed job
never lands and never self-clears, so it keeps `doctor`'s `next_action` pinned to
`fix_blocked_job` (hiding a ready validated train) and blocks re-enqueue of its
branch. `dismiss` moves it to the terminal `canceled` state; it refuses
`queued`/`in_progress` work (use `cancel` for that) and never touches git or the
remote, so it is safe to run unattended.

```sh
mergetrain dismiss 12 --note "fixed on a rebased branch"
mergetrain dismiss --all      # every blocked/failed job
```

`--all` selects every eligible row directly from the queue; it is not limited by
the default `status --limit` display window.

## `verify`

Discharge deployed jobs a crash left with `verify_status='unknown'` — reconcile
can prove a push landed but not that the post-push `deploy.verify` hooks ran, so
it parks the job at `verify_reconciled_deploy`. `verify` clears that.

```sh
mergetrain verify                 # re-run deploy.verify against every unresolved deploy_sha
mergetrain verify --job 12        # resolve one job
mergetrain verify --ack succeeded # record the outcome without re-running (non-repeatable hooks)
```

Without `--ack`, `verify` re-runs the configured `deploy.verify` hooks against the
recorded `deploy_sha` and records `succeeded`/`failed`; `--ack succeeded|failed`
records the result without re-running.

## Exit codes

`0` every requested job shipped — **including** a train that landed but whose
post-push verify only warned (`result:"warning"`) · `1` a job blocked/failed, or
an expected config/queue/command error · `2` usage error · `130` interrupted
(`Ctrl-C`). Exit `1` never means "did not ship" on its own — read `result`. In
JSON mode, run results include `ok`, `result` (`success`, `warning`, `partial`,
or `failed`), per-status `counts`, `push_counts`, `verify_counts`,
`reused_validation_shas`, and `jobs`. Each job exposes `push_status` (`not_run`,
`succeeded`, or `failed`) and `verify_status` (`not_run`, `not_configured`,
`succeeded`, or `failed`). A successful push followed by a failed verify keeps
`status=deployed` and returns `result=warning` with `ok=true` — the run executed
and the train already shipped, so the exit code is `0`. Human output prints the
same push/verify facts next to each affected job. Expected exceptions are emitted
as `{"ok": false, "error": {"code", "message", "retryable"}}`.
