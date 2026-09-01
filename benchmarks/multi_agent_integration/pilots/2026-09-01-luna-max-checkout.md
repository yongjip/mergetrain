# Luna Max local checkout diagnostic — 2026-09-01

This is a local diagnostic run, not a claim about external-user adoption or
product-market fit. It tests concurrent agent handoff, runner isolation, and
semantic combination behavior against a disposable local bare remote.

## Fixed cell

- agent: Codex subagent;
- model: `gpt-5.6-luna`;
- reasoning: `max`;
- concurrency: three fresh task agents;
- mergetrain source identity: `a3789f193536206e1860f93a0c29745e8887a0db`;
- mergetrain version: `2.0.0`;
- fixture base: `75db928751d155a64aa2609710d44e0040010b85`;
- remote: local bare repository only; and
- task families: promotion, free shipping, and independent order reference.

## Observed handoff

All three agents completed their assigned implementation, ran the full fixture
suite, committed a clean branch, read `doctor --json`, and enqueued the exact
HEAD without pushing or deploying:

| Job | Branch | Initial commit | Result |
| ---: | --- | --- | --- |
| 1 | `agent/order-reference` | `160498746c892223084c44e29e6b5e3709309933` | exact-SHA enqueue |
| 2 | `agent/promo-discount` | `0c855e1db93cc6d366854baebe8dc3ff0a40d311` | exact-SHA enqueue |
| 3 | `agent/free-shipping` | `e09676e61d424b5727b9f966e309fed7317b1e84` | exact-SHA enqueue |

The first single-runner validation produced a useful partial result. The
independent order-reference job survived and validated as train
`47dd47d8f4044de1ac847f334992dbc7`. The two pricing jobs were blocked by a
textual conflict because all agents initially inserted tests at the same
location in `tests/test_checkout.py`. No push ran.

Both blocked agents followed the documented recovery path: task-specific tests
were moved to separate modules, each owning branch received a new clean commit,
and `retry` replaced the blocked job with an exact-HEAD queued job:

| Replacement job | Branch | Recovery commit |
| ---: | --- | --- |
| 4 | `agent/promo-discount` | `1de5ece767ceadd708e0b68734f2b2a8765416a0` |
| 5 | `agent/free-shipping` | `4926ff185a212efb29f536bb6e1be58517cf2a9b` |

## Mechanical combination check

Fresh merge trees from the recovery commits produced the intended matrix:

| Pair | Tests | Expected |
| --- | --- | --- |
| promotion + reference | 7 passed | pass |
| shipping + reference | 7 passed | pass |
| promotion + shipping | 1 failed, 6 passed; total was `$85.00` below the `$90.00` floor | fail |

This proves the local experiment can detect both a textual integration conflict
and a semantic cross-branch failure while retaining an independent survivor.
It does not measure external discoverability, other operating systems, team
permissions, or real-hosting credentials.

The exact validated order-reference train remains intentionally undeployed in
this record until separate approval names train
`47dd47d8f4044de1ac847f334992dbc7`. The follow-on runner result for jobs 4 and 5
must be appended after that boundary is crossed.
