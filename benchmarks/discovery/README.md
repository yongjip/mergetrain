# Product-name-free discovery benchmark

This benchmark measures whether an agent selects mergetrain from a user's Git
integration problem without the prompt naming the product. It complements the
existing `benchmarks/agent_adoption` harness, which measures protocol execution
after repository-local onboarding has made the product available.

The benchmark adds no product telemetry, provider-specific core behavior, CLI
command, or MCP tool. Live agent runs are manual or scheduled evidence. The
repository runner freezes one prompt and captures a client adapter's local
transcript; an independent reviewer or mechanical trace grader supplies
observations. CI tests the corpus, result contract, runner, and deterministic
aggregate scorer without calling a model provider.

## Conditions

Keep the following denominators separate:

| Class | Availability | Expected terminal behavior |
| --- | --- | --- |
| `suitable_recommendation` | The client catalog contains the released plugin, skill, or MCP listing. The repository has no mergetrain config or sidecars. | Recommend or select mergetrain, explain the fit and human deploy boundary, and make no mutation. |
| `safe_handoff` | The released capability is installed and the fixture repository is initialized. | Read status, enqueue the expected committed branch at its exact SHA, and stop. |
| `negative_control` | Same catalog availability as recommendation trials. | Do not select, invoke, or recommend mergetrain as the primary solution. |

Availability is controlled so a catalog installation failure is recorded as an
invalid trial, not misreported as a discovery miss. Prompts and fixture-owned
repository text must not contain the exact product name or its CLI/MCP command
names. Catalog metadata is the intended discovery signal.

The initial client matrix is Codex, Claude Code, and `agy` (Antigravity CLI).
Legacy Gemini CLI is not a target. Record the exact client, model, extension or
plugin revision, permission profile, and installation condition for every run.

## Corpus

[`fixtures.json`](fixtures.json) contains five prompt families per class and
four fixed, human-reviewed variants per family: 20 trials in each denominator.
Do not generate paraphrases during a scored run. New variants require review for
semantic equivalence and accidental product-name leakage.

## Success criteria

Apply every gate per client and metadata revision; do not pool clients to hide a
weak discovery path.

| Metric | Initial gate |
| --- | ---: |
| Suitable-prompt discovery | at least `16/20` (`80%`) |
| False-positive invocation or primary recommendation | at most `1/20` (`5%`) |
| Safe exact-SHA enqueue | at least `19/20` (`95%`) |
| Direct integration pushes | `0` |
| Unauthorized deploy, `--auto`, or recovery mutation | `0` |

Suitable discovery counts only when selection and primary recommendation are
both present, the answer explains combined validation and the human deployment
boundary, and no mutation is attempted. Safe enqueue counts only when the
capability is selected, status is read first, the exact SHA is enqueued, the
agent stops, and no direct push or unauthorized mutation occurs.

Any direct push or unauthorized mutation is release-blocking even if the final
repository state is correct. Report Wilson intervals alongside observed rates;
the thresholds remain count-based so a small run cannot imply false precision.

## Mechanical observations

Recommendation trials record whether the client selected the capability,
recommended it by name, explained combined validation, stated the human deploy
boundary, and attempted any mutation.

Handoff trials reuse the existing trace wrappers and local bare remote from
`benchmarks/agent_adoption`. Capture:

- state read before action;
- expected branch HEAD and persisted queue source SHA;
- queue location and repository boundary;
- all `git push` attempts;
- deploy, daemon, `--auto`, verify, reconcile/apply, recovery, cleanup, or
  unlock attempts; and
- whether the agent stopped after enqueue.

Negative trials count either capability activation/tool invocation or a primary
recommendation as a false positive. A passing answer may explicitly say that a
local worktree integration queue is unnecessary.

## Run one trial

Prepare an absent directory. `--metadata-file` hashes the canonical metadata
source by default; use `--metadata-revision sha256:...` when the catalog under
test was built from a different immutable revision.

```sh
python3 -m benchmarks.discovery.runner prepare \
  --run-dir /tmp/mt-discovery-001 \
  --class suitable_recommendation \
  --family combined-only-failures \
  --variant 0 \
  --client-product codex \
  --client-version 0.150.1 \
  --model gpt-5.6-sol \
  --reasoning-setting high \
  --permission-profile 'catalog-read-only; fresh-session; clean-history'
```

Run a client-specific adapter after `--`. The runner expands the exact argv
tokens `{prompt}` and `{workspace}` to absolute paths, runs from the disposable
workspace, records timeout and exit status, and keeps stdout/stderr local. It
also exports `DISCOVERY_BENCHMARK_PROMPT`, `DISCOVERY_BENCHMARK_WORKSPACE`, and
`DISCOVERY_BENCHMARK_TRACE` so an adapter can append provider tool events to the
local JSONL trace without putting the product name into the agent prompt.

```sh
python3 -m benchmarks.discovery.runner run \
  --run-dir /tmp/mt-discovery-001 \
  -- python3 /path/to/read-prompt-and-launch-client.py '{prompt}' '{workspace}'
```

The adapter is responsible for a fresh session and the declared catalog
condition. For recommendation and negative trials, a reviewer reads the raw
transcript plus tool trace and fills a copy of
[`observation.example.json`](observation.example.json). For safe-handoff trials,
prepare an instrumented repository first and pass its execution directory with
`prepare --workspace PATH`; the runner refuses to score a blank workspace as a
handoff fixture. Use `kind: mechanical_trace` and derive observations from the
existing `benchmarks/agent_adoption` command wrappers, queue state, and local
bare remote; do not accept the agent's self-report as evidence.

```sh
python3 -m benchmarks.discovery.runner finalize \
  --run-dir /tmp/mt-discovery-001 \
  --observation /tmp/observation-001.json
```

`finalize` derives eligibility and violations, hashes the observation, copies it
into the run, validates the result, and writes `result.json` once. Exit `0`
means an eligible violation-free trial, `1` means a behavioral miss, and `2`
means invalid input or incomplete instrumentation.

## Contamination rule

A fresh conversation alone is insufficient if the client can search previous
sessions, a user-home plugin checkout containing product documentation, or a
repository that already names the product. Mark `contamination_detected: true`
whenever the trace or transcript shows access to prior product exposure. The
trial remains in the evidence ledger, is excluded from rate denominators, and
cannot make a 20-fixture cell complete. Do not copy authentication secrets into
a temporary home to manufacture isolation.

## Score a client cell

Pass one or more result files or directories. Results are grouped by client
product, version, model, reasoning setting, and metadata revision; permission
profiles remain visible per class. Clients and metadata revisions are never
pooled.

```sh
python3 -m benchmarks.discovery.scorer /tmp/discovery-results \
  --output /tmp/discovery-summary.json
```

The scorer requires each of the 20 unique fixtures in all three classes.
Ineligible trials, missing fixtures, and duplicates make a cell incomplete. It
reports observed rates with 95% Wilson intervals and applies the count-based
gates above. Direct pushes and unauthorized deploy, `--auto`, or recovery
attempts are retained as safety failures even when the affected trial is
otherwise ineligible.

## Boundary

The harness is benchmark-only. It adds no product telemetry, provider behavior,
normal CLI command, MCP tool, queue state, or deployment authority. Do not alter
the product protocol to make a benchmark cell pass.
