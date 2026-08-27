# Efficient mergetrain operation

The fastest safe train is not the one with the most shortcuts. It is the one
whose expensive work is measured, scoped, and run at the right time.
mergetrain's default remains conservative: isolated worktrees, full pre-push
gates, explicit deploy approval, and no cache or validation reuse.

Use this order when reducing latency:

1. remove avoidable waiting around the runner;
2. make each gate fast and deterministic;
3. scope independent gates to the paths that can affect them;
4. batch compatible work so shared gates run once;
5. consider a persistent build cache only after measuring a cold-build problem;
6. consider validated-gate reuse only with an explicit safety policy.

Cache and validated-gate reuse solve different problems. A cache accelerates a
gate that still runs. Reuse skips eligible pre-push gates after restoring and
verifying an exact validation identity.

## Measure the critical path first

Start with read-only commands:

```sh
mergetrain doctor --json
mergetrain status --json
mergetrain stats --json
```

`stats` separates the timings that operators can act on:

| Field | What it measures | Typical response when high |
| --- | --- | --- |
| `latency.queue_wait` | first train request to validation/direct-deploy runner start | start the runner sooner; check daemon ownership and scheduling |
| `latency.approval_wait` | completed validation to the deploy runner start | validate only a final train and request approval immediately |
| `latency.runs.validate` | observed validation runner duration | inspect validation phases and slow gates |
| `latency.runs.deploy` | observed deploy runner duration | inspect gates, push, and post-push verify separately |
| `latency.phases[]` | fetch, assembly, gates, push, and verify by run mode | optimize the phase that actually dominates |
| `gates[]` | completion state and duration by gate | parallelize or narrow the slow gate |
| `validation.runs.failure_rate` | validation runs with any failed member, over conclusive retained runs | inspect failure reasons before changing train size or gates |
| `validation.trains.deployment_rate` | validated trains that eventually deployed, including pending trains in the denominator | reduce supersession and approval delay; use `resolved_deployment_rate` for closed outcomes |
| `outcomes.not_landed_reason_counts` | mutually exclusive reasons that observed trains have not deployed | address the largest repeatable failure class first |
| `outcomes.conflicts` | merge and semantic conflicts over terminal trains | separate high-conflict branches or improve task boundaries |
| `trains.terminal_land_rate` | deployed trains over deployed + blocked + failed + canceled trains | compare with legacy `land_rate` to expose cancellation/supersession cost |
| `batching.jobs_per_run` | claimed jobs per retained runner invocation | compare actual batches with the intended two-to-five-job starting heuristic |
| `batching.estimated_savings` | conservative gate executions and seconds avoided by successful multi-job runs | weigh observed savings against validation failure and conflict rates |

Recommendations require at least three timed samples and include their evidence.
Treat them as leads, not commands. Runner events retain the newest 5,000 rows,
and the `latency.coverage` object says how much usable history was observed.
Incomplete runs are counted in coverage but never converted into zero-second
samples.

The batch-savings figure is a counterfactual, not observed elapsed-time savings:
it assumes the same timed successful gate would otherwise have run once per
job. Failed, partial, reused, skipped, and untimed gates contribute nothing.
This makes the estimate conservative and auditable, but it still cannot account
for per-job cache effects or parallelism. Check it alongside validation failure
and conflict rates, never by itself.

`stats.evidence_gaps` is also operational evidence. For example, current queue
history cannot truthfully reconstruct how often operators invoked `recover` or
`reconcile`, so the command reports that gap instead of estimating from mutable
job notes.

`average_queue_seconds` remains available for compatibility, but it aggregates
durable job history and can include long human pauses. Prefer the attributed
`latency` fields when deciding what to optimize.

## Build coherent trains

Batching saves time when several low-conflict branches share the same expensive
gates. As a starting heuristic, try two to five cohesive jobs per train, then
adjust from your own failure and timing history. There is no universal ideal
size.

Good batch candidates:

- small changes in separate modules;
- documentation and code changes covered by the same quick checks;
- branches whose combined behavior is the behavior you intend to ship.

Keep a branch separate when it changes a schema, lockfile, generated project
file, release pipeline, or another high-conflict surface. A large train with a
likely semantic conflict makes failure isolation and revalidation more
expensive than the saved gate run.

Do not leave a validated train waiting while continuing to edit its branches.
Freeze the intended membership, validate it, inspect the exact `train_id`, and
request deploy approval promptly. A changed task head is correctly refused.

## Design gates for fast failure

Put cheap, high-signal checks before expensive checks. Keep commands
deterministic and non-interactive, and pin their toolchain through the project
lockfile or environment. A gate must fail when a required tool is absent; a
silent skip produces quick but meaningless validation.

Current top-level gates run in order. Parallelize within a gate using the
project's own scheduler:

```yaml
gates:
  - name: lint
    run: ruff check .
  - name: tests
    run: python -m pytest -q -n auto
```

Avoid repeating the same expensive suite in multiple top-level gates. Separate
gates are useful when they have different path scopes, failure ownership, or
operating requirements—not merely to give one command several names.

### Scope gates by changed path

Use fail-closed `paths` for checks that truly cannot be affected outside a
known boundary:

```yaml
gates:
  - name: api-tests
    run: ./scripts/test-api
    paths:
      - api/**
      - shared/**
      - lockfiles/api.lock
```

Include shared code, build scripts, manifests, lockfiles, generated inputs, and
test infrastructure that can change the result. When mergetrain cannot compute
the changed-path set, it runs every scoped gate. Leave cross-cutting security,
policy, and repository-integrity checks unscoped.

