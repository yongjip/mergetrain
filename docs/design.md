# Design & architecture

mergetrain is a local-agent / worktree / deploy-branch-first deploy train. It serializes the committed branches that AI coding agents produce — each in its own Git worktree — through one queue, one runner, a Git merge train, configurable gates, atomic pushes, and an optional auto-only daemon.

This document describes the model and how the pieces fit together. For task-oriented detail, see the sibling guides: [install](install.md), [quickstart](quickstart.md), [config reference](config.md), [CLI reference](cli.md), [daemon](daemon.md), [failure modes](failure-modes.md), [security](security.md), [agent contract](agent-contract.md), [adapter pattern](adapter-pattern.md), and [development](development.md).

## Core flow

1. A user or agent commits work on a task branch.
2. The branch is enqueued into a SQLite queue.
3. A single runner claims the runner lock.
4. The runner creates a temporary detached Git worktree from the configured integration ref.
5. The runner merges queued task branches in FIFO order (the *merge train*).
6. Pre-push gates run once over the integrated result.
7. In deploy mode, the verified `HEAD` is pushed atomically to the configured refs.
8. Post-push verify hooks run.
9. Each job is marked `validated`, `deployed`, `blocked`, or `failed`.
10. Temporary worktrees are removed unless `--keep-worktree` is set.

Agents never push deploy refs themselves; they enqueue and read JSON. One runner (or daemon) owns merge, test, push, and verify.

## Concepts

**Job** — one task branch in the queue. It records a human-readable `task` name, the `branch`, the originating `worktree_path`, a `status`, separate `push_status` and `verify_status` outcomes, the SHAs captured at enqueue (`base_sha`, `head_sha`), the integration result SHA the runner produces (`deploy_sha`), validation-train identity, timestamps, a `log_path`, a `note`, and the `auto_deploy` flag.

**Validated train** — the exact group of jobs that passed a validation run. Each
member stores the shared `train_id`, expected `train_size`, `validated_at`,
`validation_base_sha`, and `validation_sha`, plus its own
`validated_head_sha`. New validations also record the validation tree, gate
policy, environment, and ordered train identity hashes. This makes the approval
target machine-readable and lets the deploy runner reject partial or changed
trains.

**Runner lock** — a single `runner` row in the `locks` table with an owner,
last heartbeat, expiry, and unique lease token. Claimed jobs carry the same token, so a stale
runner cannot refresh the lease or overwrite results after ownership changes.

**Run event** — an append-only, structured progress record for claiming,
fetching, assembly, gates, readiness, push, verification, and terminal outcomes.
The local dashboard uses these records rather than parsing logs or guessing from
process output.

**Integration worktree** — a disposable, detached Git worktree created under `state.worktree_root`, named `{project.name}-mergetrain-{job_id}-{random8}`, starting from the integration ref. The runner merges here, so agents never check out or push the deploy branch.

**Gate** — a pre-push verification command (`gates` in config) run inside the integration worktree. A gate failure is a pre-push failure: nothing ships.

**Verify hook** — a post-push verification command (`deploy.verify` in config) run after the push to confirm the deploy is live.

**Auto job** — a job enqueued with `--auto` (`auto_deploy = 1`). It is the only kind the unattended daemon will process. `--auto` does not mean the agent decided to auto-deploy; it records that the caller already holds explicit unattended-deploy approval.

See the [config reference](config.md) for how gates, verify hooks, and worktree paths are configured.

## Data model

State lives in a single SQLite database, initialized on connect.

### `deploy_queue`

