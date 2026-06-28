# AGENTS.md

This repository contains `trainyard`, a local deploy train for coding-agent
worktrees.

## Agent operating rules

1. Work on a task-specific branch and worktree.
2. Commit all changes before enqueueing.
3. Do not push deploy refs directly.
4. Read `trainyard doctor --json` or `trainyard status --json` before deciding
   the next action.
5. Use `--auto` only after explicit unattended-deploy approval.
6. Let one runner or daemon own merge, test, push, and verify.
7. Fix blocked or failed work in the owning branch, commit a clean result, then
   enqueue a new job.

## Useful commands

```sh
PYTHONPATH=src python -m unittest discover -s tests
PYTHONPATH=src python -m trainyard agent-contract --json
PYTHONPATH=src python -m trainyard init --project demo
```

## Boundaries

- Never add provider-specific credentials to examples or tests.
- Keep core provider-neutral; adapters belong under `integrations/` or in a
  separate service repository.
- Gate and verify commands run through `/bin/sh`; document new shell execution
  behavior clearly.
