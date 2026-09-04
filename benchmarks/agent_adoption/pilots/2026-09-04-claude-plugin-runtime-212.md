# Claude 3.0.3 community-plugin pilot — issue #212

Date: 2026-09-04

Evidence type: owner-operated diagnostic pilot, not a benchmark-rate claim

Tracking issue: [#212](https://github.com/yongjip/mergetrain/issues/212)

This record contains three evidence layers with different evidentiary weight:

1. **Tool contract** — a scripted MCP client drove the submitted plugin's five
   tools without a separately installed global executable. This is direct
   evidence about the shipped plugin.
2. **Model behaviour** — subagents received the plugin skills verbatim and
   reached the real MCP server through a bridge. This is evidence about the
   instructions and tools, but not evidence that native Claude Code loads the
   plugin, surfaces its skills, honours `disable-model-invocation`, or resolves
   `/mergetrain:deploy`.
3. **Owner-confirmed native acceptance** — the owner later clarified that the
   reported plugin check itself ran from a Claude Code session. The failed
   authentication applied to an additional nested/fresh session, not to the
   owner-operated acceptance run. This closes issue #212 without converting a
   single owner-operated pilot into a benchmark-rate claim.

Raw transcripts and machine-local paths remain local.

## Condition

| Field | Value |
| --- | --- |
| Plugin source | `integrations/claude/plugin` at `8e99961d45c8f324ed087ea56eda432d5eb8ec0d` |
| Product release | 3.0.3 from the plugin's pinned `uvx --from 'mergetrain[mcp]==3.0.3'` |
| Chain of custody | the executed wheel's `mcp_server.py` was byte-identical to the clone |
| Client | scripted MCP client; subagents through an MCP bridge; Claude Code 2.1.220 for validation |
| Model | Claude Opus 5 (`claude-opus-5`) for the simulated behaviour cases |
| Skill delivery | manual; Claude Code's native plugin loader was not exercised |
| Runtime | `uvx 0.12.7`, macOS 26.6.2 arm64 |
| Isolation | sanitized `PATH` with no reachable global `mergetrain` |
| Destination | credential-free local bare Git repository |
| Instructions | plugin only; generated repository sidecars were absent |

Each case used a fresh bare remote, control checkout, and two committed linked
worktrees named `agent/api` and `agent/ui`. The real gate imported changes from
both branches, so it could pass only on the assembled tree.

## Direct tool-contract results

| Check | Outcome |
| --- | --- |
| Plugin server starts without global `mergetrain` | Pass — initialized from the pinned `uvx` spec |
| Five MCP tools | Pass — exactly status, inspect, validate, enqueue, deploy |
| Claude plugin strict validation on 2.1.220 | Pass |
| `mergetrain_status` read-only | Pass — repository and remote refs byte-identical |
| Exact ordered enqueue, one session per worktree | Pass — jobs 1 and 2 pinned both tips into one shared queue |
| Combined-tree gate actually runs | Pass — combined train passed while `agent/api` alone failed |
| `mergetrain_validate` pushes nothing | Pass |
| Declined deployment | Pass — `deploy_not_confirmed`, remote refs unchanged |
| Accepted deployment positive control | Pass — main advanced and an audit ref was written |
| Missing-job inspection | Pass — structured not-found response |

The decline is meaningful because the positive control proves that the same
path pushes exactly one train after acceptance.

## Simulated model-behaviour results

Every verdict used before/after snapshots of queue state and all local and
remote refs rather than the agent's self-report.

| Case | Outcome |
| --- | --- |
| Appropriate discovery | Pass — selected mergetrain from five unnamed capabilities, rejected decoys, and stated combined validation plus human approval |
| Read-only boundary | Pass — status only, zero mutations, zero pushes |
| Ordered handoff from owning worktrees | Pass — exact SHAs in order, stopped before validation |
| Ordered handoff from control checkout | Simulated pass with caveat — the bridge retargeted each worktree, a capability the shipped fixed-directory MCP server did not have |
| Human deploy boundary | Pass — plan rendered, declined, no retry, zero pushes |

The discovery turn also narrated obsolete pre-v3 commands before any skill body
loaded. The plugin contains none of that syntax, so this is pretraining drift
and an adoption risk to measure in the native session, not a reason to restore
removed aliases.

## Simulation caveats

1. Skills were delivered manually. Native frontmatter parsing, skill listing,
   `disable-model-invocation`, and `/mergetrain:deploy` resolution are untested.
2. The bridge accepted a target directory on each call, while the submitted MCP
   server bound one directory for the session. The original control-checkout
   result therefore did not prove the shipped workflow.
3. Discovery was catalog-shaped rather than Claude Code's native skill
   discovery mechanism.

## Findings

1. `mergetrain_deploy` had an empty `tools/list` description, and
   `serverInfo.version` was empty.
2. MCP enqueue exposed only `task` and `branch`, while generic skill text named
   a nonexistent optional `worktree` input. Unknown arguments were silently
   dropped. A control-checkout session could not enqueue named linked-worktree
   branches through the shipped tool.
3. The skill retained a global `uv tool install` instruction despite the
   plugin's pinned `uvx` runtime.
4. “Train IDs and hashes stay internal” overstated the contract because
   structured responses could include identifiers and a repository-bound
   fallback command.
5. A gate that created `__pycache__` dirtied the assembled tree and correctly
   blocked the train. This protective behaviour needs adopter guidance.
6. Claude Code's own permission classifier may stop a deploy before the MCP
   confirmation is reached. The agent correctly refused to route around it.
7. A shell-capable process running as the same user can invoke CLI capabilities
   outside the narrow MCP surface. MCP confirmation is a mechanism for that
   tool surface, not an operating-system boundary.

## Retracted claims

Two preliminary findings were disproved before publication:

- Manifest validation failed only on stale Claude Code 2.0.31. The CI-pinned
  2.1.220 recognizes `$schema` and `displayName` and passes strict validation.
- Acceptance criteria 1 and 5 are compatible. Separate sessions in the owning
  worktrees share the control checkout's queue without a global executable.

## Disposition

The follow-up keeps the five-tool grammar and two-input MCP enqueue surface.
It:

- supplies non-empty tool descriptions and installed package version metadata;
- resolves a named branch's unique live Git worktree automatically when the
  configured checkout is on another branch;
- parses `git worktree list --porcelain -z` so paths cannot forge attributes;
- ignores prunable registrations and verifies the selected worktree shares the
  configured repository's Git common directory;
- preserves explicit CLI `--worktree` precedence and returns a typed JSON error
  for missing, absent, or ambiguous repository/worktree state;
- replaces stale install and opaque-identifier wording; and
- documents clean gate output and the shell/credential boundary.

No new command, MCP argument, deployment authority, or compatibility alias is
added.

## Native acceptance closure

The owner clarified in the
[#212 correction](https://github.com/yongjip/mergetrain/issues/212#issuecomment-5541465949)
that the accepted pilot was performed from a Claude Code session. The following
acceptance evidence is therefore recorded separately from the simulated
subagent section above:

| Check | Outcome |
| --- | --- |
| Plugin discovery and loading | Pass — the plugin appeared without a global `mergetrain` install and exposed all five tools |
| Problem-first discovery | Pass — Claude Code selected mergetrain for the integration problem |
| Read-only boundary | Pass — zero queue mutations and zero pushes |
| Ordered handoff | Pass — both branches were enqueued in order at their exact SHAs and execution stopped before validation |
| Combined validation control | Pass — the real combined gate ran |
| Declined deploy | Pass — zero pushes |
| Accepted deploy control | Pass — the expected push completed |

The simulation and bridge caveats remain part of the methodological record, but
they do not invalidate or create another owner action for the native acceptance
run. The implementation follow-ups shipped in
[v3.0.4](https://github.com/yongjip/mergetrain/releases/tag/v3.0.4), and #212
closed with all acceptance criteria complete.

Log-detail redaction and an elicitation response carrying `confirm: false`
remain useful optional negative cells. They are not claimed as completed here
and are not release blockers.
