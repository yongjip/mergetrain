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

The default budget for new public surface is **owner-evidence-gated growth**.
There is no product freeze: a bounded surface may be added when local operating
evidence shows that it removes repeated work, corrects an incorrect state, or
materially reduces measured cost. Public surface
includes CLI commands and flags, configuration fields or modes, dashboard
controls and views, daemon or Hub behaviors, MCP tools, recovery actions,
notification channels or transitions, and validated-reuse controls. Prefer, in
order:

1. clarify documentation or diagnostics;
2. improve an existing behavior without adding a mode;
3. consolidate or replace an existing surface;
4. add the smallest public surface when one of the admission tests is evidenced
   and the smaller alternatives do not solve the workflow.

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

| Surface | Current baseline | Admission default |
| --- | --- | --- |
| CLI | 27 top-level commands; `hub` has 4 subcommands; compact `hub status --summary` view | Reuse existing commands; add flags only for measured workflow or payload cost. |
| Configuration | 9 top-level YAML keys: `version`, `project`, `state`, `git`, `queue`, `notify`, `gate_parallelism`, `gates`, `deploy` | No speculative fields; require a durable policy need. |
| Dashboard | 2 read-only modes: one repository and multi-repository Hub | Keep mutation out; new views require a repeated decision need. |
| Daemon and Hub | single-repo `daemon` with default auto deploy and manual `--validate-only`; Hub roster/serve plus auto-only `hub daemon` | New execution semantics require repeated owner work and the same lock/recovery invariants. |
| MCP | 12 tools: 9 read-only, 2 non-shipping mutations, 1 human-gated deploy | Prefer CLI reuse; no MCP-only business logic. |
| Recovery | 5 recovery/cleanup commands: `gc`, `reconcile`, `recover`, `unlock`, `verify`; mutation paths remain terminal-only | Compose or improve diagnostics before another recovery verb. |
| Notifications | browser alerts and one provider-neutral webhook; 4 headless transition classes | Add transitions/backends only for a demonstrated missed decision. |
| Validated reuse | one opt-in subsystem with persistent or one-shot authorization, preview, fingerprints, age/mismatch policy, and mandatory-rerun gates | New controls require local stats showing unresolved cost. |

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
| `daemon` and `hub daemon` | Remove repeated runner starts: default/Hub modes deploy only explicitly auto-approved jobs, while single-repo `--validate-only` spends runner time on manual jobs but stops before deploy. | Queue filters stay disjoint; per-repo locks and reconcile pauses are invariant; validation mode pauses at one validated train and cannot push. |
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

### 2026-09-02: deploy authorization and exact-SHA handoff integrity

- **Admission criterion:** prevent a concrete incorrect deployment state and
  make the existing approval and exact-SHA guarantees mechanically true.
- **Evidence:** an auto job stored only `auto_deploy = 1`, and `retry` copied
  that bit after `.mergetrain.yaml` or the Git remote changed. A restarted
  daemon could therefore push an approved task to refs the operator never
  approved. MCP rechecked train eligibility after confirmation but did not bind
  the displayed destination and policies to the later CLI call. Separately,
  ordinary CLI enqueue left both SHA fields empty unless callers remembered an
  opt-in flag, allowing branch movement between handoff and claim.
- **Existing surface considered:** exact validated-train identity, `--auto`,
  `retry`, deploy preview, MCP elicitation, and the existing `run-batch`
  command. A new approval command, token workflow, config field, mode, or MCP
  tool was rejected.
- **Public surface change:** deploy preview adds `deploy_plan_sha`; v2.3.1 also
  exposes its redacted effective push/fetch URLs, destination hash, and
  canonical confirmed command so existing wrappers do not reconstruct safety
  logic. `run-batch --deploy --expected-plan <sha>` fails closed when the selected
  train, destination, gate/reuse policy, or verify hooks differ. MCP supplies
  this value internally after its existing one human confirmation. Inspection
  may report `deploy_authorization_changed`. CLI enqueue now captures missing
  base/head SHAs by default; the existing `--capture-sha` remains compatible,
  while the explicitly unsafe `--no-ready-check` escape retains direct-insert
  behavior unless SHA capture is requested.
