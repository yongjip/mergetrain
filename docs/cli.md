# CLI reference

mergetrain 3 has one small public grammar. The six core verbs shown by
`mergetrain --help` are:

```text
init  status  enqueue  validate  deploy  inspect
```

Global options may appear before or after the command:

```sh
mergetrain --repo /path/to/repo status --json
mergetrain status --json --repo /path/to/repo
```

Use `mergetrain --version` for the installed release. Add `--json` where
available for the versioned machine contract.

## `init`

```sh
mergetrain init [--project NAME]
mergetrain init [--project NAME] --write
mergetrain init --refresh-instructions
```

Without `--write`, prints the minimal config. With `--write`, creates
`.mergetrain.yaml`, `AGENTS.mergetrain.md`, and `CLAUDE.mergetrain.md` and
refuses to overwrite any existing file. `--refresh-instructions` rewrites only
the two generated sidecars.

## `status`

```sh
mergetrain status [--limit N] [--json]
mergetrain status --diagnose [--json]
```

This is the single state entry point. It returns:

- `health`: `healthy`, `unconfigured`, or `degraded`;
- `state`: `idle`, `waiting`, `running`, `ready`, or `attention`;
- a human summary and exact `next_action` object;
- grouped counts, action-required jobs, and recent compact job summaries.

`--diagnose` adds configuration, Git, runtime provenance, lock, validated-train,
and cleanup detail. Routine agents should not request it.

## `enqueue`

```sh
mergetrain enqueue --task TEXT --branch BRANCH [--worktree PATH] [--note TEXT] [--json]
```

Enqueue always verifies a clean, matching task worktree and captures the exact
integration and task commits. SHA inputs and readiness bypasses do not exist in
the v3 interface. When the configured checkout is on another branch, mergetrain
uses Git's worktree registry to resolve the branch's one live worktree. A
missing or ambiguous match is refused; the optional CLI `--worktree` remains an
explicit override.

`--auto` is intentionally hidden. Use it only after explicit bounded approval
to validate, deploy, and verify the named task unattended. The approval is
bound to the exact destination and execution policy; any later change blocks.

## `validate`

```sh
mergetrain validate [--json]
```

Claims queued work in FIFO order, builds one isolated train, and runs combined
gates. It never pushes. Compatible jobs become Ready; semantic-conflict members
are attributed and moved to Attention together.

## `deploy`

```sh
mergetrain deploy
mergetrain deploy --json
```

This is the complete manual path:

1. validate queued work when no Ready train exists;
2. use the one eligible Ready train;
3. show the tasks, destination, refs, and gate policy;
4. require `y` or `yes` in an interactive terminal;
5. recheck the exact plan, apply the configured gate-reuse policy, atomically
   push, and run verify hooks.

The user never supplies a train ID, plan hash, or preview mode. `deploy --json`
is non-pushing: it may validate queued work, then returns
`result: "confirmation_required"` and the exact plan. MCP owns the supported
non-interactive confirm-and-execute flow.

`validate` pauses while a Ready train exists. This single-Ready invariant keeps
train selection out of the normal v3 workflow. A database carried forward with
multiple pre-v3 Ready trains can use the migration-only
`deploy --train-id ID` option after `status --diagnose`; new automation must not
depend on that option.

## `inspect`

```sh
mergetrain inspect JOB_ID [--event-limit N] [--json]
```

Returns one job's full record, current progress, latest run, train outcome, and
bounded events. Start with `status`; inspect only a job that needs evidence.

## Advanced operator commands

These commands remain callable for automation, evidence, or exceptional state,
but are hidden from the default help and are not part of the six-verb product
grammar.

| Need | Command |
| --- | --- |
| Validate manual jobs continuously | `daemon --validate-only` |
| Deploy pre-approved auto jobs | `daemon` |
| Resolve stranded or ambiguous push state | `reconcile [--apply]` |
| Repair a failed job from its owning branch | `retry JOB_ID [--rebase]` |
| Replace an exact validated train | `supersede --train-id ID --replacement TASK BRANCH WORKTREE` |
| Cancel or dismiss work | `cancel JOB_ID`, `dismiss JOB_ID`, `dismiss --all` |
| Resolve post-push verification | `verify [--job ID] [--ack succeeded|failed]` |
| Clear a wedged runner lock | `unlock [--force]` |
| Preview or apply cleanup | `gc [--apply] [--delete-branches]` |
| Stream evidence | `events`, `logs`, `history`, `stats` |
| Local read-only UI | `dashboard` |
| Multi-repository read/daemon | `hub` |
| Disposable walkthrough | `demo` |
| stdio MCP adapter | `mcp` |

The state response tells the operator which exceptional command is appropriate.
Do not memorize this table or infer recovery from raw SQLite state.

## Removed v2 interfaces

v3 deliberately rejects old spellings with `error.code: "removed_interface"`
and an exact replacement:

| Removed | Replacement |
| --- | --- |
| `doctor` | `status --diagnose` |
| `run-batch --validate-only`, `run-next --validate-only` | `validate` |
| `run-batch --deploy`, `run-next --deploy` | `deploy` |
| `recover` | `reconcile --apply` |
| `version` | `--version` |
| `agent-contract` | `init --refresh-instructions` |
| enqueue SHA/readiness/duplicate flags | automatic exact-SHA readiness checks |
| deploy `--preview`, `--reuse-validated` | `deploy --json`, config policy |

There are no compatibility aliases. Keeping both grammars would preserve the
very ambiguity v3 removes.

## Exit and JSON rules

- `0`: command completed; for execution commands also inspect `result`.
- `1`: an expected operational failure or an execution result that did not ship.
- `2`: invalid or removed interface, or interactive confirmation unavailable.
- `130`: interrupted.
- Recovery commands may use additional documented exit codes.

Every JSON object has top-level `contract_version`. Failures use one envelope:

```json
{
  "ok": false,
  "error": {
    "code": "queue_error",
    "message": "...",
    "retryable": false
  }
}
```

See [the machine contract](contract.md) for the long-lived v3 compatibility
policy and [failure modes](failure-modes.md) before applying recovery commands.
