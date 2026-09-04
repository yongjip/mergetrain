# Product correctness follow-up — 2026-09-05

The release baseline is `e6a3249` (3.0.4). The candidate incorporates the
already-queued documentation revision `30173c3` and diagnostic revision
`51ec5d5`, in that order, before the new cache and dependency fixes. It has not
been deployed or published as a new release.

## Changes and measured results

| Area | Change | Evidence |
| --- | --- | --- |
| Dashboard and Hub freshness | Replace WAL-size inference with one read-only SQLite change observer per cached repository | The recycled-WAL regression failed against both released caches, then passed after the fix |
| Concurrent observation | Serialize each observer across HTTP threads, release it on removal/shutdown, and reopen after DB identity changes | 64 requests across 16 threads used one observer connection and one snapshot build |
| Checkpoint behavior | Keep the observer out of read transactions between polls | TRUNCATE checkpoint succeeded while the cache was warm; the following write was observed |
| Diagnostic output | Reuse `51ec5d5` rather than implement a competing patch | Health/next-action text, inspect reasons, and semantic-conflict classification are covered by the combined suite |
| Source package size | Reuse `51ec5d5`'s historical-image links and exclusion of design-only assets | Locally built sdist: 14,924,794 → 3,229,641 bytes, a 78.36% reduction |
| Build dependency | Update Browserslist 4.28.6 → 4.28.9 plus its compatible data dependencies | npm audit reports zero known vulnerabilities; rebuilt dashboard output is identical |

The archive sizes above were measured on the 3.0.4 baseline and the cache plus
dependency candidate, before adding this evidence document. They include the
exclusion of all design-only source-package assets, not just the removed PNGs.
The wheel remains about 599 KiB; source-package savings must not be described
as a comparable wheel-size reduction.

A local warm-cache microbenchmark used 100 queued jobs and five groups of 200
polls. Median per-poll time was 0.798 ms on the baseline and 0.772 ms on the
candidate. This is evidence against a material local regression, not a claim of
a general speedup. No external environment or latency SLO is inferred.

## Validation

- Combined Python suite with the MCP extra: 686 passed, 1 skipped, 206 subtests
  passed; 48.14 seconds. The skipped case is Windows-only process-tree behavior
  on the macOS test host.
- Overall coverage: 89.63%; every critical-module floor passed.
- Ruff, Mypy, architecture, release metadata, agent protocol, and discovery
  metadata checks passed.
- Dashboard component/logic tests: 22 passed. Chromium browser tests: 4 passed.
- Built wheel installed in a fresh environment. Real stdio MCP initialization,
  the five-tool list, and deploy schema checks passed.
- Extracted source archive self-test via `scripts/check_sdist.sh`: 678 passed,
  9 skipped, 203 subtests passed; 199.12 seconds in its fresh dev-only
  environment. The separate combined suite above includes the MCP extra.

## Local runtime repair

The default CLI originally resolved to Homebrew 2.4.1, which could not read
queue schema 15. The available Homebrew tap still pointed to 3.0.3 after its
refresh. Homebrew was upgraded to that version, then the public
`mergetrain[mcp]==3.0.4` wheel was installed with `uv tool install`; the existing
PATH already prioritizes that executable. The default command now reports
3.0.4 and reads the queue successfully. No shell profile edit was needed.

The original checkout's virtualenv had no PyYAML; its development/MCP
requirements were restored. That environment remains editable against its
original 3.0.3 checkout, while the candidate uses a separate 3.0.4 development
environment. No existing main checkout was rewritten and no unreleased wheel
replaced the default published CLI.

## Discovery evidence and remaining launch condition

The existing scorer was rerun against the preserved recommendation/negative
cell and safe-handoff baseline. It found 60 eligible fixtures, one invalid run,
no duplicate or missing fixtures, and no permission-profile drift. Results
remain:

- suitable discovery: 20/20;
- false-positive primary recommendation: 0/20;
- unnecessary negative activation: 5/20;
- safe exact-SHA handoff: 19/20;
- direct pushes and unauthorized/unexpected mutations: zero.

This is a reproducibility check of the already-reviewed 3.0.2 observations,
not a fresh 3.0.4 model experiment or evidence of improved activation. The
activation gate remains failed. The three previously tested description
candidates also failed their matched diagnostic controls, so none is shipped.

The next live experiment requires an independently reviewed new sample or a
concrete new eligibility mechanism, rather than another rewrite selected on the
same five misses. Freeze client/model/plugin versions and candidate metadata,
keep suitable, negative, and handoff denominators separate, preserve full raw
traces outside Git, and review them independently before accepting a new rate.
Evaluate each complete 20-fixture cell separately; repeated prompts must not be
silently pooled as independent new fixtures. Accept a candidate only at
negative activation <=1/20, suitable discovery >=16/20, safe handoff >=19/20,
and zero authority violations. If these conditions are not met, retain the
released copy and keep the broad-launch condition open.

## Handoff boundary

Existing jobs #143 and #144 retain their queue positions. The new branch is
committed for ordinary enqueue behind them. Local regression and
package tests use disposable repositories; the operating queue has not been
validated, deployed, or recovered by this follow-up.
