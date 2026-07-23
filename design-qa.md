# FIFO Merge Request Dashboard Design QA

## Evidence

- Selected visual target:
  `dashboard/design/dashboard-fifo-requests-target.png`
- Previous browser implementation capture:
  `/private/tmp/mergetrain-dashboard-demo-ui-final.png`
- Stale browser capture after the server restart:
  `/private/tmp/mergetrain-dashboard-stale-browser.png`
- Local preview: `http://127.0.0.1:4173/`
- Target pixels: `1505 × 1045`.
- State: dark theme, four committed FIFO requests, request #2 blocked by a
  real Git merge conflict, requests #1/#3/#4 validated together.

## Implemented Product Model

- The main card is one FIFO train containing all four merge requests.
- The shared rail is `Queue → Merge in order → Gates → Ready`.
- The request table keeps queue order visible and shows the result at each
  stage: merge, gate, and outcome.
- Request #2 is the only red row. It says `Git conflict → Skipped → Rebase`.
- Requests #1, #3, and #4 remain green train members.
- A separate validated-train summary says
  `#1 + #3 + #4 → main after approval`.
- The inspector explains that #1 merged first, #2 was skipped, and #3/#4
  continued. It separates the atomic deploy action from the #2 rebase action.
- Semantic-conflict bisection remains an advanced runner behavior and is no
  longer the default walkthrough.

## Static and Functional Checks

- The canonical local demo creates four real committed branches that edit a
  disposable repository.
- `run-batch --validate-only` produced the expected real state:
  `#2 blocked`, `#1/#3/#4 validated`.
- The replay now has seven presentation states: queued, four ordered merge
  attempts, gates, and ready.
- Dashboard unit tests passed.
- Production dashboard build passed.
- Full Python suite passed: `310 tests`, `1 skipped`.
- `git diff --check` passed.

## Visual QA Status

- The selected target preserves the existing dashboard's Inter/JetBrains Mono
  typography, Phosphor icon language, dark operational palette, borders,
  density, radii, collapsed detail panels, and desktop composition.
- The existing in-app browser tab retained the old JavaScript asset after the
  preview server restart. The browser safety policy rejected an automated
  localhost reload, so the new build could not yet be captured from that tab.
- The stale tab did receive the new queue snapshot over SSE, which confirms the
  disposable FIFO demo data is available, but it is not valid evidence for the
  new component rendering.
- Final desktop/responsive pixel comparison, replay interaction, overflow, and
  console checks remain pending until the in-app browser tab is refreshed.

final result: pending browser refresh
