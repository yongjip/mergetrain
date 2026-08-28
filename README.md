# mergetrain

<!-- mcp-name: io.github.yongjip/mergetrain -->

[![CI](https://github.com/yongjip/mergetrain/actions/workflows/ci.yml/badge.svg)](https://github.com/yongjip/mergetrain/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mergetrain)](https://pypi.org/project/mergetrain/)
[![Python](https://img.shields.io/pypi/pyversions/mergetrain)](https://pypi.org/project/mergetrain/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/yongjip/mergetrain/blob/main/LICENSE)

**Parallel agents need a serial integration spine.**

mergetrain is a local-first deploy train for coding-agent worktrees. Agents
commit and enqueue their branches; one runner assembles them in order, tests the
combined tree, and atomically updates your Git refs only after explicit approval.

## The problem

Worktrees let several agents edit one repository without sharing a checkout.
They do not decide landing order, test the combined result, prevent push races,
or tell you what happened if a laptop dies mid-push.

Without an integration boundary, the human becomes that boundary: rebase every
finished branch, rerun gates after each merge, resolve cross-branch failures,
and decide which session may push. The parallel coding gain disappears at the
last mile.

<p align="center">
  <img src="https://raw.githubusercontent.com/yongjip/mergetrain/main/docs/images/mergetrain-explainer.gif"
       alt="Three coding agents enqueue branches. One runner assembles and tests their combined train before one atomic push."
       width="720">
</p>

mergetrain makes that last mile a durable protocol:

```text
agent branches → FIFO queue → isolated integration worktree → combined gates
               → explicit approval → one atomic push → post-push verification
```

## Who should use it?

Use mergetrain when:

- multiple coding agents finish branches in the same repository throughout the
  day;
- agents work in Git worktrees and should enqueue rather than push deploy refs;
- the combined result must pass local tests before it lands;
- you want unattended processing only for explicitly pre-approved jobs; or
- one local hub should show queues and runners across several repositories.

It is harness-agnostic: Codex, Claude Code, scripts, and humans all use the same
CLI and JSON contract.

## Who should not use it?

You probably do not need mergetrain when:

- one person or agent lands one branch at a time;
- every change already goes through a PR and your forge-native merge queue;
- you need a hosted review UI, organization-wide permission system, or remote
  runner service; or
- you are looking for a general job queue, CI provider, or deployment platform.

For PR-first teams, use GitHub Merge Queue or GitLab Merge Trains. mergetrain is
for local-agent, worktree-first integration, with or before a PR.

## See it in 60 seconds

```sh
uvx mergetrain demo
```

The demo creates a disposable repository and local bare remote, then runs four
real branches through FIFO merge, a combined-only gate failure, conflict
attribution, and deployment of the compatible train. Use `--keep` to inspect the
result afterward.

<p align="center">
  <img src="https://raw.githubusercontent.com/yongjip/mergetrain/main/docs/images/demo.gif"
       alt="mergetrain's disposable one-minute workflow demonstration"
       width="900">
</p>

## Install and first run

```sh
# Install the machine-level CLI
uv tool install mergetrain          # or: pipx install mergetrain
# macOS: brew install yongjip/tap/mergetrain

cd /path/to/your/repo

# Write .mergetrain.yaml plus agent instructions
mergetrain init --project my-app --write

# After an agent commits its task branch
mergetrain enqueue \
  --task "add health check" \
  --branch agent/health \
  --capture-sha

# Inspect first, then validate or deploy explicitly
mergetrain status --json
mergetrain run-batch --validate-only
mergetrain run-batch --deploy
```

`deploy` means the configured atomic Git ref update; it does not imply an App
Store, Kubernetes, or other provider release. Projects can select `integrate`
or `push` terminology without changing the stable machine contract.

`mergetrain init` also writes agent-facing instructions. The essential rule is
simple: agents commit and enqueue; one runner owns merge → test → push → verify.
Unattended daemons process only jobs that a human explicitly enqueued with
`--auto`.

See the [quickstart](https://github.com/yongjip/mergetrain/blob/main/docs/quickstart.md)
for configuration, dashboard, daemon, and multi-repository Hub setup.

## Why not just worktrees and `git merge`?

Worktrees solve **parallel editing**. mergetrain solves **serialized
integration**.

| Integration concern | Worktrees + manual merge | mergetrain |
|---|---|---|
| Landing order | A person or agent decides repeatedly | Durable FIFO queue |
| Combined validation | Rerun manually after each merge | Gates run over the exact assembled train |
| Cross-branch failure | Diagnose by hand | Isolation runs identify the conflicting pair |
| Push ownership | Every session can race the ref | One lease-fenced runner owns the push |
| Approval | Shell convention | Explicit validate/deploy intent; `--auto` is opt-in |
| Crash recovery | Infer from local logs | Reconcile SQLite evidence against remote refs |

Plain worktrees remain the execution lanes. mergetrain is the spine that joins
their results without turning the operator into a merge coordinator.

## Why not GitHub or GitLab merge queues?

They solve a related problem for a different operating model.

| | Forge-native queue | mergetrain |
|---|---|---|
| Primary unit | Pull/merge request | Committed local task branch |
| Validation | Forge merge group + remote CI | Local assembled train + shell gates |
| Review | Built-in conversation and approvals | No code-review UI |
| Infrastructure | Forge integration and hosted services | Local SQLite, Git worktrees, any Git remote |
| Best fit | PR-first teams and distributed review | High-throughput local agent integration |

The models can coexist: push a validated train to a review branch and open one
PR, or reserve individual PRs for changes that need discussion. The
[PR workflow guide](https://github.com/yongjip/mergetrain/blob/main/docs/pr-workflows.md)
covers direct, one-PR, split-PR, and validation-only patterns.

## Core safety guarantees

- **Exact train identity.** Approval names the task HEADs and integration base;
  changed branches or a moved base cannot silently reuse that approval.
- **Combined gates before push.** A green branch is not enough. The assembled
  train passes the configured gates, or nothing lands.
- **One fenced owner.** SQLite claims and lease tokens prevent concurrent or
  stale runners from mutating the same train.
- **Atomic remote update.** Payload refs and a permanent
  `refs/mergetrain/deploys/<sha>` recovery ref update together.
- **Remote-truth recovery.** Write-ahead markers and pinned commits let
  `reconcile`/`recover` determine whether a killed push landed, without replaying
  a successful deploy or calling a missing one shipped.
- **Explicit automation.** A bare run never deploys. Daemons touch only
  pre-approved `--auto` jobs, and MCP deploy still requires attributable human
  confirmation.
- **Observable state.** `doctor`, `status`, inspection, events, and statistics
  expose structured state and the next safe action instead of asking an agent to
  infer it from processes or prose.

Queue state, locking, train assembly, and gates stay local. Your configured Git
remote and post-push verification may still use external services. Gate and
verify commands are trusted code; review the
[security boundary](https://github.com/yongjip/mergetrain/blob/main/docs/security.md)
before enabling unattended jobs.

These guarantees are exercised on macOS and Linux across Python 3.10–3.14 and
on Windows, including real-Git fault injection around `git push --atomic`. A
dedicated soak repository completed 20 landed trains at a 100% land rate,
including planned conflict recovery and a real killed-push reconciliation whose
verdict matched the remote. See the
[soak evidence](https://github.com/yongjip/mergetrain/blob/main/docs/soak.md),
then use `mergetrain stats --json` to inspect evidence from your own queue.

## Go deeper

- Start: [Quickstart](https://github.com/yongjip/mergetrain/blob/main/docs/quickstart.md) ·
  [Install](https://github.com/yongjip/mergetrain/blob/main/docs/install.md) ·
  [CLI](https://github.com/yongjip/mergetrain/blob/main/docs/cli.md) ·
  [Config](https://github.com/yongjip/mergetrain/blob/main/docs/config.md)
- Understand: [Design and architecture](https://github.com/yongjip/mergetrain/blob/main/docs/design.md) ·
  [Machine contract](https://github.com/yongjip/mergetrain/blob/main/docs/contract.md) ·
  [PR workflow comparison](https://github.com/yongjip/mergetrain/blob/main/docs/pr-workflows.md)
- Operate: [Efficient operation](https://github.com/yongjip/mergetrain/blob/main/docs/best-practices.md) ·
  [Failure modes and recovery](https://github.com/yongjip/mergetrain/blob/main/docs/failure-modes.md) ·
  [Daemon](https://github.com/yongjip/mergetrain/blob/main/docs/daemon.md) ·
  [Multi-repo Hub](https://github.com/yongjip/mergetrain/blob/main/docs/hub.md)
- Trust and extend: [Security](https://github.com/yongjip/mergetrain/blob/main/docs/security.md) ·
  [Agent contract](https://github.com/yongjip/mergetrain/blob/main/docs/agent-contract.md) ·
  [MCP server](https://github.com/yongjip/mergetrain/blob/main/docs/mcp.md) ·
  [Adapter pattern](https://github.com/yongjip/mergetrain/blob/main/docs/adapter-pattern.md) ·
  [Product scope](https://github.com/yongjip/mergetrain/blob/main/docs/product-scope.md)

## Status

The current release is `v1.3.0`. Machine-contract major 1 is additive-only:
existing JSON keys are not removed or renamed without a contract-version change.
Issues and operating reports are welcome on
[GitHub](https://github.com/yongjip/mergetrain/issues).

## License

Released under the [MIT License](https://github.com/yongjip/mergetrain/blob/main/LICENSE).
