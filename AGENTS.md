# AGENTS.md

This repository contains `mergetrain`, a local deploy train for coding-agent
worktrees.

## Agent operating rules

1. Work on a task-specific branch and worktree.
2. Commit all changes before enqueueing.
3. Do not push deploy refs directly.
4. Read `mergetrain doctor --json` or `mergetrain status --json` before deciding
   the next action.
5. Use `--auto` only after explicit unattended-deploy approval.
6. Let one runner or daemon own merge, test, push, and verify.
7. Fix blocked or failed work in the owning branch, commit a clean result, then
   enqueue a new job.
8. Do not delete or rewrite remote `refs/mergetrain/deploys/*`; they are
   permanent recovery evidence.
9. Treat public product surface as a zero-growth budget by default. Before
   adding a CLI command or flag, config field, dashboard control, daemon/Hub
   behavior, MCP tool, recovery action, notification path, or reuse control,
   apply the admission test in `docs/product-scope.md` and record the evidence.

## Useful commands

```sh
python -m pytest -q -n auto --cov=mergetrain --cov-report=term-missing --cov-report=json:.coverage.json
python scripts/check_critical_coverage.py .coverage.json
PYTHONPATH=src python -m unittest discover -s tests
PYTHONPATH=src python -m mergetrain agent-contract --json
PYTHONPATH=src python -m mergetrain init --project demo
python scripts/check_architecture.py
```

Pytest is configured to import the current `src/` checkout without an external
`PYTHONPATH`. If the full suite hits sandbox-only `PermissionError` failures for
localhost sockets or process inspection, do not repeat it in the same sandbox:
rerun it once outside the sandbox. Invoke mergetrain with the intended
virtualenv's Python; gate commands automatically prioritize sibling tools from
that environment.

When changing production imports, keep the one-way boundaries documented in
`docs/design.md`; `scripts/check_architecture.py` is a blocking CI check. Do not
silence it with broad exceptions—document and encode any legitimate new edge.

When changing public product surface, update the baseline and classification in
`docs/product-scope.md` in the same change. Prefer consolidating an existing
surface; do not turn an implementation possibility into a feature without a
measured cost, concrete incorrect state, or repeated workflow need.

## GitHub CLI authentication

- The Codex sandbox may be unable to read `gh` credentials from the macOS
  Keychain. A sandboxed `gh auth status` or `gh auth token` failure is not proof
  that the stored token is invalid.
- Before asking the user to run `gh auth login`, retry authentication outside
  the sandbox and verify both credential access and the API, for example with
  `gh auth token -h github.com >/dev/null` and `gh api user --jq .login`. Never
  print or log the token.
- If those external checks succeed, reuse the existing login. Request a new
  login only when the same checks genuinely fail outside the sandbox.
- The Codex GitHub connector and the local `gh` CLI use separate credentials.
  Prefer the connector for supported GitHub reads/writes; use externally run
  `gh` as the fallback when the connector lacks repository permission.

## Boundaries

- Never add provider-specific credentials to examples or tests.
- Keep core provider-neutral; adapters belong under `integrations/` or in a
  separate service repository.
- Gate and verify commands run through `/bin/sh`; document new shell execution
  behavior clearly.

## Imported Claude Cowork project instructions
