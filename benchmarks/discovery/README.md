# Product-name-free discovery benchmark

This benchmark measures whether an agent selects mergetrain from a user's Git
integration problem without the prompt naming the product. It complements the
existing `benchmarks/agent_adoption` harness, which measures protocol execution
after repository-local onboarding has made the product available.

The benchmark adds no product telemetry, provider-specific core behavior, CLI
command, or MCP tool. Live agent runs are manual or scheduled evidence; CI only
validates the corpus, result contract, and deterministic scorers.

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

## Scaffolding boundary

This first revision defines and validates the frozen corpus and result shape. A
later small change may add launchers and scoring by composing the existing
agent-adoption harness. It must not change the product protocol to make the
benchmark pass.
