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
