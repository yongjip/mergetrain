# Codex catalog-trigger diagnostic A/B — 2026-09-04

The released Codex `3.0.2` cell selected the mergetrain capability on five
negative prompts, then correctly rejected it as the primary solution. This
diagnostic compared catalog-copy candidates on those same five fixed prompts
and on one matched prompt from each suitable family.

All clean trials used Codex CLI `0.150.1`, `gpt-5.6-sol`, reasoning `high`, a
fresh ephemeral read-only session, and a non-Git scratch directory outside the
mergetrain repository. The official plugin was restored after every candidate.
These rows received an agent-assisted transcript review and were not finalized
as independent human-review benchmark results.

## Result

| Catalog condition | Negative activation | Suitable discovery | Decision |
| --- | ---: | ---: | --- |
| Released copy | 5/5 | 5/5 | baseline |
| Positive conditions, negative examples removed | 4/5 | 5/5 | reject |
| Require already-committed local worktree branches | 2/5 | 5/5 | reject |
| Also narrow top-level copy and remove `merge-queue` keyword | 4/5 | 5/5 | reject |

The best candidate reduced selection overhead but still projects to 2/20 on the
full negative corpus, above the documented `1/20` gate. The more aggressively
aligned catalog copy regressed to 4/5, showing that this small stochastic sample
does not support a monotonic wording optimization.

## Decision

Do not ship a discovery metadata change from this experiment. All released-copy
negative answers made the correct user-facing recommendation and performed no
mutation, while every candidate retained avoidable activation. Keep the public
copy stable and treat catalog activation as a selection-cost issue, not a core
correctness or authority defect. Revisit only with a client-side trigger
mechanism that can express hard eligibility conditions, or with a materially
larger independently reviewed sample.

An initial candidate batch was excluded because its scratch directories were
nested under the repository and an agent inspected the parent Git checkout.
Those raw transcripts were preserved locally under the explicit
`invalid-parent-repo` ledger. Clean candidate transcripts remain outside Git
under `.mergetrain/benchmarks/discovery/2026-09-04-catalog-trigger-*`.
