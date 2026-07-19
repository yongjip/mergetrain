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

Command summary:

```text
mergetrain init [--project NAME] [--write] [--force]
mergetrain agent-contract [--json]
mergetrain enqueue --task TASK --branch BRANCH [options]
mergetrain status [--json] [--limit N]
mergetrain doctor [--json]
mergetrain dashboard [--host HOST] [--port PORT] [--allow-remote] [--preview]
mergetrain run-next  (--validate-only | --deploy) [--keep-worktree] [--json]
mergetrain run-batch (--validate-only | --deploy) [--train-id ID] [--keep-worktree] [--json]
mergetrain daemon [--interval SECONDS] [--once] [--keep-worktree]
mergetrain gc [--json] [--apply] [--delete-branches]
mergetrain cancel JOB_ID [--note NOTE] [--json]
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

## `status`

Print queue and lock state.

```sh
mergetrain status --json --limit 50
```

`--json` returns `ok`, `db`, `lock`, and `jobs`. `--limit` caps the job list (default 50).

## `doctor`

Diagnose config, queue, Git remote, integration ref, GC candidates, and the next safe action.

```sh
mergetrain doctor --json
```

Key JSON fields: `ok`, `version`, `config`, `config_exists`, `db`, `db_existed_before`, `state.logs`, `state.worktree_root`, `git.repo_root`, `git.current_branch`, `git.worktree_clean`, `git.remote_url`, `git.remote_exists`, `git.integration_ref`, `git.integration_ref_exists`, `lock`, `counts`, `validated_trains`, `gc.worktree_candidates`, and `next_action`.

`next_action` is one of:

- `wait_for_runner` — a live runner already holds the lock.
- `fix_blocked_job` — there are `blocked`/`failed` jobs to resolve first.
- `deploy_validated_train_when_approved` — an exact validated train is waiting for explicit deploy approval.
- `cancel_and_reenqueue_legacy_validated_jobs` — pre-migration validated jobs lack safe train identity.
- `run_daemon_or_run_batch_deploy_when_approved` — auto-approved jobs are queued.
- `run_batch_validate` — manual jobs are queued; validate them.
- `gc_available` — only cleanup remains.
- `enqueue_clean_branch` — the queue is empty.

`next_action` is **advisory**; it never substitutes for a destructive action or deploy consent.

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

## `run-next`

Process exactly one queued job. Requires `--validate-only` or `--deploy`.

```sh
mergetrain run-next --validate-only
mergetrain run-next --deploy
```

`--keep-worktree` leaves the temporary integration worktree in place for inspection. See [Design → Runner behavior](design.md#runner-behavior).
Validated jobs are deployed through `run-batch --deploy` so train identity is
preserved, including when the train contains only one job.

## `run-batch`

Validate all currently queued jobs as one merge train, or deploy an exact
validated train. Requires `--validate-only` or `--deploy`.

```sh
mergetrain run-batch --validate-only
mergetrain run-batch --deploy
mergetrain run-batch --deploy --train-id <id>
```

After validation, plain `--deploy` selects the only pending validated train and
leaves newer queued jobs untouched. If more than one train is pending, deployment
fails safely until `--train-id` selects one. The runner verifies every validated
task HEAD, rebuilds on the current integration ref, and reruns gates before push.
Changed task HEADs block the whole validated train. During initial validation,
conflicts still block only the offending job and a train gate failure still
isolates merged jobs one-by-one. See [Design → Batch](design.md#batch--merge-train-run-batch).

## `daemon`

Run an unattended, auto-only worker.

```sh
mergetrain daemon --interval 15
mergetrain daemon --once
```

Claims only jobs enqueued with `--auto`. See the [daemon guide](daemon.md).

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

## Exit codes

`0` all requested jobs succeeded · `1` a job blocked/failed, post-push verify
warning, or an expected config/queue/command error · `2` usage error · `130`
interrupted (`Ctrl-C`). In JSON mode, run results include `ok`, `result`
(`success`, `warning`, `partial`, or `failed`), per-status `counts`,
`push_counts`, `verify_counts`, and `jobs`. Each job exposes `push_status`
(`not_run`, `succeeded`, or `failed`) and `verify_status` (`not_run`,
`not_configured`, `succeeded`, or `failed`). A successful push followed by a
failed verify keeps `status=deployed` but returns `result=warning` and `ok=false`.
Human output prints the same push/verify facts next to each affected job.
Expected exceptions are emitted as
`{"ok": false, "error": {"code", "message", "retryable"}}`.