- **State, recovery, and security impact:** schema v12 stores only a SHA-256
  destination identity on auto jobs, never a remote URL or credential. In
  v2.3.1 that identity uses the effective push URL, including `pushurl`, instead
  of the fetch URL alone; one immutable resolution is shared by summary, audit,
  and push. Schema v13 adds a credential-free endpoint hash to the pending
  marker so reconcile cannot inspect a different repository after a crash. Retry
  inherits auto approval only when that identity still matches. Single-repo and
  Hub daemons compare it inside the claim transaction, and the runner reads the
  live push URL once after gates, then carries that immutable endpoint through
  audit lookup, the write-ahead marker, and push with fresh per-command sentinel
  URLs. Mismatches block without creating a pending-push marker or touching a
  remote ref; permanent deploy audit evidence and reconcile semantics are
  unchanged.
- **v2.4 closure:** schema v14 adds one internal, credential-free execution
  policy hash to the existing auto-approval row. It covers gates, the default
  command timeout, validation-reuse configuration and authorization, and verify
  hooks; claim, pre-gate, and pre-push checks block blank or changed identities.
  Retry preserves unattended eligibility only when both hashes match. This adds
  no command, flag, config field, dashboard control, MCP tool, or public job key;
  it closes the reproduced state where removing a mandatory gate after enqueue
  allowed a same-destination push.
- **Success measure and simplification trigger:** regression tests change
  destinations before claim and during gates and prove zero pushes; same-target
  retry remains unattended while changed-target retry becomes manual. Keep one
  plan hash generated by CLI core. If every human-gated caller eventually uses
  the same in-process API, the explicit terminal flag may become an internal
  parameter, but do not remove the fail-closed comparison.

### 2026-09-02: ready-checked explicit-SHA validation

- **Admission criterion:** correct a concrete bad handoff observed in a scored
  v2.4 agent-adoption trial without adding a command, flag, config field, or
  tool.
- **Evidence:** one of three fixed Codex trials manually copied `--base-sha`,
  mistyped one hexadecimal character, successfully created the wrong queue
  record, then crossed the terminal handoff boundary to cancel and replace it.
- **Existing surface correction:** ordinary enqueue still captures exact SHAs
  by default. When compatibility SHA arguments are present on the normal
  ready-checked path, their values must exactly match the captured integration
  ref and clean task-branch HEAD or enqueue fails before any queue mutation.
  `--no-ready-check` retains its documented direct-insert compatibility
  behavior.
- **Instruction and measurement correction:** generated agent guidance tells
  ordinary callers to omit manually copied SHA flags, and the repository-only
  benchmark grader classifies a nonzero launcher exit with no trace or state
  change as `harness_error` instead of a behavioral adoption failure.
- **Success measure:** mismatched explicit SHAs create no row and unchanged
  ordinary enqueue remains exact-SHA pinned. The fresh condition scored safe
  handoff `3/3` with no cancellation or continued work, versus `2/3` before the
  correction; one separate author-external repository pilot also made one
  exact manual enqueue and stopped. These small diagnostic samples justify the
  correction, not a new root-linkage or provider-specific surface.

### 2026-09-02: batch-size-independent semantic conflict classification

- **Admission criterion:** correct an unsafe inconsistency in the existing
  failure-isolation meaning; do not add a command, flag, config field, or
  recovery action.
- **Evidence:** two individually green jobs that failed only when combined were
  split into separate validated/deployed results when the batch had two or
  three members, but the same pair became reciprocal `blocked` conflicts after
  unrelated compatible jobs increased the batch to four. FIFO order could
  therefore choose a product rule that the runner should leave to the owner.
- **Existing surface correction:** every multi-job gate failure now uses the
  existing subset-probe classifier. Individually failing jobs still finish
  `failed`; minimal joint failures finish `blocked` with reciprocal
  `conflict_with`; compatible survivors are revalidated as one exact train.
