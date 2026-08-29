# External repository pilot

This protocol answers one product question before mergetrain gains more
features:

> In repositories that already integrate several coding-agent branches, does
> mergetrain reduce human merge coordination without weakening the deployment
> boundary?

It is a prospective operating study, not product telemetry. Evidence stays in
the pilot repository or an agreed private location. Mergetrain does not gain a
new command, hosted service, or analytics identifier.

## Entry criteria

Recruit three repositories that are not the mergetrain repository. At least two
should be operated by someone other than the mergetrain maintainer. The set
should include at least two of Node, Java, and Unity; no more than one Python
repository should be used for the first cohort.

A repository is eligible only when its prospective baseline shows:

- two or more coding agents working in parallel on at least three days per week;
- at least ten eligible agent branches completed per week;
- a deterministic combined gate worth running before integration; and
- an operator willing to record coordination work as it happens.

If a repository misses these criteria, record it as `non_fit`. Do not count it
as a failed pilot or loosen the criteria after seeing the result.

## Study design

Use one prospective baseline week with the repository's existing integration
process, followed by two treatment weeks with mergetrain. Keep the task mix,
agent permissions, gate commands, working hours, and integration target as
stable as practical.

Day 0 is setup and is reported separately. Days 1–5 are the baseline. Days
6–15 are treatment. The first two treatment days are onboarding days; report
them, but also show steady-state days 8–15 separately.

Do not add product features during a pilot. A correctness or security fix ends
the current condition: record the product commit, apply the fix, and start a new
condition rather than pooling before and after.

## Required safety topology

The pilot tests the enforceable deployment model, not instructions alone:

```text
task agents: repository read/write + commit + enqueue
             no integration-branch push credential
runner:      separate deploy identity + validate/deploy permission
remote:      protected integration branch
```

Initially push trains to an `agent-integration` branch and open one normal PR to
`main` unless the repository already has an equivalently protected integration
workflow. The runner must use a trusted operator checkout and trusted
`.mergetrain.yaml`; do not execute configuration selected from an untrusted task
branch. Give unattended daemons only the minimum runner credential, and use
`--auto` only when that authorization was explicit before enqueue.

Capture the topology, branch-protection rule, task-agent credential boundary,
runner identity, and configuration commit in the pilot record. A pilot without
credential separation may measure convenience, but it cannot support a safety
claim.

## What to record

### Product evidence

At the start of each condition, retain:

```sh
mergetrain version --json
mergetrain doctor --json
git rev-parse HEAD
git status --short
```

During treatment, save a daily `stats --json` snapshot using the condition's
fixed ISO-8601 start time. At the end, save both the final stats and sufficient
history to cover the window:

```sh
mergetrain stats --since 2026-09-01T00:00:00Z --json
mergetrain history --since 2026-09-01T00:00:00Z --limit 1000 --json
```

Retain the exact `.mergetrain.yaml`, repository instructions, integration refs,
and any relevant `inspect`/`events` output. Redact credentials and absolute home
paths before sharing artifacts.

### Human coordination log

Record work when it starts and ends; do not estimate a week retroactively. One
row represents one active human interval:

```csv
started_at,finished_at,repo,condition,category,train_id,job_ids,evidence,notes
2026-09-01T09:00:00Z,2026-09-01T09:08:00Z,example,baseline,manual_merge,,,issue-123,
```

Allowed categories are `branch_handoff`, `queue_observation`, `approval`,
`manual_merge`, `conflict_resolution`, `recovery`, `tool_help`, and `setup`.
Exclude unattended gate wait and ordinary task implementation. Report setup
separately; the primary coordination metric uses all categories except `setup`.

### Daily outcome log

Keep one row per repository and day:

```csv
date,repo,condition,eligible_branches,exact_sha_handoffs,landed_branches,human_corrections,queue_bypasses,direct_push_attempts,unauthorized_mutations,combined_only_failures,recovery_actions
2026-09-01,example,treatment,5,5,4,0,0,0,0,1,0
```

An eligible branch is committed agent work that the repository's normal policy
would integrate. A human correction is an instruction needed to repair tool
discovery, state selection, SHA/branch choice, or the handoff boundary; normal
approval of an exact train is not a correction. A queue bypass is an eligible
branch integrated through another path after treatment onboarding.

A `combined_only_failure` requires all of the following evidence:

1. each member branch passed its declared individual checks;
2. the assembled train failed before remote integration;
3. the failure depends on the combined state, not a pre-existing branch defect;
4. the train ID, branches, gate, and relevant failure artifact are retained.

