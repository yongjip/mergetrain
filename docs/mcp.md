# MCP server

`mergetrain mcp` serves the queue to coding agents over the Model Context
Protocol on stdio. It exists for one reason above discovery: it turns
confirm-then-deploy from an instruction an agent may follow into a mechanism it
cannot bypass.

The server needs the optional extra:

```sh
uv tool install 'mergetrain[mcp]'   # or: pip install 'mergetrain[mcp]'
```

Register it once per agent CLI:

```sh
claude mcp add mergetrain -- mergetrain mcp
codex mcp add mergetrain -- mergetrain mcp
gemini mcp add mergetrain mergetrain mcp
```

Pass `--repo` to operate a repository other than the working directory:
`mergetrain --repo /path/to/service mcp`.

## Claude Code plugin

The repository is also a Claude Code self-marketplace. The plugin bundles this
MCP registration with an operating skill and a separate manual-only deploy
skill:

```text
/plugin marketplace add yongjip/mergetrain
/plugin install mergetrain@mergetrain
```

It exposes `/mergetrain:mergetrain` for normal queue operation and
`/mergetrain:deploy` for the explicit deploy path. The plugin does not bundle
the Python executable: install `mergetrain[mcp]` first. Its `.mcp.json` uses the
bare `mergetrain mcp` command so the same installed CLI and queue state remain
the source of truth.

The deploy skill has `disable-model-invocation: true`. Claude cannot select it
automatically, and invoking it still calls the MCP deploy tool whose
client-rendered elicitation is the actual confirmation boundary.

## What the tools return

Every tool shells out to the CLI with `--json` and returns that payload
verbatim, so [`contract_version`](contract.md) stays the single machine
interface and there is no second contract to keep in step. `ok` still means only
that the command ran: read `result` for a run's outcome, `health` for repo
health, and `error.code` on failure. A command that fails returns the contract's
failure envelope unchanged — a non-zero exit is an answer, not an exception.

| Tool | CLI behind it | Side effects |
| --- | --- | --- |
| `mergetrain_status` | `status --json` | none |
| `mergetrain_doctor` | `doctor --json` | none |
| `mergetrain_inspect` | `inspect <id> --json` | none |
| `mergetrain_history` | `history --json` | none |
| `mergetrain_stats` | `stats --json` | none |
| `mergetrain_agent_contract` | `agent-contract --json` | none |
| `mergetrain_gc_preview` | `gc --json` | none (dry run only) |
| `mergetrain_events` | `events --jsonl` | none; bounded, never follows |
| `mergetrain_logs` | `logs <id> --tail` | none; capped tail, never follows |
| `mergetrain_validate` | `run-batch --validate-only --json` | runs gates, moves job status |
| `mergetrain_enqueue` | `enqueue --capture-sha --json` | queues a job |
| `mergetrain_deploy` | preview, then `run-batch --deploy --train-id --expected-plan --json` | **pushes code** |

The default operating path deliberately foregrounds six tools:
`mergetrain_doctor`, `mergetrain_status`, `mergetrain_enqueue`,
`mergetrain_validate`, `mergetrain_deploy`, and `mergetrain_inspect`. The other
six registered tools are advanced operator views; use them only when the
default state and inspection payloads do not answer the decision.

`mergetrain_validate` is free to run under the operating contract, but it is
**not** annotated `readOnlyHint`: it creates a worktree, runs the configured gate
commands, and moves jobs to `validated` or `blocked`. Annotations describe
actual side effects, so clients are told the truth rather than what would make
the tool cheaper to call.

## What is not exposed, ever

`daemon`, `enqueue --auto`, `gc --apply`, `gc --delete-branches`, `cancel`,
`unlock`, `dismiss`, and the recovery mutations have no tool, and no tool takes a
parameter that could reach them — `mergetrain_enqueue` has no `auto`,
`mergetrain_gc_preview` has no `apply`. An agent connected through this server
cannot start an unattended deploy or a destructive cleanup no matter what a
prompt tells it. Those stay terminal-only by design; ask the human to run them.

## The deploy gate

`mergetrain_deploy` has no `confirm` parameter, deliberately: a model-supplied
argument would be the model confirming its own deploy. Instead the server

1. re-reads `doctor` and `status`,
2. selects the validated train — refusing with `train_id_required` when several
   are pending, because choosing for the human would ship code nobody picked,
3. asks CLI preview to hash the exact train, destination, gate/reuse policy, and
   verify hooks into `deploy_plan_sha`,
4. builds a human-readable summary of only the selected change set: task intent,
   member branches and recorded HEADs, the effective push endpoint and refs,
   pre-push gates,
   post-push verification, validation evidence, stale-base reassembly risk,
   `next_action`, and every blocked, failed, or reconcile-pending job from the
   uncapped `attention_jobs` view; the opaque train ID remains internal,
5. asks the client to show it and requires an explicit accept **and** a checked
   confirmation before running the deploy with that hash as `--expected-plan`.

The server uses MCP SDK v2's resolver-driven elicitation. On the current
protocol the SDK returns the confirmation as an `InputRequiredResult`, binds
the response to that exact rendered question, and then retries the tool call.
mergetrain re-reads `doctor` and `status` on that retry, so a train that stopped
being deploy-eligible while the dialog was open is refused instead of shipping
from the stale first-round snapshot. Older negotiated MCP protocols use the
same resolver and retain their server-to-client elicitation flow.
The CLI compares the plan again before claim and immediately before push, so a
fetch URL, effective `pushurl`, push ref, gate/reuse policy, or verify-hook change after the dialog
fails with `deploy_plan_changed` and touches no remote ref.

Anything short of that is a refusal, and nothing is pushed:

| Situation | `error.code` |
| --- | --- |
| Client declared no elicitation support | `confirmation_required` (with the terminal command to run) |
| Human declined or cancelled | `deploy_not_confirmed` |
| Accepted with the box unchecked | `deploy_not_confirmed` |
| The client callback or MCP exchange failed | MCP call error; deploy is not started |
| Several validated trains pending | `train_id_required` |
| Named train absent or not deploy-eligible | `train_not_found` / `no_validated_train` |
| Confirmed deploy plan changed | `deploy_plan_changed` |

A client that cannot render a dialog gets the exact command to run in a
terminal, which keeps the confirmation an attributable human act rather than
something that silently degrades to trust.

## Limits

- stdio only. There is no hosted or remote server: mergetrain is local-first and
  the queue state lives on the operator's machine.
- One repository per server process, fixed at startup by `--repo`.
- The MCP layer adds no capability the CLI lacks. An agent with shell access can
  already drive mergetrain; what this adds is a smaller surface, truthful side
  effect annotations, and a deploy that a human has to accept.
- When the MCP SDK cancels an in-flight tool coroutine, mergetrain terminates
  the CLI process group and its descendants before that cancellation returns.
  If a deploy had already written its durable pending marker, normal
  `reconcile` recovery remains the source of truth; cancellation never guesses
  whether the push landed.
- Valid CLI JSON is returned verbatim. When a child fails outside that contract,
  the MCP-synthesized diagnostic is bounded, applies the shared best-effort
  secret redaction policy, and shortens the repository/home path. Successful
  `mergetrain_logs` output remains the explicit raw-log path.
