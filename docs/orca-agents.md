# Codex, Claude Code, and agy in Orca

Use each client's native mergetrain plugin in an Orca task terminal. The three
plugins provide the same queue protocol and launch the same pinned MCP
package. Start with the [Orca worktree setup](orca-worktrees.md): the control
checkout must already have landed configuration and project gates.

These examples cover CLI sessions in Orca terminals. Installing a plugin is
per execution host and user profile, not per task branch. A local install does
not install it on an SSH host or in WSL.

## Prepare the MCP runtime in the same terminal

The native plugins use `uvx`, independently of a Homebrew or globally installed
mergetrain CLI. Before starting the client, verify their pinned runtime:

```sh
uvx --python ">=3.10" --from "mergetrain[mcp]==3.7.0" mergetrain --version
```

The current plugin and Registry launchers select Python **3.10 or newer**
with `--python ">=3.10"`, matching the package requirement. Codex 0.133.0 did
not forward the shell's `UV_PYTHON` to its MCP child in the real model test.
An older launcher then selected macOS Python 3.9.6 and closed before the MCP
handshake. An exported shell variable alone does not fix that Codex case;
refresh the plugin to use the corrected launcher.

The first run may download an interpreter and dependencies. Prepare the exact
pinned runtime in the task terminal before starting a fresh agent session:

```sh
uvx --python ">=3.10" --from "mergetrain[mcp]==3.7.0" mergetrain --version
```

Cold dependency installation exceeded the tested clients' 30-second MCP startup
limit. Preparing the runtime before model startup avoids that work during the
handshake. Claude also cached a failed connection for about 15 minutes in one
attempt; use a fresh session after preparing the runtime and check its actual
connection before assigning edits. The package stays pinned to **3.7.0**.
See [installation](install.md) for `uv` and version upgrade guidance.

## Codex CLI

Install once on the execution host:

```sh
codex plugin marketplace add yongjip/mergetrain --ref main
codex plugin add mergetrain@mergetrain
codex plugin list --marketplace mergetrain
codex mcp list --json
```

The plugin row should say **installed, enabled**, with version **3.7.0**.
The MCP inventory should include enabled `mergetrain` with a stdio `uvx`
command. Codex **0.133.0** does not accept `--json` on `plugin list`; the JSON
option above belongs to `mcp list`.

