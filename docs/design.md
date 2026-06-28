# Design & architecture

trainyard is a local-agent / worktree / deploy-branch-first deploy train. It serializes the committed branches that AI coding agents produce — each in its own Git worktree — through one queue, one runner, a Git merge train, configurable gates, atomic pushes, and an optional auto-only daemon.

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

**Job** — one task branch in the queue. It records a human-readable `task` name, the `branch`, the originating `worktree_path`, a `status`, the SHAs captured at enqueue (`base_sha`, `head_sha`), the integration result SHA the runner produces (`deploy_sha`), timestamps, a `log_path`, a `note`, and the `auto_deploy` flag.

**Runner lock** — a single `runner` row in the `locks` table that guarantees exactly one runner processes the queue at a time. Liveness is derived from the owner's PID, so a dead runner is reclaimed while a live one is never stolen (see [Safety & liveness](#safety--liveness)).

**Integration worktree** — a disposable, detached Git worktree created under `state.worktree_root`, named `{project.name}-trainyard-{job_id}-{random8}`, starting from the integration ref. The runner merges here, so agents never check out or push the deploy branch.

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
  auto_deploy   INTEGER NOT NULL DEFAULT 0
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
  expires_at    TEXT NOT NULL
);
```

### Connection policy

`connect()` creates the parent directory, sets the row factory to `sqlite3.Row`, and applies `PRAGMA busy_timeout = 5000` and `PRAGMA journal_mode = WAL`. Writes are wrapped in `BEGIN IMMEDIATE` transactions to take an early lock and reduce queue-state conflicts under concurrent writers. For forward compatibility with older databases, connect attempts an additive `ALTER TABLE deploy_queue ADD COLUMN auto_deploy …` migration and ignores the `OperationalError` if the column already exists.

## Job lifecycle

**Active states:** `queued`, `in_progress`, `blocked`, `failed`.
**Terminal states:** `deployed`, `validated`, `canceled`.

| State | Meaning |
|---|---|
| `queued` | Waiting to be processed. |
| `in_progress` | Claimed by a single-job runner. |
| `blocked` | Merge conflict or a policy situation needing human action. |
| `failed` | Command failure or unexpected error. |
| `validated` | A `--validate-only` run succeeded; nothing was pushed. |
| `deployed` | A `--deploy` push succeeded (the note may carry a post-push verify warning). |
| `canceled` | Cancelled by a user. |

A branch may only re-enter the queue once its previous job is terminal.

**Claim semantics differ by mode.** A single-job claim (`run-next`) flips that job's row to `in_progress` at claim time. A batch claim (`run-batch`, the daemon) takes the runner lock and returns all queued jobs FIFO, but does **not** flip rows to `in_progress` up front; each row transitions to its terminal/blocked/failed state as processing produces a result. In other words, a batch claim means "this runner holds the whole queue," and row status reflects per-job outcomes.

## Runner behavior

### Single job (`run-next`)

The runner creates the log directory and a unique integration worktree path, then: `git fetch <remote>` → `git worktree add --detach <path> <integration_ref>` → `git merge --no-edit <branch>` → clean-worktree check → record `deploy_sha` → `git diff --check` → run gates → (deploy mode) atomic push → (deploy mode) verify hooks → remove the temp worktree (unless `--keep-worktree`) → record final status. A `CommandFailed` marks the job `failed`; a merge/domain problem marks it `blocked`; an unexpected exception marks it `failed`. The runner lock is always released.

### Batch / merge train (`run-batch`)

The runner merges all queued jobs into one integration worktree in order. A branch that conflicts is marked `blocked`, `git merge --abort` is attempted, and the remaining jobs are still tried. Gates then run **once** over the whole train. If a pre-push gate fails for the train, the train is torn down and each merged job is re-processed individually via the single-job path, so one offending job is isolated while the others can still succeed. On success, every merged job is marked with the same `deploy_sha`.

### Atomic push

Deploy mode pushes the verified `HEAD` to every ref in `git.push_refs` atomically:

```sh
git push --atomic <remote> HEAD:<ref1> HEAD:<ref2> ...
```

If `push_refs` is empty, deploy fails by design. See [config reference → git](config.md#git).

### Post-push verify policy

Because a push already updates the remote, a verify-hook failure after push does **not** mark jobs `failed`. The jobs stay `deployed` and the failure is recorded as a warning in the note. Marking them failed would make queue state disagree with the actual remote state. See [failure modes](failure-modes.md#post-push-verify-failure).

## Daemon model

The daemon is a foreground, auto-only worker. Each tick it checks for `queued` jobs with `auto_deploy = 1`; only if any exist does it claim them and run the batch. It never touches manual jobs, catches and logs tick exceptions (attempting an owner-guarded lock release), and finishes the current tick before exiting on `SIGINT`/`SIGTERM`. "Auto" is determined solely by the `auto_deploy` field, never by daemon judgment. Operational detail and supervisor recipes are in the [daemon guide](daemon.md).

## Safety & liveness

The runner lock records an owner (`{user}:{pid}` by default) and an expiry. Owner liveness is derived from the trailing PID:

- parse failure → `unknown`
- `os.kill(pid, 0)` succeeds → `alive`
- `ProcessLookupError` → `dead`
- `PermissionError` → `alive`
- other `OSError` → `unknown`

From that: a **dead** owner's lock is reclaimed immediately; an **alive** owner is never stolen, even past TTL; an **unknown** owner whose lock has not expired blocks progress; an unknown owner with an expired lock but an `in_progress` job is **not** auto-reclaimed (a human must inspect). If there is no lock but `in_progress` jobs remain, the next claim re-queues them with the note `re-queued by trainyard (previous runner gone)`. Recovery commands are in [failure modes](failure-modes.md).

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

trainyard is built so an LLM agent can operate it reliably:

- **Non-interactive.** Every agent-facing command is non-interactive; ambiguous intent fails. A bare `run-batch` is rejected — `--validate-only` or `--deploy` is required.
- **JSON-first.** `doctor`, `status`, `agent-contract`, and `gc` all emit machine-readable JSON for deciding the next step.
- **Next safe action.** `doctor --json` emits a `next_action` so an agent does not have to infer one.
- **Explicit consent.** Deploy needs `--deploy`; validation needs `--validate-only`; unattended eligibility needs `--auto`; destructive cleanup needs `gc --apply`; branch deletion needs `gc --delete-branches`.

The full operating contract is in [agent-contract.md](agent-contract.md).

## Provider neutrality

The core ships no provider APIs for Kubernetes, AWS, Argo, Vercel, GitHub, GitLab, or any service-specific platform. Provider behavior is expressed through shell commands in `gates` and `deploy.verify`, or through a thin adapter outside the core package — see the [adapter pattern](adapter-pattern.md).

## Roadmap

`0.1.0` ships the core: SQLite-backed queue, PID-aware runner lock, Git worktree merge trains, configurable gates and atomic push refs, the auto-only daemon, and JSON-first `doctor`/`status`/`agent-contract`/`gc`. Candidate next steps:

- `trainyard config validate`, plus clearer errors for a missing remote/integration ref, gate-name uniqueness, and an empty-`push_refs` warning surfaced in `doctor`.
- Observability: `trainyard logs <job_id>`, `trainyard inspect <job_id> --json`, machine-readable failure categories, optional metrics export.
- Daemon operations: recommended log rotation, a stale-lock inspection command, and a health-check pattern.
- A protected-branch guard list and documented branch-naming conventions for `gc --delete-branches`.
- Packaging/release hardening: classifiers, a release workflow, and editable-install / old-pip fallbacks.
