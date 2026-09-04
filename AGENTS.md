# AGENTS.md

This repository contains `mergetrain`, a local deploy train for coding-agent
worktrees.

## Agent operating rules

1. Work on a task-specific branch and worktree.
2. Commit all changes before enqueueing.
3. Do not push deploy refs directly. For ordinary handoff, enqueue every named
   finished branch in the requested order with
   `mergetrain enqueue --task <task> --branch <branch>` and let mergetrain
   capture the exact SHAs. Stop after the last enqueue; asking to queue for
   validation does not authorize running validation.
4. Read `mergetrain status --json` first and follow its structured
   `next_action`. Use `status --diagnose` only for configuration, Git, runtime,
   or lock detail, and `inspect <job-id>` only for job evidence.
5. Use `--auto` only after explicit unattended-deploy approval. A bounded
   instruction to QA, deploy, verify, and finish end-to-end grants that approval
   for the named task and destination; do not ask for each opaque train ID.
   Auto jobs are bound to that Git destination and the approved gate, reuse,
   and verify policy; they must block if either identity changes.
6. Let one runner or daemon own merge and test. The default daemon may push and
   verify only `--auto` jobs; `daemon --validate-only` handles manual jobs but
   stops at a validated train and never deploys.
7. Fix blocked or failed work in the owning branch, commit a clean result, then
   enqueue a new job.
8. Do not delete or rewrite remote `refs/mergetrain/deploys/*`; they are
   permanent recovery evidence.
9. Treat public product surface as an owner-evidence budget. Before
   adding a CLI command or flag, config field, dashboard control, daemon/Hub
   behavior, MCP tool, recovery action, notification path, or reuse control,
   apply the admission test in `docs/product-scope.md`, prefer consolidation,
   and record the measured cost, repeated workflow, or incorrect state.

## Useful commands

```sh
python -m pytest -q -n auto --cov=mergetrain --cov-report=term-missing --cov-report=json:.coverage.json
python scripts/check_critical_coverage.py .coverage.json
PYTHONPATH=src python -m unittest discover -s tests
PYTHONPATH=src python -m mergetrain status --diagnose --json
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
