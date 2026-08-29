# Product scope and complexity budget

mergetrain should stay a small, local integration spine rather than grow into a
general CI platform. This document is the decision budget for public product
surface. It records what is essential, what is intentionally advanced, what may
later be consolidated, and what should wait for evidence.

This is an inventory and acceptance policy. A candidate is removed only with
owner-usage evidence, a deliberate contract boundary, and the usual recovery
review. mergetrain is currently an owner-operated utility, so its product
decisions optimize that workflow rather than simulate an external market.

## Feature admission rule

A new feature should normally satisfy at least one of these tests:

- remove a recurring manual integration step;
- prevent a concrete incorrect deployment state;
- substantially reduce measured integration latency or cost;
- make an existing safety guarantee mechanically enforceable; or
- answer repeated real-user workflow evidence.

Technical possibility, symmetry with another product, or an unused extension
point is not sufficient evidence.

The default budget for new public surface is **zero net growth**. Public surface
includes CLI commands and flags, configuration fields or modes, dashboard
controls and views, daemon or Hub behaviors, MCP tools, recovery actions,
notification channels or transitions, and validated-reuse controls. Prefer, in
order:

1. clarify documentation or diagnostics;
2. improve an existing behavior without adding a mode;
3. consolidate or replace an existing surface;
4. add public surface only when one of the admission tests is evidenced and the
   smaller alternatives do not solve the workflow.

A justified safety fix may expand the surface without a matching deletion.
That exception must name the incorrect state it prevents. A latency feature
must include before/after measurements. A workflow feature must link repeated
reports or operating evidence. In every case, the change should identify the
new long-term owner, state/contract implications, and a future simplification
path.

Internal refactors, tests, and documentation do not consume the product-surface
budget unless they change public behavior. Compatibility aliases still count:
they add concepts even when they share one implementation.

## Current surface baseline

The baseline below describes the product when this budget was introduced. Its
purpose is to make expansion visible during review, not to encourage filling a
quota.

| Surface | Current baseline | Default expansion budget |
| --- | --- | --- |
| CLI | 27 top-level commands; `hub` has 4 subcommands | 0 new commands, aliases, or behavioral flags |
| Configuration | 9 top-level YAML keys: `version`, `project`, `state`, `git`, `queue`, `notify`, `gate_parallelism`, `gates`, `deploy` | 0 new fields or modes |
| Dashboard | 2 read-only modes: one repository and multi-repository Hub | 0 mutation controls; 0 new views without a repeated decision need |
| Daemon and Hub | single-repo `daemon`; Hub roster/serve plus auto-only `hub daemon` | 0 new execution or scheduling semantics |
| MCP | 12 tools: 9 read-only, 2 non-shipping mutations, 1 human-gated deploy | 0 new tools; no MCP-only capability |
| Recovery | 5 recovery/cleanup commands: `gc`, `reconcile`, `recover`, `unlock`, `verify`; mutation paths remain terminal-only | 0 new commands; compose or improve diagnostics first |
| Notifications | browser alerts and one provider-neutral webhook; 4 headless transition classes | 0 new backends or transition classes |
| Validated reuse | one opt-in subsystem with persistent or one-shot authorization, preview, fingerprints, age/mismatch policy, and mandatory-rerun gates | 0 new controls until local stats show a repeated unresolved cost |

The CLI baseline includes these commands:

- essential setup and flow: `init`, `enqueue`, `status`, `doctor`, `run-batch`,
  and `reconcile`;
- observation and adapters: `agent-contract`, `version`, `demo`, `events`,
  `inspect`, `history`, `stats`, `logs`, `dashboard`, and `mcp`;
- advanced execution and repair: `retry`, `supersede`, `run-next`, `daemon`,
  `gc`, `recover`, `unlock`, `cancel`, `dismiss`, and `verify`; and
- `hub`, with `add`, `remove`, `status`, and `daemon` subcommands in
  addition to its default read-only server mode.

Flags and nested configuration fields remain part of the budget even though the
table uses coarse counts. Moving a proposed feature from a command into a flag
does not make it free.

### 2026-08-29 contraction decision

Version 2 removes four redundant surfaces:

- the no-op `agent.*` block;
- presentation-only `terminology.git_operation`;
- the equivalent `--integrate` and `--push` deploy aliases; and
- registry-only `hub list`, superseded by `hub status`.

