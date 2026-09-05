# mergetrain for Claude Code

Use this plugin when several coding agents finish committed branches in
separate Git worktrees and one local owner needs to test those branches
together before pushing them. It is not intended for single-agent work or for
repositories already integrated through a hosted pull-request merge queue.

The plugin contributes two skills and the five-tool mergetrain MCP server. The
ordinary task-agent boundary is deliberately small: inspect status, enqueue
every named clean branch in order, and stop. Validation, deployment, and
recovery retain their separate approval requirements.

## Requirements and installation

Install [Claude Code](https://code.claude.com/docs/en/overview), Git, and
[`uv`](https://docs.astral.sh/uv/getting-started/installation/). Then install
from the mergetrain marketplace:

```sh
claude plugin marketplace add yongjip/mergetrain
claude plugin install mergetrain@mergetrain
```

The plugin starts the exact released MCP extra with `uvx`; a separate global
mergetrain installation is not required for its tools. The first tool call may
download the pinned wheel. After community-directory approval, the equivalent
catalog install is:

```sh
claude plugin marketplace add anthropics/claude-plugins-community
claude plugin install mergetrain@claude-community
```

To exercise the product in a disposable repository without installing it:

```sh
uvx mergetrain demo
```

## Example prompts

Read-only status inspection:

> Show the current mergetrain status and explain its next action. Do not change
> queue or Git state.

Safe handoff of parallel branches:

> The committed branches `agent/api` and `agent/ui` are finished. Queue both in
> that order for combined validation, then stop.

Explicit operator workflow:

> QA, deploy, and verify the queued work to the configured main destination.
> Finish the complete bounded workflow.

The last prompt authorizes the existing approval-aware workflow; it does not
let the model invent a destination, weaken the gate policy, or bypass the
client-rendered deployment confirmation.

## Privacy and security

mergetrain has no account, hosted control plane, OAuth app, advertising, or
product telemetry. Queue state and evidence stay in the local repository. The
plugin contacts PyPI only when `uvx` needs the pinned package, and Git is used
only for the repository and destinations configured by the operator.

Deployment remains bound to the exact destination and execution policy. The
human-invoked deploy skill cannot be selected by the model, and interrupted
push recovery follows durable remote evidence. Read the full
[security boundary](https://github.com/yongjip/mergetrain/blob/main/docs/security.md)
before enabling deployment in an unfamiliar repository.

## Troubleshooting and support

- Run `claude plugin validate integrations/claude/plugin --strict` in a source
  checkout to validate the package.
- Run `uvx --from 'mergetrain[mcp]==3.0.7' mergetrain --version` to verify the
  pinned Python runtime is reachable.
- Start Claude Code with `--debug` and inspect the plugin manager's Errors tab
  if the MCP server or skills do not load.
- Report product and installation problems through
  [GitHub Issues](https://github.com/yongjip/mergetrain/issues).
- Report security concerns using the private channel documented in
  [SECURITY.md](https://github.com/yongjip/mergetrain/blob/main/SECURITY.md).
