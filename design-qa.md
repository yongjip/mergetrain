# Compact Hub Repository Cards Design QA

## Evidence

- Source visual truth:
  `dashboard/design/hub-card-info-implementation-v2.jpg`
- Scoped user override: preserve the existing operational information while
  reducing card height and unused space.
- Browser-rendered implementation:
  `dashboard/design/hub-card-compact-implementation-v3.jpg`
- Same-frame full comparison:
  `dashboard/design/hub-card-compact-comparison-v3.jpg`
- Focused card comparison:
  `dashboard/design/hub-card-compact-focused-v3.jpg`
- Local preview: `http://127.0.0.1:4175/#`
- Source pixels: `1280 × 720`.
- Implementation pixels: `1280 × 720`.
- CSS viewport: `1280 × 720`; density `1`.
- State: dark theme, two real registered repositories. Mergetrain has validated
  train `#34` awaiting deploy approval; teratorn has a clear queue after
  deployment `#53`. The reference captured teratorn at `#50`; that difference
  is live repository progress, not a visual substitution.

## Full-view comparison

- The existing dark product shell, top navigation, rollup band, two-card
  structure, borders, radii, state colors, content hierarchy, and footer remain.
- Approximate card height fell from 386px to 287px, a 26% reduction, while the
  rollup, path, policy, operational state, work identity, activity age, facts,
  target, runner, and details affordance remain visible.
- The grid still fills the available width with two aligned cards, now ending
  close to their content instead of carrying excess vertical padding.

## Focused region comparison

- Repository path and policy chips now share one metadata row, removing a
  redundant vertical band without hiding either value.
- Section gaps and panel padding are reduced consistently; the operational
  headline remains the strongest read, followed by work identity and facts.
- Both cards retain equal height and aligned operational, fact, and footer rows
  despite their different repository states.

## Required fidelity surfaces

- Fonts and typography: Inter Variable and JetBrains Mono Variable remain the
  application fonts. The hierarchy is retained at a more compact scale, with
  operational headlines at readable body emphasis and metadata in mono.
- Spacing and layout rhythm: outer card padding, section padding, and vertical
  gaps are compressed to a consistent 6–8px rhythm. Existing 6–7px radii and
  the aligned three-column fact rows remain.
- Colors and tokens: existing blue, green, amber, red, surface, and border
  tokens are reused. No decorative palette was introduced.
- Image quality and asset fidelity: the Hub has no raster content assets.
  Existing Phosphor icons provide status, branch, runner, and disclosure
  affordances.
- Copy and content: no informational field was removed. Visible copy remains
  sourced from actual snapshot state, with approval-pending distinct from
  completed validation.

## Interaction and runtime checks

- Mergetrain card opened the real `#34` repository detail and returned to Hub.
- Teratorn card opened the real queue-clear repository detail and returned to Hub.
- The whole card remains keyboard/button semantic and `Open details` makes the
  drill-down visible.
- Browser console errors: none in the final run.
- Dashboard unit tests: 7 passed.
- Dashboard backend tests: 13 passed.
- Production dashboard build: passed.

## Comparison history

- Initial P1: cards were roughly one-third taller than their information
  required. Fix: natural-height cards with reduced outer and section padding.
- Initial P2: path and policy occupied separate rows. Fix: one responsive
  metadata row.
- Initial P2: repeated 10–12px section gaps made the card read as a stack of
  large panels. Fix: a consistent compact 6–8px rhythm.
- Post-fix visual evidence:
  `dashboard/design/hub-card-compact-comparison-v3.jpg`.

## Findings

- No actionable P0, P1, or P2 differences remain for the requested compactness
  override.
- P3: extremely long repository or task names still rely on one-line
  truncation, with full detail available after opening the repository.

final result: passed
