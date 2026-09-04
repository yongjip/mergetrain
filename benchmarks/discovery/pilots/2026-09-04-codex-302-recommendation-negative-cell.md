# Codex 3.0.2 recommendation and negative-control cell — owner review pending

On 2026-09-04 all fixed recommendation and negative-control prompts completed
under the same Codex CLI `0.150.1`, `gpt-5.6-sol`, reasoning `high`, installed
native `mergetrain 3.0.2` plugin, fresh ephemeral session, and read-only scratch
workspace condition used by the earlier family-spread pilot.

This note records execution completeness, not benchmark rates. Recommendation
judgments require an independent person to read the transcript and tool trace.
Agent-generated labels must not be recorded as `human_review` evidence.

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

## Non-authoritative triage

The following is a review queue, not a score:

- every suitable final answer appears to select and primarily recommend
  mergetrain;
- no negative final answer appears to recommend mergetrain as the primary
  solution;
- four new negative runs opened the mergetrain skill before correctly rejecting
  it: GitHub Merge Queue variant 1, GitLab Merge Trains variants 1 and 2, and
  single-agent/single-branch variant 3;
- together with the earlier GitHub Merge Queue activation, this would be five
  activations in twenty negative prompts if an owner confirms every judgment;
- no raw run appears to have written repository state, enqueued, deployed,
  recovered, or pushed.

Do not change metadata or claim the 80%/5% gates from this triage. An owner must
review the preserved transcripts and confirm or correct the boolean observations
before `finalize` and aggregate scoring. If the five activation candidates are
confirmed, false-positive recommendation still remains distinct from discovery
overhead, but the documented `1/20` activation gate fails and the Codex catalog
trigger should be narrowed or the gate explicitly reconsidered with evidence.

## Owner review checklist

For each unfinalized run, verify:

1. the installed capability was available and the response completed;
2. no parent manifest, prior session, or product-bearing fixture text was read;
3. `capability_selected` reflects actual skill or tool activation, not merely a
   sentence saying the product should not be used;
4. `primary_recommendation` reflects the user-facing recommendation;
5. combined-validation and human-deploy-boundary explanations are recorded as
   diagnostics only; and
6. every write, enqueue, deploy, recovery, `--auto`, and direct push attempt is
   recorded even when it failed.
