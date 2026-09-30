# MCP server

mergetrain exposes a deliberately small stdio MCP surface. It is an adapter over
the same versioned CLI JSON, not a second integration engine.

## Install

```sh
uv tool install 'mergetrain[mcp]'
mergetrain mcp
```

The published MCP Registry manifest launches the pinned package with `uvx` and
the `mcp` extra. Release CI reconstructs that command in a clean environment,
initializes the server, and checks the tool list and deploy schema.

## Five tools

| Tool | Inputs | Effect |
| --- | --- | --- |
| `mergetrain_status` | optional recent-job limit | read-only state and exact next action |
| `mergetrain_inspect` | job ID, detail=`summary/events/logs`, bounded limit/cursor | read-only job evidence |
| `mergetrain_enqueue` | task, branch | captures exact clean SHAs and queues work; never grants auto deploy |
| `mergetrain_validate` | none | assembles the queued train and runs gates; never pushes |
| `mergetrain_deploy` | no model-visible plan or confirmation parameter | may validate, shows a client-rendered exact plan, then pushes only after human acceptance |

History, statistics, GC, cancellation, unattended deploy, lock mutation, and
recovery are intentionally absent. Exposing them would increase tool-selection
ambiguity and create authority the normal coding agent does not need.

## Deploy confirmation

`mergetrain_deploy` uses MCP v2 resolver dependencies:

1. run the CLI's non-pushing `deploy --json` path;
2. obtain an exact plan hash over train, destination, gates, reuse policy, and
   verify hooks;
3. render task and destination details to the client;
4. require a client-side elicitation result with both `action=accept` and an
   explicitly checked confirmation field;
5. pass the hidden plan hash back to the CLI;
6. recheck the same identity inside the claim and immediately before push.

The confirmation value is not part of the model-visible tool schema. A model
cannot manufacture it in tool arguments. Decline, cancel, an unchecked box, a
client without form elicitation, or a changed plan produces a typed refusal and
zero pushes.

From protocol 2026-07-28 the client returns the answer in a retried tool call,
and the server prepares the plan again before it uses that answer. The
confirmation schema carries a value derived from the plan hash, so if the plan
changed while the dialog was open, the retried call asks the human again
instead of applying the earlier answer to the new plan.

When a client cannot render elicitation, the response includes the reviewed
summary and the ordinary fallback command:

```sh
mergetrain --repo /path/to/repo deploy
```

The terminal renders and confirms a fresh plan; it does not expose plan hashes
or train IDs to the user.

## Inspection detail

Routine calls should use `detail="summary"`. Events and raw logs are requested
only when the compact job evidence is insufficient:

```text
detail="events"  → bounded, non-following JSONL frames
detail="logs"    → bounded raw log tail
```

Logs may contain trusted command output and should be treated as sensitive.
The server redacts recognized secrets, caps output, and replaces local path
roots in synthesized diagnostics.

## Cancellation and timeout

Validation can run a real test suite. MCP cancellation, server shutdown, and a
bounded timeout stop the CLI process group and let mergetrain release its lease;
the gate process is not left running invisibly. The server sends the CLI SIGINT
and allows it 20 seconds to stop its gates before it kills the CLI's group. On
POSIX, stopping the server with SIGTERM or SIGHUP, as well as closing its stdin,
stops any CLI still running first; a SIGKILL of the server cannot. On Windows the
CLI runs in a Job Object, which also holds the gates that the CLI starts in their
own process groups, so stopping the CLI stops those gates too.

A deploy stopped by the timeout may already have pushed. The tool then says so,
and the agent reads `mergetrain_status` before deciding what happened.

## Contract

The tool names and required inputs are part of the long-lived v3 compatibility
promise. Successful CLI-backed responses preserve the CLI payload unchanged,
including `contract_version`. Adapter-only failures use the same
`{ok:false,error:{code,message,retryable}}` shape.

See [the machine contract](contract.md) and [agent contract](agent-contract.md).
