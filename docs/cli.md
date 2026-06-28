# CLI reference

All commands share global options, which may appear **before or after** the subcommand (the parser normalizes their position, since agents often misplace them):

```sh
trainyard --config <path> --repo <repo> --db <sqlite> <command>
trainyard doctor --json --repo /path/to/repo      # equivalent to:
trainyard --repo /path/to/repo doctor --json
```

| Global option | Meaning |
|---|---|
| `--config` | Path to `.trainyard.yaml` (defaults to `<repo>/.trainyard.yaml`). |
| `--repo` | Repository root or worktree path (defaults to the current directory). |
| `--db` | Override the SQLite DB path from config. |
| `--version` | Print the version and exit. |

Command summary:

```text
trainyard init [--project NAME] [--write] [--force]
trainyard agent-contract [--json]
trainyard enqueue --task TASK --branch BRANCH [options]
trainyard status [--json] [--limit N]
trainyard doctor [--json]
trainyard run-next  (--validate-only | --deploy) [--keep-worktree] [--json]
trainyard run-batch (--validate-only | --deploy) [--keep-worktree] [--json]
trainyard daemon [--interval SECONDS] [--once] [--keep-worktree]
trainyard gc [--json] [--apply] [--delete-branches]
trainyard cancel JOB_ID [--note NOTE] [--json]
```

## `init`

Print or write starter config and agent instructions.

```sh
trainyard init --project demo            # print config to stdout
trainyard init --project demo --write    # write files into the repo
```

`--write` creates `.trainyard.yaml`, `AGENTS.trainyard.md`, and `CLAUDE.trainyard.md`. Existing files are not overwritten without `--force`. See [config reference](config.md).

## `agent-contract`

Print the short operating rules an agent must follow.

```sh
trainyard agent-contract
trainyard agent-contract --json
```

`--json` emits `name`, `purpose`, `rules`, and `boundary`. See [agent contract](agent-contract.md).

## `enqueue`

Add a task branch to the queue.

```sh
trainyard enqueue --task feature-a --branch agent/feature-a --capture-sha
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
trainyard status --json --limit 50
```

`--json` returns `ok`, `db`, `lock`, and `jobs`. `--limit` caps the job list (default 50).

## `doctor`

Diagnose config, queue, Git remote, integration ref, GC candidates, and the next safe action.

```sh
trainyard doctor --json
```

Key JSON fields: `ok`, `version`, `config`, `config_exists`, `db`, `db_existed_before`, `state.logs`, `state.worktree_root`, `git.repo_root`, `git.current_branch`, `git.worktree_clean`, `git.remote_url`, `git.remote_exists`, `git.integration_ref`, `git.integration_ref_exists`, `lock`, `counts`, `gc.worktree_candidates`, and `next_action`.

`next_action` is one of:

- `wait_for_runner` — a live runner already holds the lock.
- `fix_blocked_job` — there are `blocked`/`failed` jobs to resolve first.
- `run_daemon_or_run_batch_deploy_when_approved` — auto-approved jobs are queued.
- `run_batch_validate` — manual jobs are queued; validate them.
- `gc_available` — only cleanup remains.
- `enqueue_clean_branch` — the queue is empty.

`next_action` is **advisory**; it never substitutes for a destructive action or deploy consent.

## `run-next`

Process exactly one queued job. Requires `--validate-only` or `--deploy`.

```sh
trainyard run-next --validate-only
trainyard run-next --deploy
```

`--keep-worktree` leaves the temporary integration worktree in place for inspection. See [Design → Runner behavior](design.md#runner-behavior).

## `run-batch`

Process all currently queued jobs as one merge train. Requires `--validate-only` or `--deploy`.

```sh
trainyard run-batch --validate-only
trainyard run-batch --deploy
```

This is the primary command for shipping several agent commits in sequence. Conflicts block only the offending job; a train gate failure isolates merged jobs one-by-one. See [Design → Batch](design.md#batch--merge-train-run-batch).

## `daemon`

Run an unattended, auto-only worker.

```sh
trainyard daemon --interval 15
trainyard daemon --once
```

Claims only jobs enqueued with `--auto`. See the [daemon guide](daemon.md).

## `gc`

Clean up temporary worktrees and, optionally, terminal branches. Dry-run by default.

```sh
trainyard gc --json                              # dry run
trainyard gc --apply --json                      # remove temp worktrees
trainyard gc --apply --delete-branches --json    # also delete terminal branches
```

Branch deletion only targets branches of `deployed`/`validated`/`canceled` jobs and never deletes a protected ref (`push_refs`, the integration branch) or the currently checked-out branch.

## `cancel`

Cancel a non-terminal queue item. Terminal items cannot be cancelled.

```sh
trainyard cancel 12 --note "replaced by rebased branch"
```

## Exit codes

`0` success · `1` an expected error (config, queue, command failure) · `2` usage error (no subcommand) · `130` interrupted (`Ctrl-C`).