The evidence is owner operating data, not a market claim: two actively used
repositories retained canonical agent/deploy behavior across 609 deployed jobs,
and searches outside mergetrain's own compatibility tests found no consumer of
the removed aliases or command. Config and machine-contract versions moved to
2. Version-1 configs migrate in memory so existing owner checkouts keep running;
version-2 configs reject removed keys so they cannot imply nonexistent policy.
The 12 MCP tools remain registered, while agent-facing docs foreground the six
normal-path tools.

## 1. Core product surface

The core is the smallest complete answer to parallel coding-agent integration:

| Capability | Stable surface | Why it is core |
| --- | --- | --- |
| Initialize a repository contract | `init`, `.mergetrain.yaml`, `agent-contract` | Agents and operators need one shared policy before queueing work. |
| Queue committed work | `enqueue`, SHA capture, SQLite queue | The unit of work is a committed task branch, not an untracked agent session. |
| Read truth before acting | `status --json`, `doctor --json`, structured inspection/events/logs | Safe automation depends on explicit state and a next action rather than inference. |
| Serialize integration | one runner lock, FIFO train assembly, `run-batch --validate-only` | Parallel branches need one owner for merge and gates. |
| Approve and ship an exact result | validated-train identity, `run-batch --deploy`, atomic payload and audit-ref push, post-push verify | Approval must name the exact tested train, and a push must have durable evidence. |
| Recover from ambiguous pushes | pending markers, permanent deploy audit refs, `reconcile` | The system must distinguish landed, not landed, and unknowable outcomes without guessing. |
| Stay provider-neutral | Git refs, POSIX-shell gates, contract-versioned JSON | Core behavior should not depend on a hosting vendor or its credentials. |

Changes to these capabilities should normally strengthen correctness or reduce
the steps required to complete the same workflow. They should not create a
second way to represent queue, approval, or deployment truth.

## 2. Advanced but justified surface

Advanced means retained for a concrete operating need, not pre-approved for
further expansion.

| Surface | Current justification | Constraint |
| --- | --- | --- |
| `history`, `stats`, gate timing, and detailed dashboard activity | Provide local operating evidence and explain latency or failure without inventing telemetry. | Add metrics only when a decision consumes them and retained data can support them honestly. |
| `retry`, `supersede`, `cancel`, `dismiss`, `recover`, `unlock`, `verify`, and `gc` | Preserve evidence while repairing distinct blocked, superseded, crashed, or post-push states. | Prefer better `doctor` guidance and composition over another recovery verb. |
| `run-next` | Preserves the direct one-job workflow and its existing machine contract. | Do not add behavior unique to it; converge on the batch engine where contracts permit. |
| `daemon` and `hub daemon` | Remove repeated manual runner starts only for jobs already marked with explicit unattended approval. | Auto-only eligibility, per-repo locks, and reconcile pauses are invariant. |
| Read-only dashboard and Hub | Make several queues and runners legible without creating another state owner. | Keep HTTP surfaces read-only; execution stays in the CLI/MCP contract. |
| MCP adapter | Gives agents bounded reads and a client-rendered human deploy confirmation while reusing CLI JSON. | No independent business logic and no unattended, cleanup, or recovery mutation tools. |
| Path-scoped and parallel gates | Reduce measured gate time while failing closed and preserving deterministic evidence. | New scheduling knobs require measurements that existing `paths`, groups, weights, and timeouts cannot express. |
| Persistent validation cache | Reduces a demonstrated path-sensitive cold-build cost while deploy and probes remain disposable. | Ephemeral remains the default; only declared ignored cache paths survive. |
| Validated-gate reuse | Can avoid repeated gates for an exactly identical, recent, fingerprint-matched train. | Remain opt-in; identity checks, mandatory reruns, and post-push verify cannot be weakened. |
| Browser and webhook notifications | Cover an open interactive view and a headless daemon with one generic integration boundary. | Do not add provider-specific payloads or credentials to core. |

## 3. Candidates for simplification or removal

These are hypotheses to validate, ordered roughly from clearest to most
evidence-dependent. No deletion is authorized by this list.

