# Eligibility logic and paired selection diagnostic — 2026-09-05

Reject the candidate and retain the released discovery copy. In this frozen
sample, unnecessary skill-body reads increased from 2/20 to 8/20. This is a
local-skill diagnostic, not a complete product-discovery or release benchmark.

## What was designed before testing

The [decision model](../eligibility/design.md) separates explicit product tasks,
existing-queue tasks, hosted-queue administration and prospective adoption.
User prohibitions take precedence. Existing queue work remains applicable even
with one branch or an empty queue. Committed/clean branches are prerequisites
for enqueueing, not for asking whether to adopt the product. Hosting on GitHub
is distinct from using GitHub's hosted integration queue. Unknown suitability
remains unknown; selection never grants mutation authority.

The benchmark-only oracle passed every combination of four boolean and three
three-valued facts (432 states). This verifies the finite decision model, not
natural-language interpretation. No oracle was added to the runtime.

The [candidate](../eligibility/candidate.json) front-loads existing mergetrain
queue tasks and local parallel-agent integration; it narrows push recovery to
that workflow and uses a general hosted-PR exclusion. Only the skill description
changed. The operational body is identical to the signed v3.0.5 baseline.
Candidate, oracle and 48 new questions were committed in `90b3de3` before live
results. No treatment or question was retuned after viewing outcomes.

## Observed results

| Endpoint | Released v3.0.5 description | Candidate |
| --- | ---: | ---: |
| Negative prompts: observed body reads | 2/20 (10%) | 8/20 (40%) |
| Suitable prompts: raw observed body reads | 20/20 | 20/20 |
| Suitable prompts: strict paired subset after runtime exclusions | 17/17 | 17/17 |
| Boundary prompts | 8 qualitative reviews | 8 qualitative reviews |
| Completed processes | 48/48 | 48/48 |
| Unchanged disposable workspaces | 48/48 | 48/48 |

The negative activation admission gate is at most 1/20. Neither description
passes on this new sample, and the candidate adds six net unnecessary reads.
Its eight misses are `negative-01`, `02`, `05`, `06`, `07`, `08`, `10`, `11`.
The baseline's misses are `negative-15` (retrospective ordinary push recovery)
and `negative-18` (dependency caching for parallel-agent worktrees).
Eight negative pairs regressed and two improved. One trial per question is not
evidence of a population-wide regression rate or a deterministic routing rule.
The old 3.0.2 5/20 result uses a different corpus and installation condition;
do not present the new 2/20 as a version-to-version improvement.

These endpoints count a real body read observed in a shell tool result, not a
self-reported selection or a mention of the product. Suitable read counts do
not constitute a complete primary-recommendation correctness grade. The 17/17
strict subset is incomplete against a 20-fixture admission requirement.

## Counterexamples and interpretation

- For GitHub check configuration, the candidate session said it was using the
  skill because the request concerned an existing merge queue, then read the
  body and recognized that local queue actions were inapplicable. The paired
  baseline skipped the skill and cited the explicit GitHub exclusion.
- For GitLab pipeline configuration, several candidate sessions treated the
  generic merge-train topic as sufficient, despite the hosted-PR exclusion.
  Generalizing specific exclusions therefore did not improve this sample.
- The candidate avoided the baseline's needless read for retroactive recovery
  without historical audit evidence and for a dependency-cache question. Those
  gains did not compensate for the new hosted/single-worker misses.
- In the single-branch existing-queue boundary case, the baseline answered from
  memory and omitted the established status/enqueue/stop handoff. The candidate
  read the skill and gave that handoff correctly.
- In the empty-queue boundary case, the candidate instead answered from memory,
  recommended ordinary Git for one branch, and equated emptiness with absence of
  coordination/recovery state. The baseline followed status/next-action guidance.
- The candidate's existing-recovery answer read the skill but cited live web
  documentation and proposed removed `mergetrain doctor` syntax. The pinned
  3.0.5 CLI rejects it and redirects to `status --diagnose`. Reading a correct
  skill alone did not establish correct version-specific instructions.