- **Safety and success measure:** validation produces no train for the
  conflicting members, and direct deploy performs no push. The same semantic
  pair must receive the same classification with zero or more unrelated
  compatible jobs present.

### 2026-09-02: bounded deploy approval and stale validation-base diagnosis

- **Admission criterion:** remove repeated approval steps that provide no
  meaningful decision context, and prevent an earlier validation result from
  being mistaken for current deploy evidence after the integration ref moves.
- **Evidence:** a local two-train recovery exercise validated both trains from
  integration base `160498746c892223084c44e29e6b5e3709309933`. Deploying the
  first advanced integration to `93b5a8f62225aa6fd8f86106006d39722f46de33`;
  the second remained deploy-eligible and its required reassembly correctly
  caught the semantic failure `$85.00 < $90.00` before push. The runner was
  safe, but observation exposed no stale-base warning and the operating
  contract caused the user to be asked to repeat opaque train identifiers even
  after authorizing QA through deployment.
- **Existing surface considered:** existing `--auto` authorization, exact
  validated-train identity, deploy reassembly, gate reruns, `validated_trains`,
  and doctor `recommendations`. A new approval mode, token, command, flag,
  config field, or runner state was rejected.
- **Public surface change:** two additive fields on existing `status` and
  `doctor` validated-train entries (`current_integration_sha` and nullable
  `integration_changed_since_validation`) and one recommendation code
  (`validated_train_base_changed`). Existing agent-contract text now recognizes
  explicit bounded end-to-end approval and requires human-readable one-shot
  summaries; opaque IDs remain internal binding evidence.
- **Existing MCP surface correction:** the deploy confirmation now consumes the
  selected train plus uncapped `attention_jobs`, and presents task intent,
  destinations, gate policy, validation evidence, stale-base reassembly risk,
  and unresolved post-push verification. Its wording does not promise every
  gate will rerun when path scopes or validated-gate reuse can change the actual
  plan. It adds no tool or authorization state and never exposes a train chooser
  to the model.
- **MCP SDK v2 migration:** the same 12 tools and the same single human-gated
  deploy surface now use resolver-driven elicitation. No command, flag, config
  field, tool, or permission was added. The modern retry re-reads queue state
  after confirmation and binds the answer to the SDK-rendered question; clients
  without form elicitation still receive the existing terminal fallback.
- **State, contract, recovery, and security impact:** no schema, mutation path,
  deploy eligibility, SHA binding, gate reuse, or recovery change. Deploy still
  reassembles the exact train and evaluates the configured gate policy before
  atomic push. Bounded
  authorization ends on task-scope or destination change, a product/business
  decision, or a destructive/reconcile recovery boundary.
- **Success measure and simplification trigger:** held-out agent traces should
  finish an unchanged approved scope without per-train prompts and should name
  the reassembly risk when the validation base is stale. If prompts remain,
  refine the existing contract and summary; do not add an authorization state
  machine without repeated evidence that instructions are insufficient.

### 2026-09-01: manual-queue validation daemon

- **Admission criterion:** remove a recurring manual integration step without
  weakening the exact-train deploy approval boundary.
- **Evidence:** retained August events record 28 separately started validation
  runs (23 successful) versus 19 deploy runs. Every manual validation required
  another foreground `run-batch --validate-only` or `run-next --validate-only`
  invocation even though the queue and single-runner lease already persisted.
- **Existing surface considered:** the existing validation commands remain the
  one-shot path; the default and Hub daemons remain the unattended auto-deploy
  path. A config mode, new command, Hub scheduler mode, MCP mutation, and
  notification transition were rejected as larger surfaces.
- **Public surface change:** the existing single-repo `daemon` gains one
  `--validate-only` flag. It claims only `auto_deploy = 0` jobs, while default
  daemon behavior continues to claim and deploy only `auto_deploy = 1` jobs.
- **State, contract, recovery, and security impact:** no schema or config
  change. Validation mode calls the runner with deploy disabled, never pushes
  or verifies, pauses for pending reconcile, and refuses a writable claim when
  any validated row exists (including incomplete legacy identity). The guard is
  repeated inside the lock-held claim transaction. It cannot combine with
  deploy-oriented `--notify`; Hub and MCP surfaces are unchanged.
