# Eligibility before activation: frozen diagnostic design

Baseline: signed mergetrain v3.0.5 (`dd1269e`). This is a benchmark-only
proposal, not a shipped routing mechanism. The code oracle consumes reviewed
facts; it does not infer facts from natural language or control Codex.

## Objective and logical model

Avoid reading the skill merely to reject it, while retaining prospective
adoption, existing-queue operation, explicit comparisons and recovery help.
Selection never grants permission to initialize, enqueue, validate, deploy or
repair anything. Product execution continues to use the existing protocol.

Apply these rules in order:

1. Respect a user's prohibition on using/reading the capability. Merely naming
   the product inside a prohibition, quote, log or unrelated task is not a request.
2. Select for an explicit product-specific task (including a comparison), or
   a task about an existing mergetrain queue. Mere installation or configuration
   presence is insufficient. A currently empty queue or single remaining branch
   does not invalidate such a task.
3. Exclude administration of an existing hosted PR queue. Merely hosting code
   on GitHub/GitLab is not this exclusion; an explicit migration/comparison is
   covered by rule 2.
4. For prospective adoption, require a local workflow, parallel coding-agent
   branches, and an integration need: ordered assembly, combined validation,
   serialized integration pushes or durable evidence for future push recovery.
   Facts may be established by the user's described workflow, not necessarily
   by exact keywords. A false condition excludes; absent information leaves
   the decision unresolved. Unknown does not mean false or grant license to
   invent evidence. Use available context; ask only if the missing fact matters.

Commit cleanliness is an enqueue precondition, not a discovery precondition.
Installing a new queue cannot retroactively create evidence for an old ordinary
Git push. Generic incident recovery therefore does not qualify on its own.
An unresolved request is not a mandate to run Git or read the skill to reject it.

`policy.py` models these rules over 432 combinations (four booleans and three
three-valued facts). Boundary cases verify precedence and the differences
between mentions/tasks, hosting/hosted queues, current/future recovery, and
adoption/execution preconditions. These tests check the design, not model accuracy.

## Candidate and causal hypothesis

Change only the skill's discovery description. Keep the released body byte-for-
byte unchanged. Put existing-queue and local-agent-integration applicability
first; scope recovery to this integration workflow. Do not add a router skill,
MCP tool, config field, telemetry, or explicit-only policy.

The hypothesis is that naming the requested workflow instead of broad Git
capabilities reduces read-to-reject behavior. No guarantee follows from wording.
Official docs describe description-based implicit matching, progressive loading
and an explicit-only switch, but do not document a semantic pre-load predicate:
https://learn.chatgpt.com/docs/build-skills

## Test protocol frozen before live results

- Baseline and one candidate; do not tune the candidate after viewing outcomes.
- 20 new prospective-fit prompts, 20 new negative prompts, and 8 boundary prompts.
  These author-reviewed diagnostics are not an independently human-reviewed
  replacement for the published corpus. Boundary cases have a separate denominator.
- Paired fresh ephemeral Codex CLI sessions, the same model and reasoning as
  prior pilots (`gpt-5.6-sol`, high); randomize pair order and arm order.
- Use a non-Git disposable directory outside this repository for every run.
  Expose one local skill from v3.0.5, with only its description changed per arm.
  Disable other installed skills per invocation; do not change user installation
  or copy credentials. Verify catalog availability before measured runs.
- This isolates native skill selection; it is NOT a full installed-plugin,
  MCP-catalog, competing-skill, or cross-client benchmark. Record that limitation.
- Keep full stdout/stderr and final answers outside Git, hash the frozen inputs,
  keep labels out of child prompts/workspaces, and report incomplete/invalid
  trials instead of imputing outcomes. Inspect command traces for reads, other
  capability calls, mutation attempts and out-of-workspace contamination.
- Measure actual skill reads separately from named recommendations. Capture
  reported token usage and wall time; local overhead and provider latency are
  included, so timing does not isolate inference savings.
- Review answers and traces; do not treat a substring classifier as a semantic
  grader. One author reviewing their own candidate remains non-independent.
- At most 1/20 negative activations and at least 16/20 suitable selections with
  correct recommendations are necessary to advance. A paired regression in
  boundary behavior or any authority violation rejects the candidate.
- No production promotion from this diagnostic alone. Full installed-plugin
  testing, independent review and a fresh 20-case exact-SHA handoff cell (>=19/20,
  zero authority violations) remain required before changing public copy.
