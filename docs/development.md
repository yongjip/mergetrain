# Development

How to work on mergetrain itself. For using mergetrain in a repo, start with [quickstart](quickstart.md).

## Project layout

```text
mergetrain/
  src/mergetrain/
    __init__.py        # version
    __main__.py        # python -m mergetrain
    cli.py             # argument parsing and top-level dispatch
    cli_support.py     # shared JSON/config/rendering helpers
    commands/          # command-domain execution modules
      setup.py         # init, contract, version, demo, MCP
      queue.py         # enqueue, retry, supersede, dismiss, cancel
      inspection.py    # status, events, history, stats, logs, doctor
      deploy.py        # validation/deploy execution
      daemon.py        # single-repository auto-only daemon command
      recovery.py      # reconcile, recover, unlock, verify, cleanup
      hub.py           # dashboard and multi-repository hub commands
    config.py          # safe YAML loading + typed policy validation
    daemon.py          # auto-only daemon loop
    evidence.py        # evidence-backed outcome, validation, and batching metrics
    errors.py          # MergetrainError hierarchy
    command_runner.py  # managed subprocesses, POSIX shell, environment
    gate_runner.py     # gate scheduling, paths, fingerprints, verify hooks
    git_ops.py         # narrow Git/ref queries and worktree garbage collection
    git_runner.py      # queue-aware single/batch train orchestration
    atomic_push.py     # durable marker, atomic push, post-push verification
    validation_reuse.py # validated-train identity and reuse decisions
    worktree_manager.py # ephemeral/persistent integration worktrees
    observability.py   # job/train outcomes and event/heartbeat read models
    dashboard.py       # stdlib read-only HTTP/SSE server
    snapshot.py        # privacy-conscious dashboard read model
    dashboard_dist/    # packaged production dashboard assets
    models.py          # Job, RunnerLock, and RunEvent dataclasses
    path_gates.py      # POSIX glob matching and NUL-safe Git diff parsing
    store.py           # stable compatibility façade for persistence APIs
    persistence/
      transactions.py  # BEGIN IMMEDIATE and time helpers
      connection.py    # writable/read-only SQLite connection policy
      schema.py        # schema definition and ordered migrations
      jobs.py          # queue/job reads, mutations, validated trains
      leases.py        # liveness, token-fenced locks, orphan recovery
      claims.py        # atomic job+lease+event claim transactions
      events.py        # append-only event storage and scoped reads
      recovery.py      # durable push markers and reconcile guards
  dashboard/           # React/Vite dashboard source
  docs/                # this documentation set
  examples/            # example .mergetrain.yaml and agent metadata
  integrations/        # provider-neutral adapters and the Claude Code plugin
  tests/               # unittest suite
  pyproject.toml
  AGENTS.md  CHANGELOG.md  LICENSE  README.md
  llms.txt  llms-full.txt
```

mergetrain uses a `src/` layout and requires PyYAML for safe, consistent
`.mergetrain.yaml` parsing on every supported platform. The parser output is
then validated into typed runtime policy by `config.py`.

`cli.py` owns only parsing, handler selection, and the top-level error envelope.
Command modules own presentation and command orchestration for one domain; they
call runner, persistence, recovery, and observability APIs rather than carrying
business rules of their own. Shared CLI helpers stay narrow so command modules
do not import each other.

## Running tests

mergetrain requires **Python 3.10+** (it uses `dataclass(slots=True)` and other
3.10+ features). On macOS the built-in `/usr/bin/python3` is 3.9 and fails fast
with `TypeError: dataclass() got an unexpected keyword argument 'slots'` — reach
for an explicit newer interpreter (`python3.12`), a virtualenv, or pyenv. This
repo pins `.python-version` to a 3.12 build so bare `python` resolves correctly
under pyenv shims; note that a system `python3` earlier on your `PATH` can still
shadow it, so prefer `python` or a versioned `python3.12` when in doubt.

The suite remains compatible with plain `unittest`. This is the
dependency-light contributor fallback; with the `src/` layout, put the package
on the path:

```sh
PYTHONPATH=src python -m unittest discover -s tests
```

Or install editable and run without `PYTHONPATH`:

```sh
python -m pip install -e .
python -m unittest discover -s tests
```

Pytest is configured with `pythonpath = ["src"]`, so the blocking CI and local
deploy-gate command always imports the current checkout even if the selected
interpreter also has an older mergetrain installed. Install the dev extra first;
it supplies pytest-xdist, Ruff, and mypy:

```sh
python -m pip install -e ".[dev]"
python -m pytest -q -n auto --cov=mergetrain --cov-report=term-missing --cov-report=json:.coverage.json
python scripts/check_critical_coverage.py .coverage.json
```

Some tests intentionally bind localhost sockets or inspect child processes. If
a restricted sandbox reports `PermissionError` for socket binding or `ps`,
rerun the full suite once outside that sandbox; repeating the same sandboxed
suite cannot add evidence. Use focused tests inside the sandbox before that
single full run.

