# Contributing to mergetrain

Thanks for your interest. mergetrain is a local-first merge-and-push queue for
coding-agent worktrees; see [README.md](./README.md) and [docs/](./docs/) for how
it works.

## Development setup

```sh
python -m pip install -e ".[dev]"
```

Python 3.10–3.14 are supported. The package ships **zero runtime dependencies**
(PyYAML is an optional extra) — please keep it that way; the built-in fallback
YAML parser is the default install path and must stay behaviour-compatible with
PyYAML.

## Before you open a PR

Run the same gates CI runs:

```sh
ruff check .          # lint + import order
mypy                  # type-check src/mergetrain
python -m pytest      # tests
```

- **Tests** live in `tests/`; cover any behaviour change. Real-git and
  concurrency tests are welcome — see `tests/test_git_runner.py` and
  `tests/test_store.py` for the patterns.
- **End-to-end:** `bash scripts/e2e.sh` drives the installed CLI through every
  workflow against real git repositories.
- **The machine contract** — every `--json` payload's shape, the `error.code`
  values, and `contract_version` — is guarded by
  `tests/test_contract_fingerprints.py`. If you change a JSON envelope, update its
  fingerprint, and bump `contract_version` when the *shape* changes.
- **English only** for everything that lands in the repo or tracker: issues, PRs,
  commit messages, comments, and docs.

## Changelog

Every user-facing change needs a [`CHANGELOG.md`](./CHANGELOG.md) entry;
`scripts/check_release.py` (run in CI) enforces that a release has a matching
changelog heading.

## Commits & PRs

Keep each PR focused on one logical change, with a clear conventional commit
message. CI must be green before merge: `ruff` + `mypy` + `pytest` across the
3.10–3.14 matrix, a no-yaml leg that exercises the fallback parser, and an
end-to-end leg.
