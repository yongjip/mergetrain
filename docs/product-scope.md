# Product scope and complexity budget

mergetrain is an owner-operated, local-first integration train for coding-agent
worktrees. Its job is narrow: accept committed branches, validate the combined
result, bind approval to an exact destination and policy, atomically push, and
retain enough evidence to recover truth after a crash.

The internal safety engine may be detailed. The product grammar must stay
small.

## v3 surface baseline

| Surface | v2.4.1 | v3 target and baseline |
| --- | ---: | ---: |
| Commands shown by default help | 27 | **6** |
| Normal MCP tools | 12 | **5** |
| Generated config sections | 9 | **2 plus `version`** |
| Always-loaded agent rules | 12 | **5** |
| State entry points | 2 | **1** |
| User-facing state groups | 8+ combinations | **5** |
| Manual deployment grammar | modes, IDs, hashes, preview flags | **`deploy`** |

The six public verbs are:

```text
init  status  enqueue  validate  deploy  inspect
```

The five MCP tools mirror them except initialization:

```text
mergetrain_status
mergetrain_inspect
mergetrain_enqueue
mergetrain_validate
mergetrain_deploy
```

This is now a ceiling, not a launch target. Version 3 has no planned successor
grammar; see [the compatibility policy](contract.md#long-lived-v3-compatibility-policy).

## Why these names are final

| Verb | Decision |
| --- | --- |
| `init` | Conventional one-time repository setup; clearer than `configure` because it writes a scaffold, not live policy. |
| `status` | Conventional read of current state and next action; avoids a second `doctor` or an abstract `state` synonym. |
| `enqueue` | Names the durable handoff and ordering operation; `add` and `submit` hide the queue invariant. |
| `validate` | States the non-pushing guarantee; `test` would understate merge assembly and semantic-conflict attribution. |
| `deploy` | Covers validate, approval, atomic Git update, and verify; `push` is too narrow and `land` is forge-specific. |
| `inspect` | One bounded evidence read for a named job; avoids teaching separate detail commands on the normal path. |

Advanced repair commands remain direct and hidden rather than moving into
`job`, `admin`, or `evidence` namespaces. A namespace would create another
grammar, break existing operator muscle memory, and add no decision value when
`status.next_action.command` already supplies the exact exceptional command.

## Product layers

### Core

- exact-SHA enqueue from a clean task worktree;
- durable FIFO queue and one lease-fenced runner;
- isolated combined-train assembly and semantic-conflict attribution;
- configured gates and optional safe validation reuse;
- exact train, destination, execution-policy, and confirmation binding;
- one atomic payload plus permanent audit-ref push;
- post-push verification and remote-truth reconciliation.

### Optional operator surfaces

- validation and pre-approved deploy daemons;
- evidence streams, history, statistics, cleanup, and repair commands;
- local read-only dashboard and multi-repository Hub;
- generic notifications;
- stdio MCP adapter.

Optional layers may render or schedule core behavior. They may not own queue
truth, create a weaker authorization path, or add provider policy to core.

## Admission test

Before adding a CLI command or option, config field, dashboard control, daemon
mode, Hub behavior, MCP tool, recovery action, notification path, or reuse
control, record all of the following in this file:

1. **Evidence:** a repeated real workflow, measured material cost, or concrete
   incorrect state.
2. **Existing fit:** why `status`, `inspect`, current config, or composition
   cannot solve it.
3. **Decision cost:** the new choice a person or agent must understand.
4. **Safety impact:** authorization, exact identity, lock, push, recovery, and
   failure behavior.
5. **Success measure and removal trigger:** how value will be observed and when
   the surface should disappear.

Missing evidence means no feature. Implementation possibility and speculative
future adoption are not evidence.

## Default decisions

Prefer these in order:

1. improve the output or next action of an existing verb;
2. add an optional field behind a safe code default;
3. consolidate duplicated switches or renderers;
4. keep specialist behavior hidden and state-guided;
5. add a surface only after the admission test passes.

Do not add another normal-path command, state entry point, approval token,
manual identity input, config format, MCP mutation tool, or presentation
vocabulary alias.

## Explicit non-goals

Until external evidence changes the scope, mergetrain will not become:

- a hosted control plane or organization permission system;
- a general job queue, CI provider, or deployment platform;
- a forge-native review UI or provider-specific merge queue;
- a mutating dashboard/Hub control plane;
- a provider-specific notifications or credentials framework;
- an automatic history rewriter;
- a system that deletes or rewrites `refs/mergetrain/deploys/*`;
- a second package merely to isolate the existing dashboard or Hub.

Provider adapters belong under `integrations/` or in a separate service and
must use the same core contract.

## v3 consolidation record — 2026-09-03

### Evidence

The v2.4.1 product exposed 27 top-level commands, 12 MCP tools, two state reads,
machine identity flags in the human deploy path, nine generated config areas,
and twelve always-loaded agent rules. The ordinary workflow still reduced to
enqueue, validate, approve, and push. The mismatch created tool-selection
ambiguity and duplicated safety explanations without adding correctness.

### Changes admitted

- Default help now shows six core verbs.
- `status --diagnose` absorbs `doctor`; `--version` absorbs the version command.
- `validate` and `deploy` replace runner-mode vocabulary.
- `validate` pauses while an exact Ready train exists, so normal v3 operation
  has one approval slot instead of a train-selection workflow.
- `deploy` validates when needed, renders that exact Ready plan, confirms
  interactively, and keeps train IDs and hashes internal.
- `deploy --json` is a non-pushing machine plan; MCP performs the supported
  attributable non-interactive confirmation flow.
- Enqueue SHA inputs, readiness bypasses, duplicate override, routine deploy
  train-ID selection, one-shot preview, and reuse switches are removed. A
  hidden train-ID selector exists only to drain pre-v3 databases that already
  contain multiple Ready trains.
- Reuse authorization has one policy source: committed config.
- `recover` is removed; `reconcile --apply` heals safe stranded claims before
  resolving ambiguous pushes. Cleanup remains separate.
- MCP exposes exactly five tools; events and logs fold into inspect detail.
- Init writes minimal config and five-rule sidecars.
- LLM and Claude instruction surfaces share the same generated protocol block;
  CI rejects removed grammar in high-signal onboarding and wrapper surfaces.
- Status projects Waiting, Running, Ready, Attention, and Done while preserving
  the detailed database state for recovery.

### Deliberate tradeoffs

There are no v2 aliases. Retaining both grammars would keep the ambiguity and
double the compatibility burden. Removed invocations fail with a typed
`removed_interface` error and an exact replacement.

Advanced commands remain callable but hidden. Moving them into new namespaces
was rejected because it would increase grammar and force another migration
without changing how often users decide. The normal path discovers them only
through an exact status recommendation.

The database schema and safety engine were not simplified away. Exact SHA,
combined validation, conflict isolation, destination and policy binding,
atomic push, durable marker, and remote-truth recovery are the product's value,
not accidental complexity.

### Success measures

- default-help command count remains six;
- MCP tool count remains five;
- generated starter config remains under twelve nonblank lines before gates;
- agent sidecar remains five rules;
- ordinary task trials use `status → enqueue → stop`;
- authorized operator trials use `status → deploy` without train IDs or hashes;
- read-only prompts create zero queue mutations;
- deployment/recovery safety regression suites remain green;
- external pilots report discovery or workflow misses before any new surface is
  considered.

## Release freeze after 3.0

After 3.0, core feature work pauses unless a concrete defect violates a stated
guarantee. The next product investment is Tier-2 authority/recovery agent
benchmarks, Claude Code and larger-repository pilots, and observation with
independent developers. Onboarding or UX additions require evidence from those
pilots; new core engine capability does not.

## 3.0.1 projection-correctness record — 2026-09-03

### Evidence

The simplified status projection could classify a deployed job with failed
post-push verification as Done. With mixed attention rows, aggregate counts
selected `fix_blocked_job` while a separately sorted list supplied an unrelated
verification job to the command. Status also recommended enqueue without a
configured remote or resolvable integration ref, created local queue state on a
never-used repository, and enqueue interpreted an omitted worktree from the
process CWD instead of the explicit `--repo` boundary.

### Existing fit and decision cost

These are correctness defects in the existing `status` and `enqueue` verbs.
The repair adds no command, option, config field, database schema, state group,
approval path, or MCP tool. Additive target/reason fields and warnings make the
already-required decision inspectable without introducing a new user choice.

### Safety impact and success measure

One planner now selects the action code, exact target, command, approval class,
and stable reason together. Unknown verification and the latest deployed
generation's known verification failure remain visible; a later deploy
supersedes an older current-health result without deleting its inspectable
evidence. The existing non-pushing `verify --job` path can recheck and clear a
current failure. Git readiness fails closed before enqueue advice, a missing
queue is observed without filesystem creation, and CLI/MCP enqueue use the
bound repository by default. Table-driven projection, mixed-priority,
superseded-health, zero-create status, and out-of-CWD enqueue tests must remain
green. Remove none of these checks from the v3 regression suite.

This paragraph records the 3.0.1 policy as shipped. The 3.0.2 record below
replaces heuristic automatic supersession with explicit resolution after new
evidence showed the heuristic could conflate unrelated deployments.

## Discovery-metadata alignment record — 2026-09-04

### Evidence

The README explains parallel-worktree integration well, but the PyPI, MCP, and
Claude catalog descriptions led with “operate mergetrain” or category language.
Those descriptions only help after a person or agent already knows the product
name. The executable adoption harness also began at `current_init`, so it could
measure protocol execution after discovery but not recommendation from an
unnamed problem. Legacy Gemini CLI is no longer a supported benchmark client;
the active local adapter targets agy (Antigravity CLI).

### Changes admitted

- `discovery/metadata.yaml` owns the problem-first headline, descriptions,
  trigger boundaries, catalog tags, and desired GitHub About values.
- Its MCP Registry projection is separately bounded to the registry's 100-character
  description limit while remaining in the same canonical source.
- CI validates PyPI, MCP Registry, Claude marketplace/plugin/skill, README, and
  LLM-facing summaries against that source.
- A product-name-free fixture corpus separates suitable recommendation, safe
  exact-SHA handoff, and negative-control denominators for Codex, Claude Code,
  and agy.
- The corpus and result contract are scaffolding only. Live provider execution
  remains an external benchmark and adds no runtime telemetry.

### Decision cost and safety impact

No command, option, config field, state, MCP tool, queue behavior, approval
path, or hosted component is added. Ordinary task agents still use
`status → enqueue → stop`; deployment and recovery retain their existing human
authority requirements. The Claude deploy skill remains explicit-only.

### Success measures and removal trigger

- suitable-prompt discovery is at least 80% per supported client;
- false-positive selection is at most 5%;
- exact-SHA safe handoff is at least 95%; and
- direct pushes and unauthorized deployment or recovery attempts remain zero.

Descriptions that do not improve held-out discovery, or that raise false
positives, must be narrowed or removed before adding another distribution
surface.

## agy native-distribution record — 2026-09-04

### Evidence

Legacy Gemini CLI is no longer the active local Google agent path. The existing
benchmark adapter and dated pilot evidence use agy, while agy's current native
plugin format requires a root `plugin.json` and can package skills and an MCP
server. Without that package, users must separately discover both mergetrain and
its integration instructions before agy can select them.

### Changes admitted

- The repository root is an installable agy plugin with `plugin.json`.
- `skills/mergetrain/SKILL.md` carries the same generated protocol and canonical
  discovery description as the Claude surface.
- `mcp_config.json` starts the existing local stdio MCP adapter from the exact
  released wheel through `uvx`.
- Release, discovery, and protocol checks reject manifest, version, or safety
  drift. No hooks, rules, subagents, hosted service, or provider credentials are
  included.

### Decision cost and safety impact

The installation adds one ecosystem choice but no product command, option,
config field, state, queue behavior, or MCP tool. agy receives the same five MCP
tools. Ordinary agents stop after enqueue, and deploy or recovery still requires
the existing human authority. The plugin cannot silently substitute direct Git
integration if `uvx` is unavailable.

### Success measure and removal trigger

The agy discovery cell must meet the shared 80% discovery, 5% false-positive,
95% safe-handoff, and zero unauthorized-mutation gates. Remove or narrow the
plugin if agy changes its native schema, if the pinned MCP launch cannot be
reproduced, or if held-out trials show persistent false activation.

## Codex native-distribution record — 2026-09-04

### Evidence

The repository already shipped a reusable Codex-compatible skill, but Codex's
native plugin catalog could not install it from the repository. That left a gap
between problem-first copy and actual availability: an unknown agent cannot
select a capability that its plugin catalog does not expose.

### Changes admitted

- `.agents/plugins/marketplace.json` exposes one Git-installable marketplace
  entry under `plugins/mergetrain`, which packages the canonical ordinary-agent
  skill.
- The companion `.mcp.json` launches the existing released MCP adapter through
  `uvx`; it adds no server and exposes the same five tools.
- CI validates the marketplace, plugin manifest, canonical copy, exact package
  version, and generated protocol block.

### Decision cost and safety impact

This adds one installation surface, not a product surface: no CLI command,
option, config field, dashboard control, state transition, or MCP tool changes.
Catalog metadata contains both positive and negative triggers. The installed
skill preserves `status → enqueue → stop`; deploy, unattended operation, and
recovery still require their existing human authority.

### Success measure and removal trigger

The Codex discovery cell must meet the shared 80% discovery, 5% false-positive,
95% exact-SHA handoff, and zero unauthorized-mutation gates. Narrow or remove
the marketplace entry if held-out trials show false activation, or if Codex
changes its manifest or Git-marketplace contract.

## 3.0.2 confidentiality and verification-consistency record — 2026-09-04

### Evidence

Compact status copied persisted notes into CLI JSON without the shared secret
redaction or a length bound, and MCP returned that valid JSON unchanged. Status
also assembled counts and job rows across independent SQLite snapshots, so a
concurrent writer could pair a verification action with an unrelated blocked
job. Dashboard clients independently omitted known verification failures from
some Attention calculations.

A deployed train shares one push and one post-push verification result, but
`verify --job` updated only one member. It could also mark that member succeeded
after all verify hooks were removed. Finally, heuristic latest-deploy
supersession could hide an unresolved production failure after an unrelated
deployment, and a legacy duplicate of the built-in integrity gate suppressed
the no-project-gates warning even though it was ignored at runtime.

### Existing fit and decision cost

The repair stays inside the existing `status`, `verify`, dashboard, Hub, and
warning behavior. It adds no command, option, config field, state group,
dashboard control, recovery action, notification path, or MCP tool. The only
public shape additions are explicit `reason_truncated`, `note_truncated`, and
`message_truncated` metadata on existing bounded text projections; internal
SQLite fields preserve deployment, destination, and verification-policy
identity without asking the operator to supply them.

### Safety decision

Status reasons are redacted before a 1,000-character bound, and every
multi-query projection reads one WAL snapshot. Presentation clients consume one
shared Attention predicate. A verification re-run executes once and resolves
the exact deployment generation atomically only when its persisted policy still
matches; missing, changed, or legacy-unprovable policy requires the existing
explicit `--ack` path.

Known failures now remain Attention until that explicit resolution. Automatic
supersession was removed rather than adding destination-aware policy controls:
this is the smaller fail-closed behavior and does not create a new user choice.
Because it corrects the meaning introduced by contract 3, machine output moves
to contract 4 under the documented safety exception while the v3 product
grammar stays fixed.

### Success measure

CLI and MCP secret/length probes, concurrent-writer snapshot tests,
deployment-group repair and policy-drift tests, cross-client Attention tests,
schema migration tests, and effective-gate warning tests must remain green.
Remove the internal deployment identity only if verification recovery is
removed; do not restore inferred supersession without evidence and exact
destination plus policy identity.