## Testing strategy

The suite covers the behaviors that make the queue safe:

- **persistence** — atomic token-fenced claims; stale-owner rejection; cooperative whole-train cancellation; validated-train identity; resumable/scoped events; orphan recovery; and versioned legacy-DB migrations.
- **daemon** — `--once` processes only auto jobs and leaves manual jobs queued; repeated DB connections do not leak file descriptors; a tick exception releases the lock and leaves the job queued.
- **git_runner** — managed subprocess heartbeats, timeout/process-group cleanup, cooperative cancellation, atomic refs, exact validation identity, integration movement, and failure isolation.
- **cli** — structured JSON errors and result counts, truthful exit codes, agent contract, validated-train status, resumable JSONL events, inspect/log follow termination, `doctor` next actions, global option normalization, dashboard bind policy, and init output.
- **dashboard** — privacy-conscious snapshots, security headers, packaged static assets, and path-traversal rejection.
- **config** — safe YAML loading, ambiguous-scalar rejection, fail-closed deploy
  refs, positive queue timing, unique gate names, defaults, and path resolution.

When adding behavior, add or extend the matching `tests/test_*.py` module.

### Coverage and typing ratchets

Coverage is a regression guard, not a target to inflate with trivial tests.
The blocking pytest command enforces the measured cross-platform baseline of
87% overall coverage through `pyproject.toml`. It also writes `.coverage.json`
and runs `scripts/check_critical_coverage.py`, which applies higher floors to
correctness-critical modules:

| Module | Minimum |
| --- | ---: |
| `atomic_push.py` | 90% |
| `command_runner.py` | 85% |
| `gate_runner.py` | 94% |
| `git_ops.py` | 85% |
| `git_runner.py` | 88% |
| `persistence/claims.py` | 90% |
| `persistence/connection.py` | 87% |
| `persistence/events.py` | 94% |
| `persistence/jobs.py` | 88% |
| `persistence/leases.py` | 82% |
| `persistence/recovery.py` | 86% |
| `persistence/schema.py` | 95% |
| `persistence/transactions.py` | 90% |
| `recovery.py` | 91% |
| `reuse.py` | 94% |
| `validation_reuse.py` | 83% |
| `worktree_manager.py` | 91% |

The original floors were chosen from the same successful Linux, macOS, and
Windows CI matrix. The collaborator floors preserve the exercised behavior
after the GitRunner and persistence responsibility splits, with conservative
headroom for platform-specific branches; the blocking matrix confirms them
before merge.
Raise them when durable state-transition coverage improves. Lower them only
with a documented explanation of which behavior moved or became unreachable.
A missing critical module in coverage JSON fails closed rather than silently
disappearing from the policy.

Mypy remains incremental for peripheral adapters and CLI rendering. The train
coordinator, its six correctness-critical collaborators, the persistence
package, and the existing recovery/reuse modules additionally reject untyped
definitions, check function bodies, reject implicit optionals, and warn on
`Any` returns. This
keeps the stricter boundary focused on lease fencing, process control,
worktrees, recovery, validation identity/reuse, and atomic push state rather
than forcing a repository-wide annotation rewrite.

The current baseline still leaves several high-value state transitions without
direct execution evidence. Prefer these over tests that merely increase the
percentage:

- successful automatic clearing and audit of a dead-owner lock, including the
  race where that lock is replaced during the check;
- cancellation or failure of the deploy audit-ref preflight before the durable
  pending-push marker exists;
- validated-train reassembly that encounters a merge conflict or unexpectedly
  leaves the integration worktree dirty; and
- the row-count race where an active train changes while cancellation is being
  recorded.

### The fault matrix

`tests/test_fault_*.py` inject the failures that decide whether mergetrain tells
the truth about what shipped, against real git and a real bare remote:

| File | Injects |
| --- | --- |
| `test_fault_push_kill.py` | a real `git push --atomic` SIGKILLed mid-flight, once with the refs applied (`post-receive` hook) and once not (`pre-receive`), as one decision table |
| `test_fault_push_timeout.py` | a push exceeding `command_timeout_seconds`, both hook variants — the likeliest real ambiguous push, and the one that also runs on Windows |
| `test_fault_reconcile_ancestry.py` | a remote tip that moved *on top of* a landed deploy, a legacy no-audit rewind, and an audited deploy rewritten sideways that must block |
| `test_fault_lock_steal.py` | `unlock --force` stealing the lease between a landed push and its `deployed` write |
| `test_fault_db_contention.py` | a SQLite writer held past `busy_timeout` across the pre-push and post-push status writes |

Two of these tests are `@unittest.expectedFailure`, each with a comment naming an
open defect and the file:line that causes it. That is deliberate: a recorded
defect that keeps the suite green is better than a deleted case.

