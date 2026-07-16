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
    models.py          # Job and RunnerLock dataclasses, status sets
    store.py           # SQLite schema, queue ops, runner lock
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

- **store** — atomic token-fenced claims; stale-owner rejection; cooperative whole-train cancellation; validated-train identity; orphan recovery; and versioned legacy-DB migrations.
- **daemon** — `--once` processes only auto jobs and leaves manual jobs queued; repeated DB connections do not leak file descriptors; a tick exception releases the lock and leaves the job queued.
- **git_runner** — managed subprocess heartbeats, timeout/process-group cleanup, cooperative cancellation, atomic refs, exact validation identity, integration movement, and failure isolation.
- **cli** — structured JSON errors and result counts, truthful exit codes, agent contract, validated-train status, `doctor` next actions, global option normalization, and init output.
- **config** — built-in YAML parsing, fail-closed deploy refs, positive queue timing, unique gate names, defaults, and path resolution.

When adding behavior, add or extend the matching `tests/test_*.py` module.

## Packaging

The build backend is `hatchling`; the wheel packages `src/mergetrain` and exposes the `mergetrain` console script.

```sh
python -m build
python -m pip install dist/*.whl
mergetrain --version
```

Supported Python: 3.10+. See the [release checklist](release.md) for the full publish flow.

## Conventions

- Keep the core provider-neutral. Service-specific deploy logic belongs in `gates`/`deploy.verify` config or an [adapter](adapter-pattern.md), never in the core package.
- Any new shell execution path (gates, verify hooks, subprocess calls) must be documented; see [security](security.md).
- Never put provider credentials in examples or tests.
