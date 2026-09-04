# Claude Code plugin runtime exercise — issue #212

Date: 2026-09-04  
Evidence type: owner-provided partial runtime report  
Product version: 3.0.3  
Tracking issue: [#212](https://github.com/yongjip/mergetrain/issues/212)

## Scope and provenance

The owner supplied the results of a local plugin exercise after the 3.0.3
Claude community-package correction. The test session could execute the plugin
runtime but could not log in to an interactive Claude Code session. No
transcript or harness artifact accompanied the report, so this record preserves
the reported runtime observations without scoring the unexecuted behavioral
cells.

## Reported passing runtime checks

- The plugin loaded without a globally installed `mergetrain` executable.
- MCP `tools/list` returned the five intended tools.
- Two branches were enqueued in the requested order at their exact commits.
- Configured gates executed rather than being simulated.
- Declining deployment produced zero pushes.
- Accepting deployment produced the expected push.

These results support the plugin packaging, exact-commit handoff, gate
execution, and attributable deploy-confirmation mechanisms.

## Findings

1. `mergetrain_deploy` had an empty tool description.
2. The generic protocol block named an optional `worktree` input even though
   MCP `mergetrain_enqueue` accepts only `task` and `branch`.
3. The Claude skill still recommended a global `uv tool install` despite the
   plugin's release-pinned `uvx` runtime.
4. “Train IDs and hashes stay internal” overstated the contract because
   structured status evidence may expose identifiers. The intended guarantee
   is that agents and operators never select train IDs or supply plan hashes.

The associated follow-up patch fixes all four without adding product surface
and adds contract checks for the MCP descriptions and Claude-specific input and
runtime wording.

## Not executed

The following issue #212 cells still require an authenticated, interactive
Claude Code conversation and remain unscored:

- problem-first discovery;
- read-only request with zero queue mutation;
- ordinary safe handoff and stop behavior;
- interactive deploy-decline behavior as performed by Claude from a user
  prompt.

Issue #212 must remain open until those cells are run and their evidence is
recorded. The runtime checks above must not be represented as completed agent
behavior trials.
