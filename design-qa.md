# Compact Batch Status Design QA

## Evidence

- Selected visual source:
  `dashboard/design/status-clarity-option-1.png`
- Browser-rendered implementation:
  `dashboard/design/status-clarity-implementation-v2.jpg`
- Same-frame full comparison:
  `dashboard/design/status-clarity-comparison-v2.jpg`
- Focused status, lifecycle, and request comparison:
  `dashboard/design/status-clarity-focused-v2.jpg`
- Local preview:
  `http://127.0.0.1:4175/#repo=~%2FProjects%2Fmergetrain`
- Source pixels: `1487 × 1058`.
- Implementation pixels: `1487 × 1058`.
- CSS viewport: `1487 × 1058`; density `1`.
- State: dark theme, exact train `#34`, tests passed, runner idle, and explicit
  deployment approval still required before updating `origin/main`.

## Full-view comparison

- The implementation keeps the source's single dominant amber state banner,
  four-stage lifecycle, one dense request row, metadata line, and collapsed
  operational detail.
- The status area now answers the state in one read: `Awaiting deploy approval`,
  `Tests passed · Not on main yet`, and `Approve deployment to origin/main`.
- The permanent right inspector and fixed-height workspace are removed. The
  implementation is intentionally more compact than the source to satisfy the
  user's request to eliminate empty space.
- Existing product navigation, connection status, theme control, and local-only
  footer remain because they are real application context rather than mock
  content.

## Focused region comparison

- Status banner: amber is restricted to the unresolved approval and next
  action. Batch size and runner state are secondary facts separated by rules.
- Lifecycle: queued, merged, and tests passed are green completed states;
  approval is an amber clock state and is not presented as running or done.
- Request row: order, request, branch, merge result, test result, and approval
  are visible in one scan. `Exact train #34` and FIFO order remain explicit.
- Metadata and history: the train ID remains visible while activity and runner
  internals are collapsed into a single compact drawer.

## Required fidelity surfaces

- Fonts and typography: the product's Inter Variable and JetBrains Mono
  Variable fonts preserve the source hierarchy for human-readable status and
  machine identifiers.
- Spacing and layout rhythm: natural content height replaces the previous tall
  container. The banner, lifecycle, table, metadata, and drawer form one dense
  vertical sequence without reserved empty regions.
- Colors and tokens: amber marks pending approval, green marks completed merge
  and test outcomes, and the existing dark operational palette remains.
- Image and icon fidelity: no raster assets are used in the application.
  Existing Phosphor clock, check, repository, and disclosure icons match the
  source's restrained operational iconography.
- Copy and content: the selected source wording is preserved for the dominant
  state and the real target ref is shown in the next action.

## Interaction and runtime checks

- `Full activity and history` opened to reveal Activity, Runner, attention
  history, and deployment history; it closed back to the compact state.
- `← All repos` returned to the two-repository Hub, and the mergetrain card
  reopened this repository view.
- The Hub continued to show both mergetrain and teratorn at a glance.
- Browser console errors: none.
- Live snapshot progress is derived from the real runner phase. A running gate
  no longer defaults to the final validated step.

## Comparison history

- V1 used terminology-derived `deployment` in the dominant heading.
  Fix: aligned the heading to the selected source's `Awaiting deploy approval`.
- V1 risked making the final marker read like generic activity.
  Fix: the approval marker and request outcome now use static clock treatment,
  while only an actual running batch uses a spinner.
- Post-fix evidence:
  `dashboard/design/status-clarity-comparison-v2.jpg`.

## Findings

- No actionable P0, P1, or P2 differences remain for the selected desktop
  target and the user's compactness override.
- P3: the implementation keeps the richer existing product header, which is
  useful real-application context and sits outside the changed status workflow.

final result: passed
