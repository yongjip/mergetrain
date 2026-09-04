# Quickstart

mergetrain gives parallel coding-agent worktrees one serialized path into an
integration branch. The normal workflow is intentionally small:

```text
enqueue → validate → approve → push
```

`deploy` can perform the middle three steps in one command, so most operators
use only `status`, `enqueue`, and `deploy` after setup.

## 1. Initialize

From the repository root:

```sh
mergetrain init --project example-app --write
```

The generated `.mergetrain.yaml` contains only the schema version, project
name, and gates. Git remote `origin`, integration branch `main`, local state
paths, sequential gates, and no validation reuse are code defaults.

Add at least one meaningful project gate before deploying. For example:

```yaml
version: 2

project:
  name: example-app

gates:
  - name: tests
    run: pytest -q
```

`init --write` also creates `AGENTS.mergetrain.md` and
`CLAUDE.mergetrain.md`. Link the relevant sidecar from your root agent
instructions, then commit the config and instructions. Existing files are
never overwritten.

After an upgrade, refresh only generated instructions with:

```sh
mergetrain init --refresh-instructions
```

## 2. Commit and enqueue a task branch

```sh
git switch -c codex/feature-a
# edit and test
git add .
git commit -m "feature a"

mergetrain status
mergetrain enqueue --task "feature a" --branch codex/feature-a
```

mergetrain verifies a clean worktree and captures the exact integration and
task commits. Do not copy SHAs by hand.

For a task agent, enqueue every named finished branch in the requested order.
The last successful enqueue is the handoff boundary. Stop there unless the user
explicitly authorized validation or the complete validation-and-deployment
workflow. Asking to queue branches for validation authorizes enqueue only.

## 3. Deploy

```sh
mergetrain deploy
```

If queued work has not been validated, `deploy` first assembles the FIFO train
and runs the configured gates. It then shows the exact tasks, destination,
refs, and gate policy and asks for confirmation. Only `y` or `yes` continues to
the pre-push execution. With the safe default reuse policy, that execution
reassembles the train and reruns gates before the atomic push and post-push
verification. Train IDs and plan hashes remain internal.

To run gates earlier without any possibility of a push:

```sh
mergetrain validate
```

The resulting Ready train stays available for a later `mergetrain deploy`.

## 4. Follow the state

```sh
mergetrain status
mergetrain status --json
mergetrain inspect <job-id>
```

`status` is the single entry point. It projects detailed queue state into five
groups—Waiting, Running, Ready, Attention, and Done—and provides an exact
`next_action.command` when action is needed. Use `inspect` only for one job's
evidence. Add `status --diagnose` for configuration, Git, runtime, and lock
details.

If `status` reports an ambiguous push, follow its recovery command:

```sh
mergetrain reconcile --apply
```

Reconciliation asks the pinned remote endpoint what landed; it never guesses
or blindly repeats a push.

## Next guides

- [Configuration reference](config.md) for non-default Git, gates, verification,
  and performance settings.
- [Operations](daemon.md) for validation and pre-approved deploy daemons.
- [Failure modes](failure-modes.md) for repair and recovery commands.
- [MCP server](mcp.md) for the five-tool agent adapter.
- [CLI reference](cli.md) for the stable core and advanced operator surfaces.
