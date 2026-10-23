# Run several coding agents on one laptop

This guide is for one person running several Claude Code or Codex sessions on
one laptop, with one mergetrain queue landing their work. It assumes the
repository is set up as in the [quickstart](quickstart.md): landed
configuration, real gates, and linked agent instructions. Every recipe below
ran in disposable repositories with a local bare remote using mergetrain
**3.7.0**; see [What was tested](#what-was-tested).

Install one mergetrain release for the daemon and for every session. Agent
shells can have a different `PATH` from yours: in the tested Codex run, a
login shell found an older installation first, and that release refused the
newer queue until Codex switched to the matching executable. Check
`mergetrain --version` in each agent's shell.

## Give each agent a linked worktree

The queue belongs to the control checkout, the checkout that holds the shared
Git directory. Its linked worktrees share the queue database, logs, and runner
lock, so an agent's successful `enqueue` is the handoff the runner sees. A
separate clone has a queue of its own: `enqueue` there succeeds, but the
control checkout's runner never sees the job.

On the test machine, Claude Code's worktrees, the Codex app's worktrees under
`~/.codex/worktrees/<id>/<repo>`, and the worktree that `codex exec --worktree`
creates were all linked worktrees of the local checkout. To check a task
worktree:

```sh
git -C /path/to/task-worktree rev-parse --path-format=absolute --git-common-dir
mergetrain --repo /path/to/task-worktree status --diagnose --json | jq -r .diagnostics.db
```

Both must name the control checkout: `/path/to/example-app/.git` and
`/path/to/example-app/.mergetrain/queue.sqlite`. Work from a clone, a cloud
session, or another machine reaches this queue only through a branch that has
a worktree in the control checkout.

`codex exec --worktree` starts on a detached `HEAD`, so the agent creates its
task branch before it commits. Branch creation, commits, and `enqueue` write
to the control checkout's `.git` and `.mergetrain/`, outside Codex's managed
worktree. In the tested `workspace-write` sandbox, with the control checkout
passed as `--add-dir`, the first `git switch -c` still failed with
`cannot lock ref`; Codex completed it through its configured approval flow.
Expect that request unless your Codex setup already grants those writes.

## Approve unattended landing explicitly

The default daemon lands only jobs enqueued with `--auto`. It leaves other jobs
queued for `validate` and `deploy`, or for `daemon --validate-only`. The flag is
hidden from `--help` on purpose: it records approval to validate, deploy, and
verify the named task without another prompt. A task agent uses it only after
an explicit, bounded instruction such as "QA, deploy, verify, and finish this
task":

```sh
mergetrain enqueue --task "render: cache frame sizes" --branch agent/render --auto
```

The approval is bound to the push destination and to the execution policy:
the gates, including their `paths`, the default command timeout, validation
reuse, and verify hooks. If the control checkout's policy changes before the
job lands, the daemon blocks it with `approval_execution_policy_changed` and
pushes nothing. `retry` under the changed policy creates a manual replacement;
once the approved policy is back, `retry` keeps the approval. To approve a new
policy, dismiss the blocked job and enqueue it again with `--auto`; see
[Deploy authorization changed](failure-modes.md#deploy-authorization-changed).

## Keep heavy gates affordable

Gates share the laptop's CPU with the agents' own builds and tests. This
configuration runs two quick checks side by side and keeps a heavy suite off
trains that cannot break it:

```yaml
version: 2

project:
  name: example-app

gate_parallelism:
  max_workers: 4

gates:
  - name: lint
    run: ruff check .
    parallel_group: quick
    workers: 1
  - name: unit
    run: python -B -m pytest -q -p no:cacheprovider tests/unit
    parallel_group: quick
    workers: 1
  - name: render
    run: python -B -m pytest -q -p no:cacheprovider -n 4 tests/render
    needs:
      - lint
      - unit
    workers: 4
    paths:
      - src/example_app/render/**
      - tests/render/**
      - tests/conftest.py
      - pyproject.toml
```

- `paths` limits `render` to the renderer, its tests, the shared pytest
  configuration, and `pyproject.toml`. A documentation-only train skipped it
  and landed in about a second. A renderer change ran it, and so did moving a
  file out of `src/example_app/render/` and a batch in which only one member
  touched the renderer. See [`gates[].paths`](config.md#gatespaths).
- `gate_parallelism.max_workers` caps the worker tokens that gates hold at
  once. `lint` and `unit` run together in the `quick` group. `render` starts
  only after both pass, and its `workers: 4` matches its own `-n 4`
  pytest-xdist pool, so it never overlaps another gate. Choose `max_workers`
  from the cores you can spare while agents keep working.
- A persistent validation workspace helps only `validate` runs. A deploying
  daemon assembles every `--auto` train in a fresh worktree, so its cache
  directories start empty each time. In the tested repository, a
  cache-building gate took 6.7 s cold and 0.7 s warm under `validate`, and
  6.7 s and 6.8 s on two daemon deploys. With an unattended daemon, scope
  heavy gates and rely on tool caches outside the worktree, such as the
  package manager's.

`mergetrain stats --json` reports each gate's median duration and how often a
scoped gate was skipped (`gates[].state_counts.skipped`).

## Keep the daemon running

Start the daemon from the control checkout and hold a sleep assertion for as
long as it runs:

```sh
cd /path/to/example-app
caffeinate -i mergetrain daemon --once
PYTHONUNBUFFERED=1 nohup mergetrain daemon --interval 15 >> .mergetrain/daemon.log 2>&1 &
echo $! > .mergetrain/daemon.pid
nohup caffeinate -i -w "$(cat .mergetrain/daemon.pid)" >/dev/null 2>&1 &
```

The first command runs one tick in the foreground. On a new queue it also
creates `.mergetrain/`, which mergetrain marks as ignored, so the log and PID
file stay out of `git status`. Do not create that directory yourself:
mergetrain then ignores only its database files, and the log, the PID file,
and the integration worktrees show up as untracked files.
`PYTHONUNBUFFERED=1` writes each log line as it happens. Without it, the tested
daemon's redirected output reached the log only when the daemon exited.

`caffeinate -i -w` holds macOS's idle-sleep assertion until the daemon exits;
`pmset -g assertions` lists it as `PreventUserIdleSystemSleep`. It prevents
idle sleep only. Closing the lid still puts a MacBook to sleep, unless it keeps
running closed with an external display and accessories connected.

### If the Mac sleeps anyway

Sleep pauses the daemon, its gates, and any push. The queue, each job's pinned
commits, and the push's write-ahead marker are on disk, so nothing is lost.
What you see after waking depends on what was running:

| Running when the Mac slept | After waking | What to do |
| --- | --- | --- |
| Nothing | The daemon's next tick runs | Nothing |
| Gates | `status` reports `running` until the train lands | Nothing |
| Gates, for longer than `queue.lock_ttl_minutes` (30 by default) | `status` recommends `unlock_wedged_runner` until the daemon's next heartbeat renews its lease; heartbeats come every `queue.heartbeat_interval_seconds` (10 by default) | Read `status` again after a heartbeat; do not run `unlock --force` while the daemon process is running |
| A push whose connection dropped | `status` reports `attention` and recommends `mergetrain reconcile --apply`; the daemon pauses deploys meanwhile | Run `mergetrain reconcile --apply`; the daemon then runs the job's gates again and lands it |

While the lease is expired, other runners are refused with `lock_held`, so a
`validate` started right after waking cannot take over the train. To check
whether the Mac slept during a long run, read its power log:

```sh
pmset -g log | grep -E ' (Sleep|Wake|DarkWake) {2,}'
```

## Pause landing for a quiet period

A timing benchmark needs the CPU to itself. Stop the daemon first and start it
again afterwards with the commands above:

```sh
kill -TERM "$(cat .mergetrain/daemon.pid)"
while kill -0 "$(cat .mergetrain/daemon.pid)" 2>/dev/null; do sleep 1; done
mergetrain status --json
```

SIGTERM lets the daemon finish its current tick, including gates, push, and
verification, before it exits; `caffeinate` exits with it. In the tested run
the daemon exited 10 s after the signal, once its running gate had passed and
the job had landed. Do not use `kill -9`: a killed runner leaves its train for
recovery.

While the daemon is stopped, agents keep enqueueing and their jobs wait:
`status` reports `waiting` with `next_action.code` `run_daemon_when_approved`.
`validate` and `daemon --validate-only` run gates too, so leave them off until
the benchmark ends. A [Hub daemon](hub.md) runs gates for every registered
repository.

## Guard against accidental pushes

An agent with shell access and your push credential can update the integration
branch directly and bypass the queue. On a laptop where agents run as you, a
`pre-push` hook in the control checkout catches that mistake:

```sh
#!/bin/sh
# Refuse direct updates of the integration branch. A mergetrain deploy carries
# refs/mergetrain/deploys/<sha> for the same commit in the same push.
protected="refs/heads/main"
input=$(cat)
printf '%s\n' "$input" | while read -r _ sha ref _; do
  case " $protected " in *" $ref "*) ;; *) continue ;; esac
  printf '%s\n' "$input" | grep -q " refs/mergetrain/deploys/$sha " && continue
  echo "pre-push: $ref is updated by mergetrain; enqueue your branch instead" >&2
  exit 1
done
```

Save it as `pre-push` in the directory that
`git rev-parse --path-format=absolute --git-path hooks` prints in the control
checkout, and make it executable. Linked worktrees share that directory. List
every ref from `git.push_refs` in `protected`, separated by spaces, for
example `"refs/heads/develop refs/heads/main"`.

mergetrain's atomic deploy push always carries the audit ref for the commit it
lands, so it passes. In the tested repository the hook refused a direct push
to `main`, a deletion of `main`, and a push that paired `main` with an audit
ref for another commit. Task-branch pushes and mergetrain deploys passed.

The hook is an accident guard, not an enforcement boundary:

- `git push --no-verify` skips it;
- a separate clone, another machine, and changes made through the forge's web
  UI or API never run it;
- an agent with shell access can change or delete it;
- with `core.hooksPath` set, for example by a hook manager, Git runs hooks from
  that directory instead; add the check to the hook it already runs.

Only credential separation and remote branch protection enforce one runner;
see the [security boundary](security.md#runner-and-task-agent-credentials).

## Report a job's outcome from an orchestrating session

The task agent's rules stay the same: it enqueues its branch, reports the job
ID and pinned commit, and stops. `enqueue --json` returns the ID as `job.id`.
When a session must tell the user whether the work landed, for example the
session that started several task sessions, that orchestrating session waits
with read-only calls:

```sh
mergetrain --repo /path/to/example-app inspect 12 --json \
  | jq -r '.job | "\(.status) \(.push_status) \(.verify_status)"'
```

`--repo` names the control checkout, so the orchestrator can run from any
directory. Repeat every 30 to 60 seconds and stop at the first matching row:

| `job.status` | `job.verify_status` | Report |
| --- | --- | --- |
| `deployed` | `succeeded` or `not_configured` | Landed |
| `deployed` | `failed` or `unknown` | Landed; post-push verification needs the operator, and this is not a failed submission |
| `failed` or `blocked` | any | Needs a fix; include `outcome.message` and `retry_command` |
| `validated` | any | Ready; waiting for deploy approval |
| `needs_reconcile` | any | Push outcome unknown; the operator reconciles |
| `canceled` | any | Canceled or replaced |

Keep waiting on any other value. A job stays `in_progress`, with `push_status`
`pending`, through its push and post-push verification; `deployed` appears
with both results. These calls start no runner and grant no authority. They
worked while the daemon held the runner lock.

The agent that owns the branch fixes a failed or blocked job: it commits the
fix in its worktree and runs the job's `retry_command`. The orchestrator relays
the failure; the runner never edits code. `retry` replaces the job, so follow
the new ID: `retry --json` returns it as `job.id`, and it is the newest
`recent_jobs` row for that branch in `status --json`. A verification failure
has already landed and is not retried; `status` points the operator to
`verify --job`.

## When AGENTS.md ships to users

Some projects package `AGENTS.md` for their users' agents, for example as a
guide inside the wheel. Linking `AGENTS.mergetrain.md` from that file would send
users' agents to a queue they do not have. Keep the shipped file free of queue
rules and reach contributors through files that do not ship:

- Link `AGENTS.mergetrain.md` from contributor documentation such as
  `CONTRIBUTING.md`.
- For Claude Code, import the Claude sidecar from a `CLAUDE.md` that is not
  packaged:

  ```text
  @AGENTS.md
  @CLAUDE.mergetrain.md
  ```

- Codex reads `AGENTS.md`, not `CONTRIBUTING.md`. Give the shipped file one
  section that sends contributors on. It does not apply to users' agents,
  which do not change your repository:

  ```markdown
  ## Changing this repository

  This section is only for agents that change example-app itself: follow
  [CONTRIBUTING.md](CONTRIBUTING.md) before you commit or hand off work.
  ```

Check a built package: it must contain neither `AGENTS.mergetrain.md` nor
`CLAUDE.mergetrain.md`, and the shipped `AGENTS.md` must not mention
mergetrain. A [pre-push guard](#guard-against-accidental-pushes) catches a
contributor agent that missed the pointer and pushes directly.

## What was tested

On **2026-10-23**, a script ran each recipe's commands through `/bin/sh`, each
in its own disposable repository with a local bare remote. It used mergetrain
**3.7.0** from PyPI in a fresh virtual environment with Python 3.12.14, ruff
0.17.2, pytest 9.1.1, and pytest-xdist 3.8.0, on macOS 27.0.1 with Apple Git
2.54.0. The laptop ran on battery, and its power log showed no sleep during the
runs.

| Recipe | Result |
| --- | --- |
| Linked worktree | A worktree outside the checkout shared its Git directory and queue; the control daemon landed its job. A clone's job stayed in the clone's own queue and never landed. |
| `--auto` | Hidden from `enqueue --help`; the default daemon landed the auto job and left a manual one queued. A gate-command change and a `paths`-only change each blocked an auto job before push. |
| Heavy gates | `render` was skipped for documentation and ran for a renderer change, a rename out of its paths, and a mixed batch. `lint` and `unit` started together; `render` started after both passed. |
| Persistent workspace | `validate` 6.7 s cold, 0.7 s warm; two daemon deploys 6.7 s and 6.8 s. |
| Daemon start | One foreground tick created the ignored `.mergetrain/`; the control checkout stayed clean with the log and PID file. Without `PYTHONUNBUFFERED=1` the log stayed empty until the daemon exited. `caffeinate` held its assertion and exited with the daemon. |
| Sleep | Daemon and gate suspended for 76 s, past a 60 s lock TTL: `status` offered `mergetrain unlock --force` (`unlock_wedged_runner`), a competing `validate` got `lock_held`, and after resume the offer was gone within a second and the train landed. A push cut off at the remote parked in `needs_reconcile`; `reconcile --apply` requeued it and the daemon landed it. |
| Quiet period | SIGTERM during a 10 s gate: the job landed, then the daemon exited. A job enqueued while it was stopped waited, and landed after restart. |
| Push guard | Refused direct, deletion, and mismatched-audit-ref pushes; passed branch pushes and a mergetrain deploy. `--no-verify` and a separate clone bypassed it. |
| Outcome report | Polling from another directory reported a landing, a failure with `retry_command`, the retried replacement's landing, a landing with failed verification and no `retry_command`, and a manual job at Ready. |
| Shipped `AGENTS.md` | The wheel contained `toolkit/guide/AGENTS.md` without queue rules and no sidecar. |

Real sessions then worked in a package with that layout and the push guard.
Neither prompt mentioned mergetrain:

| Client | Worktree | Handoff |
| --- | --- | --- |
| Claude Code 2.1.287 | linked worktree made by the operator | with the sidecar imported through `CLAUDE.md`, read `CONTRIBUTING.md` and `AGENTS.mergetrain.md`, committed, enqueued job 1, stopped |
| Codex 0.162.1 | `codex exec --worktree`, detached `HEAD` | followed the shipped pointer to `AGENTS.mergetrain.md`, created a branch, committed, enqueued job 2 |

Both jobs were manual, `daemon --validate-only --once` left both Ready, and no
session pushed. See the [verification summary](evidence/laptop-agents-2026-10-23.json).

Sleep was simulated, not exercised: SIGSTOP and SIGCONT suspended the daemon
and its gate processes, and killing the remote's `receive-pack` cut off the
push. The lease renewed as soon as the processes resumed because the clock that
schedules heartbeats kept running; after a real sleep, expect up to one
heartbeat interval. A real lid close, a network remote, Linux, and Windows were
not exercised; `caffeinate` and `pmset` are macOS tools.