- Both descriptions read the skill for the intentionally underspecified queue
  choice question, then asked for context. Keep this separate from the known
  negative denominator; unresolved suitability is not a confirmed exclusion.

Both descriptions respected explicit non-use, incidental translation, and an
unrelated Markdown task in a configured repository. Some conceptual boundary
prompts prohibit commands without distinguishing document reads from product
commands. They are unsuitable for a strict load/no-load success metric: future
fixtures should explicitly allow document reading while prohibiting Git and
product actions. Do not reinterpret these cases as proven mutation violations.

## Evidence quality and scope

The run used Codex CLI 0.150.1, gpt-5.6-sol, high reasoning, paired fresh ephemeral
sessions, read-only execution with no approval, four simultaneous question
pairs, fixed seed 305, and opaque non-Git temporary workspaces. Other skills
were disabled per invocation. Calibration confirmed only the intended skill
was available. User configuration, installed plugin caches and credentials were
not changed. Both copied skills use the v3.0.5 body; this does not simulate the
full plugin/MCP catalog, normal competing skills, or another client.

All 96 processes completed; no retries or timeout results were substituted.
Full traces retain 115 shell tool results and 44 web-search events. Author trace
review found only read operations and no deployment, queue mutation or direct
push attempts. `mergetrain init --project example` appeared once; its default
prints configuration and needs `--write` to mutate. No file changes occurred.

The ambient CLI was not pinned by the initial runner. `suitable-08` baseline
explicitly reported version 3.0.4, and `suitable-19` baseline and `suitable-12`
candidate also inspected the ambient executable. Exclude all three question
pairs from the strict suitable comparison, keeping both arms and raw records.
Their first body reads preceded CLI consultation, so raw read counts remain
visible, but their final answers are not clean v3.0.5 evidence. No negative
trial invoked that CLI. Future runs must pin and verify the executable inside
the same login-shell environment as the model, not just in the parent process.

This is author-reviewed diagnostic evidence, not independent human approval.
Semantic comments inspect the relevant task/authority claims, not the accuracy
of every external provider configuration instruction. Web lookups are not
version-frozen evidence for mergetrain's CLI. No safe-handoff workload was run,
so the former 19/20 handoff result must not be reused as a new candidate result.

Raw negative median wall times were 23.752 s baseline and 16.273 s candidate;
median output tokens were 731.5 and 570.5. These are descriptive only. Browse
count, answer length, provider/cache variation, parallel runs and an overlapping
local regression suite confound timing. The result does not prove that fewer
skill reads save end-to-end latency or token cost. Do not relax the activation
gate after seeing these measurements to make this candidate pass.

## Artifacts, validation and next decision

[Machine-readable results](../eligibility/results-2026-09-05.json) retain input
hashes, per-run trace hashes, exclusions and separate denominators. Full raw
records remain outside Git under the repository's local
`.mergetrain/benchmarks/discovery/2026-09-05-eligibility/` directory. The exact
measured runner is recoverable at commit `1357f73` and its hash is recorded.
The frozen baseline was subsequently copied into the experiment directory to
prevent future product edits from silently changing a historical comparison.

Reproduce aggregation with:

```sh
python -m benchmarks.discovery.eligibility.summarize \
  /path/to/2026-09-05-eligibility \
  --output /tmp/eligibility-results.json
```

Validation: 691 tests passed, one skipped, 206 subtests passed; production
coverage 89.72%, all critical floors passed. Ruff, Mypy, architecture and
released discovery-metadata checks passed. A harness failure test verifies that
labels remain outside the child workspace and failed traces remain inspectable.

Keep the public metadata and implicit-invocation policy unchanged. Preserve
specific provider exclusions in any later proposal; the logical rules are a
review framework, not proof that more abstract wording improves selection.
Before another scored experiment, fix runtime pinning, remove ambiguity about
permitted document reads, and independently review fresh fixtures. Evaluate
full installed-plugin discovery and exact-SHA handoff before promotion. Adding
a router skill would itself incur selection/reading cost, and no documented
hard semantic pre-load predicate was established in the official skills docs:
https://learn.chatgpt.com/docs/build-skills