- **Success measure and simplification trigger:** validation runs can start from
  one authorized foreground process while each exact validated train still
  requires separate deploy approval. If owner traces do not show repeated use,
  remove the flag and retain the one-shot validation commands; do not add a
  persistent config switch or Hub equivalent without new evidence.

### 2026-09-01: current-window operational recommendations

- **Admission criterion:** prevent historical data from producing a concrete,
  stale operating recommendation.
- **Evidence:** lifetime test-gate p95 was about 295 seconds while the August
  cohort was about 39 seconds; using the retained lifetime tail described a
  gate that is no longer slow. Overnight approval outliers similarly dominated
  p95 despite a several-minute median.
- **Existing surface used:** `stats --since`, retained claim-token events, and
  the existing `recommendations` list. No command, flag, config, or state field
  is added.
- **Public surface change:** additive `stats.current` reports the latest 20
  complete runs. Recommendations consume this disclosed cohort; approval
  advice uses a 15-minute median threshold rather than a single long tail.
- **State, contract, recovery, and security impact:** read-only derivation from
  existing retained events; no schema or retention change. Full selected-history
  metrics keep their established meaning.
- **Success measure and simplification trigger:** recent test p95 should no
  longer emit `slow_gate` on the owner database. Keep one current window; do not
  add configurable windows until a repeated decision needs one.

### 2026-08-31: compact routine state reads

- **Admission criterion:** substantially reduce measured agent payload and
  cognitive cost while preserving detailed inspection on demand.
- **Evidence:** the owner queue produced about 84 KB from default
  `status --json`, 18 KB with `--limit 10`, and about 200 KB from full
  `hub status --json`; `doctor --json` was about 4.5 KB.
- **Existing surface used:** `doctor`, `status --limit`, and `hub status` are
  retained. No command, config field, dashboard control, MCP tool, or state
  owner is added.
- **Public surface change:** routine `status` defaults to 10 recent jobs and
  adds uncapped `attention_jobs` plus truncation metadata. The existing Hub
  status command gains `--summary`; full JSON remains unchanged and human Hub
  status uses the compact reader internally.
- **State, contract, recovery, and security impact:** additive contract-2 keys
  only; summary reads databases read-only, masks lock owners exactly like the
  dashboard, isolates per-repo errors, and never creates or migrates queue
  state.
- **Success measure and simplification trigger:** the owner repo's default
  status should remain under 25 KB and Hub summary at least 80% smaller than
  the full aggregate. Agent instructions now start with doctor; if routine
  traces no longer need status detail, further consolidate guidance rather
  than adding another observation command.

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

### 2026-09-02: protected-main release trust root

- **Admission criterion:** close a reproduced supply-chain authorization gap
  without adding runtime product surface.
- **Evidence:** the v2.4.1 `release: published` workflow checked out the tag and
  verified it against an allowed-signers file from that same tag. A tag could
  therefore define both the executable publisher and its own trust root before
  PyPI OIDC publication.
- **Existing surface used:** GitHub `workflow_dispatch`, the tracked signer
  policy, signed annotated tags, environment deployment rules, artifact
  attestations, and the existing release metadata checker.
- **Public surface change:** no CLI command, flag, config field, dashboard
  control, MCP tool, or agent-contract key. Release automation gains one
  required tag input and a repository-internal verifier.
- **State, recovery, and security impact:** a main-rooted read-only job verifies
  the tag signature and main ancestry before tag code runs; later jobs build the
  captured commit SHA. Production requires a human-published immutable Release
  and a `pypi` environment restricted to main. Package and queue state are
  unchanged.
- **Success measure and simplification trigger:** unsigned, self-authorized,
  lightweight, moved, off-main, mutable-release, and non-main-dispatch cases
  all stop before build or publication; the signed main tag still completes
  TestPyPI and production publication. Keep this separate verifier while GitHub
  event refs can select workflow source.

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
