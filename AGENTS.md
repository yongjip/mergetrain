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

## Useful commands

```sh
PYTHONPATH=src python -m unittest discover -s tests
PYTHONPATH=src python -m mergetrain agent-contract --json
PYTHONPATH=src python -m mergetrain init --project demo
```

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