Do not infer combined-only failures from a generic blocked status.

## Metrics

The primary efficiency metric is normalized for changing throughput:

```text
coordination minutes per 10 eligible branches
= coordination minutes / eligible branches * 10
```

Report baseline, all treatment days, and steady-state treatment separately for
each repository. Do not pool raw minutes across repositories.

Required supporting metrics are:

| Metric | Source |
| --- | --- |
| eligible branches and exact-SHA handoff rate | daily outcome log plus queue evidence |
| queue adoption rate | exact-SHA handoffs / eligible branches |
| human correction rate | corrected handoffs / eligible handoffs |
| queue bypass rate | bypasses / eligible branches |
| direct-push and unauthorized-mutation counts | credential/remote audit plus command evidence |
| landed, blocked, conflict, validation, batching, recovery, and latency data | `stats --json` |
| setup and tool-help minutes | human coordination log |
| combined-only failures | retained incident evidence |
| operator retention intent | final structured interview |

The batching `estimated_savings` field is a labeled counterfactual, not observed
wall-clock savings. Keep it separate from the primary human-time result.
Combined-only failures and correct recovery events are valuable evidence but
are not required to occur in a short pilot.

## Decision thresholds

Continue investing in the standalone product only when all safety guardrails
hold and at least two of the three repositories show both sustained adoption and
material efficiency:

- zero direct integration attempts by task agents and zero unauthorized
  deploy/recovery/destructive mutations;
- exact-SHA handoff rate at least 95% after onboarding;
- human integration correction rate below 5%;
- steady-state queue adoption at least 80%; and
- coordination minutes per 10 eligible branches at least 40% below baseline,
  or an absolute reduction of at least 30 minutes per repository-week.

Also require the operators of those repositories to choose continued use after
the pilot without a maintainer running the queue for them.

Interpret other outcomes explicitly:

- **Integration-engine direction:** safety and efficiency pass, but operators
  consistently want mergetrain embedded in an agent orchestrator rather than
  installed as a standalone tool.
- **Maintenance mode:** fewer than two repositories meet the entry criteria or
  sustain treatment use; fix correctness regressions but freeze feature work.
- **Stop/pivot:** fit-qualified repositories bypass the queue, require frequent
  correction, or show no meaningful coordination reduction after onboarding.

One exceptional avoided incident can justify a targeted correctness fix, but it
does not by itself prove product adoption.

## 2.0 removal evidence

The pilot also closes the compatibility audit. For every repository, search
operator scripts, instructions, and configuration for:

- `agent.require_clean_worktree_before_enqueue`;
- `agent.require_explicit_auto_approval`;
- `agent.prefer_json_status`;
- `terminology.git_operation`, `--integrate`, and `--push`; and
- `hub list`.

Record occurrence counts and whether each occurrence is an actual dependency or
only documentation. Remove these surfaces in 2.0 only if pilot migrations are
clean, no external workflow requires them, and the release notes give the
canonical replacement. Evaluate `run-next`, persistent validation workspaces,
notifications, and other advanced features separately; this pilot does not
pre-authorize their removal.

## Final report template

Publish one table per repository and a cohort decision:

```text
Repository / ecosystem / operator:
Product commit and condition dates:
Entry-criteria result:
Safety topology:
Baseline eligible branches:
Treatment eligible branches:
Coordination min / 10 branches (baseline -> treatment -> steady state):
Exact-SHA handoff / correction / adoption / bypass rates:
Direct-push and unauthorized-mutation counts:
Combined-only failures and recovery actions:
Setup and tool-help minutes:
Operator retention decision and reason:
Evidence paths and known gaps:
```

State `continue`, `integration-engine direction`, `maintenance mode`, or
`stop/pivot` only after showing the per-repository data. A three-repository
pilot is directional evidence, not a universal market claim.

## Recruiting brief

Use this concise description when inviting a pilot operator:

> We are evaluating a local merge queue for repositories that already run
> several coding agents in parallel. The pilot uses one baseline week and two
> mergetrain weeks, keeps task agents away from integration credentials, and
> measures active human coordination time rather than stars or train count. It
> adds no hosted telemetry. We need an operator who completes at least ten agent
> branches per week and can log merge-coordination intervals as they occur.

The separate [agent adoption benchmark](agent-adoption-benchmark.md) measures
whether an agent discovers and follows the protocol. Do not substitute its
synthetic handoff rate for this pilot's real operating-efficiency evidence.
