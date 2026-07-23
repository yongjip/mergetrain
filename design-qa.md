# Hub Repository Information Design QA

## Evidence

- Source visual truth:
  `dashboard/design/audit-hub-card-info/01-current-hub.jpg`
- Scoped user override: repository cards need enough information to understand
  actual work and state without opening each repository.
- Browser-rendered implementation:
  `dashboard/design/hub-card-info-implementation-v2.jpg`
- Same-frame full comparison:
  `dashboard/design/hub-card-info-comparison-v2.jpg`
- Focused rollup and card comparison:
  `dashboard/design/hub-card-info-focused-v2.jpg`
- Local preview: `http://127.0.0.1:4175/#`
- Source pixels: `1280 × 720`.
- Implementation pixels: `1280 × 720`.
- CSS viewport: `1280 × 720`; density `1`.
- State: dark theme, two real registered repositories. Mergetrain has validated
  train `#34` awaiting deploy approval; teratorn has a clear queue after
  deployment `#50`.

## Full-view comparison

- The existing dark product shell, top navigation, rollup band, two-card
  structure, borders, radii, typography, state colors, and footer remain.
- The grid now uses the full available width for two repositories instead of
  reserving a third empty column.
- Each card has a single operational headline, one current/latest work item,
  three comparable facts, target and runner metadata, and an explicit
  `Open details` affordance.
- The denser cards remain above the fold at the supplied viewport.

## Focused region comparison

- Rollup: `ready or idle` is replaced by separate approval and queue-clear
  counts, so the one required action is visible before scanning cards.
- Mergetrain: green `READY` is replaced by amber `APPROVAL`; validated work
  remains green while the unresolved deployment state stays amber.
- Teratorn: the empty middle now shows latest deployment `#50`, task, branch,
  commit, and workflow age.
- Both cards retain equal height and aligned fact/footer rows despite different
  operational states.

## Required fidelity surfaces

- Fonts and typography: Inter Variable and JetBrains Mono Variable remain the
  application fonts. Operational headlines are readable body emphasis; labels,
  SHAs, branches, counts, and timestamps use the existing compact mono scale.
- Spacing and layout rhythm: the original card padding and compact 6–7px radii
  remain. Added sections use consistent 10–12px internal spacing and aligned
  three-column fact rows.
- Colors and tokens: existing blue, green, amber, red, surface, and border
  tokens are reused. No decorative palette was introduced.
- Image quality and asset fidelity: the Hub has no raster content assets.
  Existing Phosphor icons provide status, branch, runner, and disclosure
  affordances.
- Copy and content: visible copy is sourced from actual snapshot state. The
  dominant label distinguishes approval-pending from completed validation.

## Interaction and runtime checks

- Mergetrain card opened the real `#34` repository detail and returned to Hub.
- Teratorn card opened the real queue-clear repository detail and returned to
  Hub.
- The whole card remains keyboard/button semantic and `Open details` makes the
  drill-down visible.
- Browser console errors: none in the final run.
- Dashboard unit tests: 7 passed.
- Production dashboard build: passed.

## Comparison history

- Initial P1: `READY` implied completion while deploy approval was still
  required. Fix: amber `APPROVAL` card state plus `Awaiting deploy approval`.
- Initial P1: work identity and event age were absent. Fix: current/latest work
  preview with request, task, branch, SHA, and workflow timestamp.
- Initial P2: two cards occupied only two of three auto-filled columns. Fix:
  `auto-fit` lets the two cards fill the Hub width.
- Initial P2: clickable cards lacked a visible action affordance. Fix:
  `Open details` is present in each live card footer.
- Post-fix visual evidence:
  `dashboard/design/hub-card-info-comparison-v2.jpg`.

## Findings

- No actionable P0, P1, or P2 differences remain for the existing Hub design
  plus the requested information-density override.
- P3: extremely long repository or task names still rely on one-line
  truncation, with full detail available after opening the repository.

final result: passed