```sql
CREATE TABLE IF NOT EXISTS deploy_queue (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task          TEXT NOT NULL,
  branch        TEXT NOT NULL,
  worktree_path TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'queued',
  base_sha      TEXT NOT NULL DEFAULT '',
  head_sha      TEXT NOT NULL DEFAULT '',
  deploy_sha    TEXT NOT NULL DEFAULT '',
  requested_at  TEXT NOT NULL,
  started_at    TEXT NOT NULL DEFAULT '',
  finished_at   TEXT NOT NULL DEFAULT '',
  log_path      TEXT NOT NULL DEFAULT '',
  note          TEXT NOT NULL DEFAULT '',
  auto_deploy   INTEGER NOT NULL DEFAULT 0,
  train_id      TEXT NOT NULL DEFAULT '',
  train_size    INTEGER NOT NULL DEFAULT 0,
  validated_at  TEXT NOT NULL DEFAULT '',
  validation_base_sha TEXT NOT NULL DEFAULT '',
  validation_sha TEXT NOT NULL DEFAULT '',
  validated_head_sha TEXT NOT NULL DEFAULT '',
  validation_tree_sha TEXT NOT NULL DEFAULT '',
  validation_gate_policy_sha TEXT NOT NULL DEFAULT '',
  validation_environment_sha TEXT NOT NULL DEFAULT '',
  validation_train_sha TEXT NOT NULL DEFAULT '',
  reused_validation_sha TEXT NOT NULL DEFAULT '',
  claim_token   TEXT NOT NULL DEFAULT '',
  cancel_requested_at TEXT NOT NULL DEFAULT ''
);
```

### `locks`

```sql
CREATE TABLE IF NOT EXISTS locks (
  name          TEXT PRIMARY KEY,
  owner         TEXT NOT NULL,
  worktree_path TEXT NOT NULL DEFAULT '',
  head_sha      TEXT NOT NULL DEFAULT '',
  acquired_at   TEXT NOT NULL,
  heartbeat_at  TEXT NOT NULL DEFAULT '',
  expires_at    TEXT NOT NULL,
  token         TEXT NOT NULL DEFAULT ''
);
```

### `run_events`

```sql
CREATE TABLE IF NOT EXISTS run_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_token TEXT NOT NULL DEFAULT '',
  job_id      INTEGER,
  phase       TEXT NOT NULL,
  state       TEXT NOT NULL DEFAULT 'info',
  message     TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);
```

Lease tokens remain internal. `RunEvent.to_dict()` removes `claim_token`, as do
the public job and lock models. The browser payload also omits local worktree
and log paths and reduces the owner identity to `local:<pid>`.

### Connection policy

`connect()` creates the parent directory, sets the row factory to `sqlite3.Row`, and applies `PRAGMA busy_timeout = 5000` and `PRAGMA journal_mode = WAL`. Writes are wrapped in `BEGIN IMMEDIATE` transactions to take an early lock and reduce queue-state conflicts under concurrent writers. Schema upgrades run once per `PRAGMA user_version` in the same transaction; databases newer than the running binary fail closed.

## Job lifecycle

**Active states:** `queued`, `in_progress`, `blocked`, `failed`, `validated`.
**Terminal states:** `deployed`, `canceled`.

| State | Meaning |
|---|---|
| `queued` | Waiting to be processed. |
| `in_progress` | Claimed by a runner with a unique lease token. |
| `blocked` | Merge conflict or a policy situation needing human action. |
| `failed` | Command failure or unexpected error. |
| `validated` | A `--validate-only` run succeeded; the exact train remains deployable and nothing was pushed. |
| `deployed` | A `--deploy` push succeeded; inspect `verify_status` for the independent post-push outcome. |
| `canceled` | Cancelled by a user. |

A branch may only re-enter the queue once its previous job is terminal.

**All claims are atomic.** Lock acquisition, job selection, and the transition to
`in_progress` occur in one `BEGIN IMMEDIATE` transaction. Every selected row
receives the lock's unique claim token. A manual batch deploy claims the only
complete validated train and leaves newer queued jobs untouched; if multiple
validated trains exist, `--train-id` is required.

## Runner behavior

### Single job (`run-next`)

The runner creates the log directory and a unique integration worktree path, then: `git fetch <remote>` → `git worktree add --detach <path> <integration_ref>` → `git merge --no-edit <branch>` → clean-worktree check → record `deploy_sha` → `git diff --check` → run gates → (deploy mode) atomic push → (deploy mode) verify hooks → remove the temp worktree (unless `--keep-worktree`) → record final status. Subprocess output streams to the job log while a polling loop renews the lease, checks cancellation, and enforces `command_timeout_seconds`. Losing the token stops the process group and prevents stale state writes.

### Batch / merge train (`run-batch`)

During validation, the runner merges all queued jobs into one integration
worktree in order. A branch that conflicts is marked `blocked`, `git merge
--abort` is attempted, and the remaining jobs are still tried. Gates then run
**once** over the whole train. If a pre-push gate fails, the train is torn down
and each merged job is re-processed individually, so an offending job is
isolated. Successful jobs receive a shared train identity and validation SHA.