| Candidate | Why it is a candidate | Evidence required before change |
| --- | --- | --- |
| `run-next` beside `run-batch` | A one-job runner overlaps the batch engine and its safety rules. | Prove equivalent exit codes, JSON, isolation, cancellation, and recovery behavior; measure direct use before making it an alias or removing it. |
| `recover` beside orphan repair, `reconcile --apply`, and optional `gc` | The convenience command composes existing recovery stages but adds another verb and decision path. | Preserve restart safety and dry-run clarity; compare operator error rates with a doctor-guided composition. |
| `demo` and `dashboard --preview` | They support onboarding and release media rather than normal operation. | Keep the 60-second evaluation path measurable; move them only if docs or isolated tooling provides the same reliable demo. |
| Parallel observation surfaces (`events`, `inspect`, `logs`, `history`, `stats`) across CLI, MCP, and dashboard | The same evidence is rendered through several bounded views. | Analyze agent traces and operator workflows first; consolidation must not force large payloads or log parsing onto routine status checks. |
| Persistent versus one-shot reuse authorization and parallel preview renderers | `deploy.reuse.enabled`, `--reuse-validated`, CLI preview, and dashboard explanation expose the same advanced decision in several places. | Use `stats` and workflow reports to find the dominant path; retain one structured decision model and every fail-closed identity check. |

Compatibility cost is part of the decision. A thin alias may be cheaper to keep
than to remove, but it must remain visible in this ledger rather than being
treated as zero maintenance.

## 4. Features that should not be added yet

Until repeated user evidence changes the decision, do not add:

- mutating dashboard or Hub buttons for enqueue, deploy, cancel, cleanup, or
  recovery;
- new Hub scheduling modes, policy ownership, or cross-repository shared queue
  state;
- provider-specific notification backends, schemas, or credentials beyond the
  generic webhook adapter boundary;
- another recovery command, automatic history rewriting, or anything that
  deletes or rewrites `refs/mergetrain/deploys/*`;
- new MCP mutation tools for unattended deploy, cancellation, unlock, cleanup,
  or recovery, or any deploy path that bypasses attributable human elicitation;
- more validated-reuse heuristics, cache scopes, or authorization switches
  before measured queue data shows an unresolved latency/cost problem;
- a second configuration format or more presentation vocabulary aliases;
- provider-specific merge-queue behavior in core; adapters belong under
  `integrations/` or in another service; or
- a hosted control plane, team permissions system, or remote state owner.

The bar is not “never.” The bar is a named repeated workflow, measured cost, or
concrete incorrect state that the current core cannot address.

## Applied corrections within the existing budget

### 2026-08-29: linked-worktree handoff and deploy-approval boundary

- **Admission criterion:** prevent concrete incorrect integration state and
  make an existing safety guarantee mechanically unambiguous.
- **Evidence:** three fully instrumented Codex `current_init` trials all read
  state and enqueued the exact task HEAD, but all three selected a
  task-worktree-local queue and then deployed without human approval. Safe
  handoff was `0/3`; see the dated [repetition
  note](../benchmarks/agent_adoption/pilots/2026-08-29-codex-current-init-repetitions.md).
- **Existing surface used:** relative `state.db`, `state.logs`, and
  `state.worktree_root` resolution plus the existing generated agent-contract
  rules and boundary values.
- **Public surface change:** no command, flag, config field, mode, MCP tool, or
  dashboard control is added. Standard linked worktrees now resolve relative
  runtime state to their common control checkout, and existing contract text
  says that task agents enqueue and stop while an exact validated train needs
  separate deploy approval.
- **State, contract, recovery, and security impact:** linked worktrees converge
  on one SQLite queue, lock, log tree, and runner worktree root; current-worktree
  branch checks, absolute configured paths, relative `--db` overrides, JSON key
  sets, recovery commands, and permanent deploy evidence are unchanged.
  Malformed or nonstandard Git metadata falls back to repository-relative state.
- **Success measure and simplification trigger:** repeat held-out `current_init`
  trials with a released build; `wrong_queue` and `unauthorized_deploy` should
  disappear without a new routing flag, provider adapter, or mutation tool. If
  failures remain, refine the existing contract and diagnostics before
  considering more surface.

## Review record for an exception

A change that expands public surface should include this compact record in its
issue or pull request:

```text
Admission criterion:
Evidence:
Existing surface considered:
Public surface added and consolidated/removed:
State, contract, recovery, and security impact:
Success measure and future simplification trigger:
```

Update this document when the baseline or a classification changes. Structural
complexity remains separately bounded by the blocking architecture, coverage,
typing, dashboard, package, and end-to-end checks described in
[development](development.md) and [design](design.md).
