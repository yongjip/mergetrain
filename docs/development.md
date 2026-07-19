# Development

How to work on mergetrain itself. For using mergetrain in a repo, start with [quickstart](quickstart.md).

## Project layout

```text
mergetrain/
  src/mergetrain/
    __init__.py        # version
    __main__.py        # python -m mergetrain
    cli.py             # argument parsing and command handlers
    config.py          # .mergetrain.yaml loading (+ built-in YAML subset parser)
    daemon.py          # auto-only daemon loop
    errors.py          # MergetrainError hierarchy
    git_runner.py      # worktree merge train, gates, atomic push, verify, gc
    observability.py   # job/train outcomes and event/heartbeat read models
    dashboard.py       # stdlib read-only HTTP/SSE server
    snapshot.py        # privacy-conscious dashboard read model
    dashboard_dist/    # packaged production dashboard assets
    models.py          # Job, RunnerLock, and RunEvent dataclasses
    store.py           # SQLite schema, queue ops, runner lock, events
  dashboard/           # React/Vite dashboard source
  docs/                # this documentation set
  examples/            # example .mergetrain.yaml and agent metadata
  integrations/        # thin service wrapper examples
  tests/               # unittest suite
  pyproject.toml
  AGENTS.md  CHANGELOG.md  LICENSE  README.md
  llms.txt  llms-full.txt
```

mergetrain uses a `src/` layout and has **zero required runtime dependencies**. PyYAML is optional; when it is absent, `config.py` falls back to a small YAML-subset parser that understands the generated config shape.

## Running tests

mergetrain requires **Python 3.10+** (it uses `dataclass(slots=True)` and other
3.10+ features). On macOS the built-in `/usr/bin/python3` is 3.9 and fails fast
with `TypeError: dataclass() got an unexpected keyword argument 'slots'` — reach
for an explicit newer interpreter (`python3.12`), a virtualenv, or pyenv. This
repo pins `.python-version` to a 3.12 build so bare `python` resolves correctly
under pyenv shims; note that a system `python3` earlier on your `PATH` can still
shadow it, so prefer `python` or a versioned `python3.12` when in doubt.

The suite is plain `unittest`. With the `src/` layout, put the package on the path:

```sh
PYTHONPATH=src python -m unittest discover -s tests
```

Or install editable and run without `PYTHONPATH`:

```sh
python -m pip install -e .
python -m unittest discover -s tests
```

## Testing strategy

The suite covers the behaviors that make the queue safe:

- **store** — atomic token-fenced claims; stale-owner rejection; cooperative whole-train cancellation; validated-train identity; resumable/scoped events; orphan recovery; and versioned legacy-DB migrations.
- **daemon** — `--once` processes only auto jobs and leaves manual jobs queued; repeated DB connections do not leak file descriptors; a tick exception releases the lock and leaves the job queued.
- **git_runner** — managed subprocess heartbeats, timeout/process-group cleanup, cooperative cancellation, atomic refs, exact validation identity, integration movement, and failure isolation.
- **cli** — structured JSON errors and result counts, truthful exit codes, agent contract, validated-train status, resumable JSONL events, inspect/log follow termination, `doctor` next actions, global option normalization, dashboard bind policy, and init output.
- **dashboard** — privacy-conscious snapshots, security headers, packaged static assets, and path-traversal rejection.
- **config** — built-in YAML parsing, fail-closed deploy refs, positive queue timing, unique gate names, defaults, and path resolution.

When adding behavior, add or extend the matching `tests/test_*.py` module.

## Dashboard authoring

The published wheel does not need Node at runtime; it serves committed assets
from `src/mergetrain/dashboard_dist`. Node is only needed when editing the UI:

```sh
cd dashboard
npm install
npm run build
```

Commit both the source and rebuilt `dashboard_dist` output. The UI uses bundled
fonts and icons and makes no external runtime requests.

## Packaging

The build backend is `hatchling`; the wheel packages `src/mergetrain` and exposes the `mergetrain` console script.

```sh
python -m build
python -m pip install dist/*.whl
mergetrain --version
```

Supported and tested Python: 3.10 through 3.14. See the
[release checklist](release.md) for the full publish flow.

## Conventions

- Keep the core provider-neutral. Service-specific deploy logic belongs in `gates`/`deploy.verify` config or an [adapter](adapter-pattern.md), never in the core package.
- Any new shell execution path (gates, verify hooks, subprocess calls) must be documented; see [security](security.md).
- Never put provider credentials in examples or tests.