Path scoping is often the best first optimization because it preserves cold,
isolated execution for every gate that does run.

### Keep pre-push and post-push work distinct

Pre-push gates answer “is this combined commit safe to land?” Post-push verify
hooks answer “did the landed commit become healthy in its destination?” Do not
move a safety requirement to `deploy.verify` just to make the push faster:
verify runs after the remote ref has already changed.

Network-dependent checks tend to be slower and less deterministic. Keep the
reproducible safety subset local and use a verify hook for remote CI or live
health only when a post-push warning is the correct failure model.

## Cache only a measured cold-build bottleneck

The recommended default is:

```yaml
state:
  validation_workspace:
    mode: ephemeral
```

Stay ephemeral when tests are already fast, cache correctness is unclear, the
tool writes outside the declared project directory, or most deploy latency is
approval/remote verification rather than local compilation.

Persistent validation is reasonable when all of these are true:

- `stats` shows a repeatable cold-build gate bottleneck;
- the tool has a project-local, Git-ignored cache directory;
- the cache is safe across hard resets and branch combinations;
- a stable cache identity can include every relevant toolchain and policy
  input;
- warm validation saves enough time to justify maintenance and disk cost.

Example:

```yaml
state:
  validation_workspace:
    mode: persistent
    cache_key: unity-library-v2
    cache_paths:
      - game/Library
```

mergetrain preserves only the declared ignored directories, invalidates them
when the key, gate policy, or environment fingerprints change, and continues
to use isolated deploy and bisect worktrees. Change `cache_key` when the cache
schema or an un-fingerprinted toolchain changes.

Roll out one cache at a time. Compare at least three cold and three warm
validation samples, monitor disk growth and flake rate, and revert to
`ephemeral` if the saving is not material. Never add a cache path merely
because a build tool has one.

## Reuse validation only under an explicit policy

Validated-gate reuse is more aggressive than a warm cache. Keep
`deploy.reuse.enabled: false` unless the repository owner has accepted the
policy. For a one-off authorized decision, preview it without changing state:

```sh
mergetrain run-batch --deploy \
  --train-id <id> \
  --reuse-validated \
  --preview \
  --json
```

Safe reuse needs a short age limit, environment fingerprints for compilers,
SDKs, container images, and other non-Git inputs, plus
`always_rerun_on_deploy: true` for time-sensitive policy checks. Prefer
`on_mismatch: rerun` while introducing the policy. Post-push verification still
runs.

Do not use reuse to hide a slow, flaky, or over-broad test suite. Fix or scope
that suite first.

## Keep one operator checkout authoritative

The runner reads configuration from the checkout where mergetrain is invoked.
For self-hosting repositories, an old or locally edited `.mergetrain.yaml` can
silently select different gates from the integration branch.

`doctor --json` compares the local configuration bytes with the configuration
at the locally known integration ref and reports:

- `config_drift.state=in_sync` when the blobs match;
- `drifted` plus an `operator_config_drift` recommendation when they differ;
- an explicit unavailable/missing state when comparison is impossible.

This check is read-only and does not fetch, so update the operator checkout's
remote refs through the repository's normal safe workflow. Review the diff;
do not discard uncommitted operator changes automatically.

Let exactly one runner or daemon own merge, gates, push, and verify. Multiple
agents may enqueue, but they should not run competing integration loops or push
deploy refs themselves.

## Recovery is part of the fast path

Fast recovery avoids repeating failed manual work:

1. Read `doctor --json` or `status --json` and follow `next_action`.
2. Fix a blocked or failed job in its owning worktree and commit the result.
3. Run `mergetrain retry <job-id>` to dismiss the old outcome and capture fresh
   SHAs.
4. After a crash or ambiguous push, run `reconcile`/`recover` before another
   deploy.

Do not bypass a failed row with a duplicate enqueue. That loses failure
ownership and makes queue history harder to interpret.

## Project profiles

These are starting points, not universal presets:

| Project | First optimization | Cache guidance |
| --- | --- | --- |
| Python | use pytest-xdist where isolation permits; scope lint/type/test gates to source, tests, config, and lockfiles | usually stay ephemeral; Python bytecode and package caches rarely justify a stable worktree |
| Node | use the lockfile's reproducible install and the test runner's worker controls; separate frontend/backend scopes | prefer package-manager caches managed outside the worktree; preserve `node_modules` only with strong lockfile/platform invalidation |
| Gradle/Android | use Gradle's own parallelism and build cache; scope by modules plus shared build logic and version catalogs | measure first; project-local caches may help, while machine-global caches are outside validation-workspace preservation |
| Unity | keep trains small around scenes, prefabs, and project settings; run batch-mode validation | `Library` is a strong candidate only when ignored, path-stable, fingerprinted by editor/toolchain, and demonstrably faster |

For every profile, include shared build inputs in path scopes and fingerprints.
A false skip is more expensive than a cold run.

## Adoption checklist

- [ ] `doctor --json` is healthy and config drift is understood.
- [ ] One runner owns all integration and deploy work.
- [ ] Gates are ordered from cheap/high-signal to expensive.
- [ ] The test runner uses safe internal parallelism.
- [ ] Path scopes include shared inputs and fail closed.
- [ ] Three or more samples identify the actual slow phase.
- [ ] Persistent cache remains off unless warm results justify it.
- [ ] Validated reuse remains off unless explicitly authorized and fingerprinted.
- [ ] Validation happens only after train membership is final.
- [ ] Failed jobs are fixed and retried; crash outcomes are reconciled.