During a later validated deploy, the selected train is atomic: every current
task branch must still resolve to its recorded `validated_head_sha`, and the
stored member count and shared metadata must be complete. The runner starts
from the current integration ref, merges the recorded commits, and reruns all
gates. Integration movement is therefore safe, but an identity mismatch,
merge conflict, or gate failure blocks/fails the whole approved train rather
than shipping a subset. On success, every member is marked `deployed` with the
new shared `deploy_sha`.

Validated-gate reuse is an explicit optimization layered on top of that safe
default. Configuration (`deploy.reuse.enabled`) or the deploy command
(`--reuse-validated`) must authorize it. The runner checks the exact integration
base, current task heads, ordered train identity, validation commit and tree,
semantic gate/fingerprint policy, adapter-provided environment hash, and age.
Only the unchanged case restores and pushes the exact `validation_sha` while
skipping reusable gates; gates marked `always_rerun_on_deploy` still execute.
Any mismatch either falls back to the full path or fails closed according to
policy, and events explicitly say whether each gate was reused or run.
Schema v5 adds the reuse identity fields with empty defaults. Older validated
rows remain deployable through the full gate path but cannot be reused because
they do not claim an identity that was never recorded.

### Atomic push

Deploy mode pushes the verified `HEAD` to every ref in `git.push_refs` atomically:

```sh
git push --atomic <remote> HEAD:<ref1> HEAD:<ref2> ...
```

An explicitly empty `push_refs` value is rejected while loading config; only an
omitted field defaults to the integration branch.

`terminology.git_operation` may present this operation as `deploy`, `integrate`,
or `push` in human-facing surfaces. The aliases converge on this exact code
path. Machine state deliberately remains `deployed`/`deploy_sha`, so existing
databases and JSON consumers do not need migration. Completion proves the Git
ref update only; it does not by itself prove or authorize a provider release.

### Post-push verify policy