Run the suite in parallel — every case builds its own repo and bare remote, so the
work is I/O-bound on git and parallelism is nearly free:

```sh
python -m pytest -q -n auto
```

That is ~30s wall time versus ~160s serial on a 10-core machine, which is what
makes fault injection cheap enough to run on every push. CI uses `-n auto`.

The remaining non-enumerable evidence is the
[real-remote soak](soak.md): repeated released-wheel use on a confirmed
throwaway GitHub repository, classified operator interventions, and one
deliberate crash whose queue verdict is compared with the real remote.

## Dashboard authoring

The published wheel does not need Node at runtime; it serves committed assets
from `src/mergetrain/dashboard_dist`. Node is only needed when editing the UI:

```sh
cd dashboard
npm ci
npx --no-install playwright install chromium
npm test
npm run test:browser
npm run build
```

Commit both the source and rebuilt `dashboard_dist` output. The UI uses bundled
fonts and icons and makes no external runtime requests.

The CI `dashboard` job runs the same checks (installing Chromium with its Linux
system dependencies) and then fails if
`src/mergetrain/dashboard_dist` came out different from what is committed, so a
UI change that forgets the rebuild cannot ship. That check needs the build to be
reproducible: use `npm ci` (not `npm install`, which can move dependencies off
the lockfile) and the Node major pinned in `dashboard/.nvmrc`.

## Claude Code plugin authoring

The self-marketplace manifest lives at `.claude-plugin/marketplace.json`; the
plugin itself lives under `integrations/claude/plugin`. Validate both surfaces
with the same strict CLI checks used by CI:

```sh
claude plugin validate integrations/claude/plugin --strict
claude plugin validate . --strict
python scripts/check_agent_protocol.py
```

The generated block in `CLAUDE.md` and the plugin's operating skill come
from the CLI's `render_agent_contract()` output. After intentionally changing
that contract, run `python scripts/check_agent_protocol.py --write` and review
the result. The checker also requires the skill reference tables to enumerate every
implemented `next_action` and MCP-local error code.

## Packaging

The build backend is `hatchling`; the wheel packages `src/mergetrain` and exposes the `mergetrain` console script.

```sh
python -m build
python -m pip install dist/*.whl
mergetrain --version
```

Supported and tested Python: 3.10 through 3.14. See the
[release checklist](release.md) for the full publish flow.

## Dogfooding: mergetrain deploys mergetrain

The repository commits its own `.mergetrain.yaml`, so `doctor` reports real
configuration instead of running on defaults, and a train through this repo runs
the checks CI runs:

| Gate | Covers |
| --- | --- |
| `diff-check` | whitespace errors against the integration ref |
| `ruff`, `mypy` | the blocking `lint` CI job |
| `tests` | the full unit suite on Python 3.12, parallelized with pytest-xdist |

These gates require the `dev` extra. Invoke mergetrain from the same reviewed
virtualenv so its `bin` directory supplies `ruff`, `mypy`, `pytest`, and the
`python3.12` launcher. Plain `unittest` discovery remains available as a
dependency-light contributor command, but it is intentionally not the deploy
gate: serial execution turns the current suite into a roughly five-minute
delay, while the CI-shaped parallel run completes in about one minute.

The repository's gate plan also exercises the resource scheduler shipped in
1.2: `ruff` and `mypy` share the `quality` parallel group, while `tests`
explicitly depends on both and consumes the complete four-token budget. This
shortens the independent lint phase without overlapping pytest-xdist's
CPU-heavy worker pool. The built-in integrity check still finishes first.

One machine cannot reproduce the whole matrix, so the CI legs it cannot run
(Windows, Python 3.10-3.14, `e2e`, `package`) are covered *after* the push by
the `github-ci` verify hook — [`scripts/verify-ci.sh`](../scripts/verify-ci.sh)
waits for the `ci.yml` run on the pushed SHA. Tunables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `MERGETRAIN_CI_WAIT_SECONDS` | `900` | how long to wait for a conclusion |
| `MERGETRAIN_CI_POLL_SECONDS` | `15` | interval between polls |
| `MERGETRAIN_CI_WORKFLOW` | `ci.yml` | workflow to watch |

A verify failure records a post-push verify warning on the already-deployed job
(`verify_status: failed`); it cannot un-land a push, and it is deliberately not
reported as a failed deploy. The hook skips when `gh` is missing or has no
credentials, because an unavailable CLI is not evidence about the commit, and it
treats "still running" as needing attention rather than as a pass.

## Conventions

- Keep the core provider-neutral. Service-specific deploy logic belongs in `gates`/`deploy.verify` config or an [adapter](adapter-pattern.md), never in the core package.
- Any new shell execution path (gates, verify hooks, subprocess calls) must be documented; see [security](security.md).
- Never put provider credentials in examples or tests.
