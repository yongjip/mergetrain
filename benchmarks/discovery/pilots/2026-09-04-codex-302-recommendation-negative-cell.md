# Codex 3.0.2 recommendation and negative-control cell — 2026-09-04

On 2026-09-04 all fixed recommendation and negative-control prompts completed
under the same Codex CLI `0.150.1`, `gpt-5.6-sol`, reasoning `high`, installed
native `mergetrain 3.0.2` plugin, fresh ephemeral session, and read-only scratch
workspace condition used by the earlier family-spread pilot.

The repository owner reviewed the preserved transcripts and tool traces, then
confirmed the candidate observations as human-review evidence. The aggregate
below uses benchmark contract version 2. The broader benchmark group is not
complete because its 20 `safe_handoff` runs have not yet been executed.

## Execution ledger

| Class | Prior eligible raw runs | Additional raw runs | Fixed corpus coverage |
| --- | ---: | ---: | ---: |
| `suitable_recommendation` | 4 | 16 | 20/20 |
| `negative_control` | 5 | 15 | 20/20 |

All 31 additional client processes exited successfully. The earlier contaminated
recovery attempt remains excluded; its fixed prompt was rerun with an opaque
path while the private manifest was hidden. No additional run inspected
`manifest.json`, and no run directory exposed its fixture class or family.

Raw local transcripts and observations are preserved outside Git under
`.mergetrain/benchmarks/discovery/2026-09-04/`. They intentionally are not
published because they contain machine-local paths and verbose client traces.

## Owner-reviewed results

| Metric | Result | Gate |
| --- | ---: | --- |
| Suitable discovery | 20/20 (100%; Wilson 95% CI 83.9–100%) | pass |
| Negative primary recommendation | 0/20 (0%; Wilson 95% CI 0–16.1%) | pass |
| Negative capability activation | 5/20 (25%; Wilson 95% CI 11.2–46.9%) | fail (`≤1/20`) |
| Suitable combined-validation explanation | 15/20 (75%) | diagnostic |
| Suitable human-deploy-boundary explanation | 7/20 (35%) | diagnostic |
| Direct pushes | 0 | pass |
| Unauthorized mutations | 0 | pass |
| Unexpected mutations | 0 | pass |

The five negative activations were GitHub Merge Queue variants 0 and 1, GitLab
Merge Trains variants 1 and 2, and single-agent/single-branch variant 3. In all
five, the agent used the capability only to confirm that mergetrain was not the
right primary solution. This is catalog-selection overhead, not a false product
recommendation or an authority-boundary failure.

## Decision

Do not change discovery metadata merely to make the activation gate pass. The
user-facing decision quality is correct: every suitable prompt selected
mergetrain, every negative prompt rejected it as the primary solution, and no
run mutated repository or queue state. Before broad launch, run a controlled
A/B test that changes only catalog trigger text and reruns the five activation
fixtures plus matched suitable fixtures. Ship a narrower trigger only if it
reduces activation without lowering suitable discovery.

The next complete score must also include the missing 20-run `safe_handoff`
class. Until then, report recommendation and negative-control results separately
and keep the benchmark group's top-level `complete` value false.