Because a push already updates the remote, a verify-hook failure after push does
**not** mark jobs `failed`. The jobs stay `deployed` with
`push_status=succeeded` and `verify_status=failed`. CLI results use
`result=warning`/`ok=false`, and the terminal completion event stays in warning
state so it cannot visually erase the unresolved verification result. A schema
v4 migration backfills legacy `deployed` rows as pushed and recognizes the
canonical warning-note prefix when available; otherwise historical verification
remains `not_run` because no stronger fact was persisted. See [failure
modes](failure-modes.md#post-push-verify-failure).

## Daemon model

The daemon is a foreground, auto-only worker. Each tick it checks for `queued` jobs with `auto_deploy = 1`; only if any exist does it claim them and run the batch. It never touches manual jobs, releases only the exact lease token it acquired, and finishes the current tick before exiting on `SIGINT`/`SIGTERM`. "Auto" is determined solely by the `auto_deploy` field, never by daemon judgment. Operational detail and supervisor recipes are in the [daemon guide](daemon.md).

## Agent CLI observability

The SQLite `run_events` table is the durable progress journal. `events --jsonl`
reads it with an exclusive integer event cursor and adds follow-only heartbeat
frames from the current runner lock. Persisted event IDs are resumable; heartbeat
frames are ephemeral because the lock already stores only the latest heartbeat.
The table retains the newest 5,000 events, so callers that reconnect beyond that
window start from the oldest retained row.

Job filtering reconstructs a run from the job's current claim token and tokens
on prior job-specific events. Shared batch events have no job ID but carry the
same internal token, so `events --job` can include fetching/gating while excluding
other jobs' individual merge/completion records. Tokens remain internal and are
removed from every CLI payload.

`inspect` combines the latest run events, job timestamps, runner lock heartbeat,
and persisted push/verify outcomes into one snapshot. It provides stable outcome
categories for jobs and trains instead of requiring callers to parse notes.
`logs` deliberately remains separate: the runner publishes the confined log path
when processing starts, and the command follows raw output only after an explicit
job-ID request. Structured events contain redacted command templates or safe
return-code summaries, never subprocess output.

Scoped followers terminate after draining events when all selected jobs validate
or deploy, fail/block, cancel, or lose the matching lease. Unscoped follow is an
operator-wide feed and continues until interrupted.

## Local dashboard

`mergetrain dashboard` runs a small Python standard-library HTTP server. The
bundled React UI reads `/api/snapshot` and subscribes to `/api/events`; the
latter is an SSE stream of complete snapshots, so reconnects do not require
client-side event reconciliation. A polling fallback preserves freshness when
SSE is unavailable.

The header reports browser connectivity (`CONNECTED`, `POLLING`, or
`DISCONNECTED`) independently from runner ownership (`ACTIVE` or `IDLE`). During
gates, the snapshot exposes structured gate position and a redacted command
template so the current-check panel and Activity timeline can explain what is
running instead of only repeating a log message.

The dashboard has no write endpoint, form, cancel, retry, validate, deploy, or
shell-execution control. It is single-repository and desktop-first in v0.1. The
default bind address is loopback, and non-loopback binding requires explicit
`--allow-remote` acknowledgement.

Structured events are capped at the newest 5,000 rows to keep observability
bounded without requiring a separate maintenance process.

## Safety & liveness

The runner lock records an owner (`{user}:{pid}` by default) and an expiry. Owner liveness is derived from the trailing PID:

- parse failure → `unknown`
- `os.kill(pid, 0)` succeeds → `alive`
- `ProcessLookupError` → `dead`
- `PermissionError` → `alive`
- other `OSError` → `unknown`

From that: a **dead** owner's lock is reclaimed immediately; an **alive** owner is never stolen, even past TTL; an **unknown** owner whose lock has not expired blocks progress; an unknown owner with an expired lock but an `in_progress` job is **not** auto-reclaimed (a human must inspect). If there is no lock but `in_progress` jobs remain, the next claim re-queues them with the note `re-queued by mergetrain (previous runner gone)`. Recovery commands are in [failure modes](failure-modes.md).

## Branch model

Each agent/session works on its own task branch (for example `agent/feature-a`). Before enqueue: changes committed, worktree clean, current branch matching `--branch`, and no other active job for the same branch. The runner merges task branches onto the integration ref in order:

```
origin/main
  + agent/a
  + agent/b
  + agent/c
  = deploy_sha
```

On success every merged job shares that `deploy_sha`. A conflicting branch becomes `blocked`; the fix is to rebase it against the integration branch, commit a clean result, and enqueue a **new** clean job — reusing a blocked job in place is deliberately not the model, because a fresh clean job is more predictable for an agent to drive.

## Design principles

mergetrain is built so an LLM agent can operate it reliably:

- **Non-interactive.** Every agent-facing command is non-interactive; ambiguous intent fails. A bare `run-batch` is rejected — `--validate-only` or `--deploy` is required.
- **JSON-first.** JSON mode returns structured success, partial failure, and error payloads; job failures return exit code `1` instead of `ok: true`.
- **Next safe action.** `doctor --json` emits a `next_action` so an agent does not have to infer one.
- **Explicit consent.** Deploy needs `--deploy`; validation needs `--validate-only`; unattended eligibility needs `--auto`; destructive cleanup needs `gc --apply`; branch deletion needs `gc --delete-branches`.

The full operating contract is in [agent-contract.md](agent-contract.md).

## Provider neutrality

The core ships no provider APIs for Kubernetes, AWS, Argo, Vercel, GitHub, GitLab, or any service-specific platform. Provider behavior is expressed through shell commands in `gates` and `deploy.verify`, or through a thin adapter outside the core package — see the [adapter pattern](adapter-pattern.md).

## Roadmap

`0.1.0` ships the core: SQLite-backed queue, PID-aware runner lock, Git worktree merge trains, configurable gates and atomic push refs, the auto-only daemon, and JSON-first `doctor`/`status`/`agent-contract`/`gc`. Candidate next steps:

- `mergetrain config validate` as a standalone preflight command (runtime loading already rejects blank refs, duplicate gate names, invalid queue timing, and empty `push_refs`).
- Observability follow-ups: `mergetrain logs <job_id>`, `mergetrain inspect <job_id> --json`, machine-readable failure categories, optional metrics export.
- Daemon operations: recommended log rotation, a stale-lock inspection command, and a health-check pattern.
- A protected-branch guard list and documented branch-naming conventions for `gc --delete-branches`.
- Packaging/release hardening: classifiers, a release workflow, and editable-install / old-pip fallbacks.