Select the intended Orca task worktree, open its terminal, and start a fresh
`codex` session there. `/plugins` opens the plugin browser. A new session loads
the installed skills and tools. Follow [OpenAI's plugin guidance](https://learn.chatgpt.com/docs/plugins#plugin-browser-in-codex-cli)
and [marketplace setup](https://developers.openai.com/plugins/build/plugins#add-a-marketplace-from-the-cli).

## Claude Code

Install once on the execution host:

```sh
claude plugin marketplace add yongjip/mergetrain
claude plugin install mergetrain@mergetrain
claude plugin list --json
claude plugin details mergetrain@mergetrain
claude mcp list
```

Check `enabled: true`, version **3.7.0**, and its mergetrain MCP configuration.
`plugin details` lists the `mergetrain` and `deploy` skills and one MCP server.
After preparing the pinned runtime, the native MCP health check reported
**Connected** in the recorded 3.5.0 test.

Start `claude` from the task worktree's terminal. `/plugin` shows the installed
plugin; its task skill is `/mergetrain:mergetrain`. Use the task skill or the
shared prompt below for registration. Follow [Claude's plugin installation and
reload guidance](https://code.claude.com/docs/en/discover-plugins) if an existing
session has not loaded a new installation.

## agy / Antigravity CLI

Install the native plugin directly from the repository:

```sh
agy plugin install https://github.com/yongjip/mergetrain
agy plugin list --json
```

Check `name: mergetrain`, `enabled: true`, a `mergetrain` entry in `skills`, and
a `mergetrain` entry in `mcpServers`. Its `mcp_config.json` supplies the pinned
stdio server. In the tested **agy 1.3.5 interactive model session**, that plugin
inventory did not make the MCP tools available. Explicitly registering the same
pinned stdio runtime before starting a fresh session exposed the tools:

```sh
agy mcp add mergetrain-orca -- uvx --python ">=3.10" \
  --from "mergetrain[mcp]==3.7.0" mergetrain --repo /absolute/path/to/control-checkout mcp
agy mcp list
```

Replace the path with the control checkout on the execution host. Linked task
worktrees share that repository's queue; enqueue resolves the named branch's
live owning worktree. Give different repositories different server names and
start a fresh session after changing the entry. This configuration is bound to
one repository by default. Since 3.7.0, [repository scope support](mcp.md#repository-scope)
accepts an optional per-call `repo` for another configured checkout. Keep the
same `repo` on every call for that task; omitting it uses the launch repository.

Start `agy` from the task worktree's terminal. `/plugin` or `/plugins` opens
its plugin manager. In the earlier plugin-only **agy 1.3.5** check, standalone
`agy mcp list` printed
`No MCP servers configured` even though the native plugin inventory included
its MCP server. Use the plugin inventory and the session's actual tool
availability to check this plugin; that standalone message alone did not
establish a missing plugin. The later real session showed why actual tool
availability must also be checked. The tested account did not support `--mode auto`; its task used `accept-edits` with individual approvals for MCP and shell
commands. Do not assume plugin installation grants those permissions. See [Antigravity's native plugin guide](https://www.agy.dev/docs/plugins?tab=cli).

## Give the task agent a bounded handoff

All three clients can use the same instruction, with your actual task and
worktree filled in:

```text
Use the installed mergetrain plugin for this Orca task worktree.
Implement the requested change, run the project checks, and commit a clean HEAD.
Read mergetrain_status for health, state, and handoff context.
Its next_action guides an authorized runner; do not execute runner actions.
Enqueue this finished branch with mergetrain_enqueue using its task and actual
Git branch name. Report the job ID and pinned commit, then stop.
Do not validate, deploy, or start a daemon.
```

Start the session from the intended worktree so the MCP server's default
repository is correct. Linked worktrees share their control checkout's queue.
Since 3.7.0, the tools also accept an explicit `repo`; keep it on every
call for the same task. Servers with explicit `--config` or `--db` remain bound
to their original checkout. See [Repository scope](mcp.md#repository-scope).

The five stable tools are `mergetrain_status`, `mergetrain_inspect`,
`mergetrain_enqueue`, `mergetrain_validate`, and `mergetrain_deploy`. Enqueue
requires only `task` and `branch`; it resolves the clean owning worktree and
captures exact commits. Plugin installation does not authorize the task agent
to validate or deploy.

For a separately authorized validation session, use an instruction such as:

```text
Validation is approved for the named queued jobs in this repository.
Read mergetrain_status and confirm the queue contains only the approved work.
If it does, call mergetrain_validate once, then read status and inspect the
resulting jobs. Report Ready and Attention results and the pinned commits.
Do not call deploy, start a daemon, or perform recovery.
```

For repair, follow the [worktree retry example](orca-worktrees.md#repair-a-failed-gate)
in the owning task terminal. If the plugin tools are unavailable, check the
client's plugin inventory, `uvx`, the selected Python, and the execution host;
the existing [terminal CLI workflow](orca-worktrees.md#register-committed-work-then-validate-when-authorized)
remains available for an explicitly chosen CLI session.

## What was tested

On **2026-10-14**, real Orca PTYs in an isolated macOS profile installed the
remote native plugins using Codex **0.133.0**, Claude Code **2.1.287**, and agy
**1.3.5**. All three clients recognized their plugins. Codex published its
stdio MCP configuration, Claude's native MCP connection succeeded, and agy
listed the skill and plugin MCP component.

A stdio test client then invoked each declared plugin MCP command from the same
Orca task worktree. The first registered and validated a committed branch;
all three servers exposed exactly five tools and read/inspected the same Ready
job with exact commit pins. `auto_deploy` remained false, `push_status` stayed
`not_run`, and local/remote main stayed unchanged. The native installation and
runtime-flow Electron test, 12 manifest/registry tests, and the published-package
Registry probe passed. See the [verification summary](evidence/orca-agents-2026-10-14.json).

The preceding installation and stdio probe did not run model-driven editing.
A follow-up acceptance run on **2026-10-15 KST** used real **Codex 0.162.1**, **Claude Code 2.1.287**,
and **agy 1.3.5** sessions in Orca-created worktrees and PTYs. The native
plugins were installed from the local checkout with the Python-selection fix;
the MCP package came from the published **3.5.0** pin. Agy used an explicit
stdio registration and individual approvals, as described above.

| Client | Actual work | Commit | Queue job |
| --- | --- | --- | --- |
| codex | slug, 7 tests | `a68c76c0f07d` | #2 |
| claude | chunks, 9 tests | `85ead898285d` | #1 |
| agy | retry, 4 tests | `4c3751d5e15a` | #3 |

Each model read MCP status before editing, changed only its assigned module
and test file, committed with Yongjip's personal Git identity, and enqueued
through MCP. After all three handoffs, the operator clicked **Validate queue**
in the [draft Orca panel](https://github.com/yongjip/orca/pull/1) at
`b6ab6b700233`. The combined gate passed all **20 tests** and the UI reported
**Ready 3, Attention 0**. Every job retained its exact commit pin,
`auto_deploy: false`, and `push_status: not_run`; local and remote main stayed
at the original commit. The hidden Electron acceptance test passed in
12.2 minutes. The manifest, Registry, release, and discovery checks passed
29 tests plus two subtests, and the real published-package Registry startup
check passed with the system's older Python as the ambient selection.

See the [real model verification summary](evidence/orca-real-agents-2026-10-14.json).
Temporary profiles, credential copies, the Agy plugin, and its test MCP entry
were removed. The result covers CLI sessions on macOS, with additional Agy
setup and approvals. Orca structured chats and Windows/WSL/SSH combinations
remain untested. The [worktree guide](orca-worktrees.md#verification-and-host-limits)
records the separate terminal/UI queue and failure-repair tests.
