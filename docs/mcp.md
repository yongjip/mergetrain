# MCP server

mergetrain exposes a deliberately small stdio MCP surface. It is an adapter over
the same versioned CLI JSON, not a second integration engine.

## Install

Install [Git](https://git-scm.com/downloads) and
[uv](https://docs.astral.sh/uv/getting-started/installation/) on the machine
that holds your repository. mergetrain is a local stdio server; it has no hosted
endpoint or mergetrain account. The client must be able to start `uvx` and access
the configured checkout, its queue, and its Git remote.

For Codex or Claude Code, the [native plugin](install.md#codex-native-plugin)
adds both the agent skill and the pinned MCP server. Choose the plugin or a
manual MCP configuration below; installing both creates duplicate tools.

### Prepare the repository

For an already configured repository, keep its configuration and skip `init`.
For a new setup:

```sh
uv tool install 'mergetrain==3.7.0'
cd /absolute/path/to/repo
mergetrain init --project my-app --write
```

Add the project's test commands to `.mergetrain.yaml`: a new configuration has
`gates: []` and runs no project tests. Commit the configuration and agent
instructions through your normal integration path before creating task
worktrees. See the [first-run guide](quickstart.md).

The machine-level CLI and the MCP package are separate installations. Keep
their versions aligned. A client can launch the pinned MCP extra without
adding it to the global CLI installation:

```sh
uvx --python ">=3.10" --from 'mergetrain[mcp]==3.7.0' \
  mergetrain --repo /absolute/path/to/repo mcp
```

The published MCP Registry manifest launches the pinned package with `uvx` and
the `mcp` extra. Release CI reconstructs that command in a clean environment,
initializes the server, and checks the tool list and deploy schema.

### Codex: manual MCP configuration

Replace `/absolute/path/to/repo` with your configured checkout:

```sh
codex mcp add mergetrain -- \
  uvx --python ">=3.10" --from 'mergetrain[mcp]==3.7.0' \
  mergetrain --repo /absolute/path/to/repo mcp
codex mcp list
```

Start a fresh session and inspect `/mcp`. See the
[official OpenAI MCP documentation](https://developers.openai.com/codex/mcp).

### Claude Code: manual MCP configuration

Run from your project. The default local scope keeps this entry private to
that project rather than committing a machine-specific absolute path:

```sh
claude mcp add --transport stdio mergetrain -- \
  uvx --python ">=3.10" --from 'mergetrain[mcp]==3.7.0' \
  mergetrain --repo /absolute/path/to/repo mcp
claude mcp get mergetrain
```

Inspect `/mcp` in Claude Code. See its
[MCP installation reference](https://code.claude.com/docs/en/mcp#option-3-add-a-local-stdio-server).

### VS Code / GitHub Copilot

For the VS Code configuration format, add this entry to `.vscode/mcp.json`:

```json
{
  "servers": {
    "mergetrain": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--python", ">=3.10",
        "--from", "mergetrain[mcp]==3.7.0",
        "mergetrain", "--repo", "${workspaceFolder}", "mcp"
      ]
    }
  }
}
```

Open the configured repository as the workspace. Use **MCP: List Servers** to
start the server and inspect its output. In a remote workspace or dev
container, install the runtime where this command executes.

Copilot's portable `.mcp.json` and user `mcp-config.json` use `mcpServers`
instead of `servers`. Use **MCP: Add Server → Command (stdio)** for the selected
configuration destination and enter the pinned launch command above with an
absolute repository path. Follow the
[VS Code configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)
for the format used by your harness.

### Check the connection

Confirm that the five tools below are available. Ask the client to call
`mergetrain_status` with `repo` set to the absolute configured checkout. Check
the returned `repo`, `health`, `state`, and `next_action` before queueing work.
A successful connection or read-only call does not verify deployment-dialog
support. Check that separately before using MCP to deploy.

These configuration examples were checked against the client references on
2026-10-25. They do not promise the same approval UI in every client version.

## Five tools

| Tool | Inputs | Effect |
| --- | --- | --- |
| `mergetrain_status` | optional recent-job limit, optional `repo` | read-only state and exact next action |
| `mergetrain_inspect` | job ID, detail=`summary/events/logs`, bounded limit/cursor, optional `repo` | read-only job evidence |
| `mergetrain_enqueue` | task, branch, optional `repo` | captures exact clean SHAs and queues work; never grants auto deploy |
| `mergetrain_validate` | optional `repo` | assembles the queued train and runs gates; never pushes |
| `mergetrain_deploy` | optional `repo`; no model-visible plan or confirmation parameter | may validate, shows a client-rendered exact plan, then pushes only after human acceptance |

History, statistics, GC, cancellation, unattended deploy, lock mutation, and
recovery are intentionally absent. Exposing them would increase tool-selection
ambiguity and create authority the normal coding agent does not need.

## Repository scope

Available from **3.7.0**. Refresh the plugin and start a fresh MCP session to
load this input; updating the global CLI does not replace a running server.

The server answers for the repository it was launched in. Every tool takes an
optional `repo` path to operate on a different configured checkout, and any
path that is not a checkout already configured by `mergetrain init` is refused
with `unknown_scope` before a CLI runs — never silently answered from the bound
repository.

A server launched with `--config` or `--db` stays bound to that checkout.
A different checkout is refused before any CLI runs; the server never
combines another repository with its original settings or queue.
Existing subdirectories of a checkout with the standard `.mergetrain.yaml`
resolve to that checkout root. With a custom config, pass the checkout root.

Keep the same `repo` value on every call for a task that uses an explicit
scope. Omitting it returns to the server's launch repository; a status call
does not change later calls. Verify the reported checkout and read `health`,
`state`, and `next_action` together. Linked worktrees report different
checkout paths while sharing their control checkout's configuration and queue.
`degraded` can mean a missing remote or integration ref in the correct
repository. Follow the reported action; do not infer wrong scope or run init
from queue counts alone. Outside the server, use `mergetrain --repo <root> ...`.

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

When step 1 has to validate queued work and a job does not pass, there is no
plan to show. The tool returns `deploy_plan_unavailable` with each job's result
in the message and the CLI's validation payload under `validation`. After a
partial result, the jobs that passed form the Ready train that the next call
presents.

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

### What counts as deployment confirmation?

Starting or trusting the server, approving a tool invocation, and writing
"I approve this deploy" in chat do not fill in the server's deployment form.
For MCP deployment, review the tasks and destination in the client-rendered
form, check its confirmation field, and submit the form. The server requires
both the accepted response and that checked field.

Claude Code documents [form elicitation](https://code.claude.com/docs/en/mcp#respond-to-mcp-elicitation-requests).
Other client versions may connect and expose all five tools while failing to
display the form. Do not infer support from the tool list alone.

### If no confirmation dialog appears

1. Read `mergetrain_status` for the same `repo`. Confirm whether anything
   landed before trying another deployment path.
2. If the call returned `confirmation_required` or `deploy_not_confirmed`,
   report that deployment was not confirmed. `deploy_not_confirmed` alone
   does not establish that the person saw and canceled a dialog.
3. If the person declined the displayed plan, stop. Do not retry through
   another path to override that decision.
4. If the client did not display a form, give the operator the returned
   terminal command. In an interactive terminal, the operator runs:

   ```sh
   mergetrain --repo /absolute/path/to/repo deploy
   ```

5. The operator reviews the fresh task list, Git destination, and refs, then
   enters `y` or `yes` to approve that plan. Read status afterward and check
   the configured post-push verification result.

Do not loop the MCP deploy call, manufacture an acceptance response, supply a
hidden plan hash, or add `--auto` to work around a missing dialog. Explicitly
pre-approved unattended operation is a separate
[CLI workflow](cli.md#enqueue); MCP enqueue never grants that approval.

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
`{contract_version,ok:false,error:{code,message,retryable}}` shape, and the
`events` (`frames`) and `logs` (`job_id`, `tail_lines`, `log`) results that the
adapter wraps also carry top-level `contract_version`.

See [the machine contract](contract.md) and [agent contract](agent-contract.md).
