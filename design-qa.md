# Current Train Dashboard Design QA

## Evidence

- Source visual truth: `dashboard/design/dashboard-demo-ui-target.png`
- Browser-rendered implementation: `/private/tmp/mergetrain-dashboard-demo-ui-implementation-v2.png`
- Full-view comparison: `/private/tmp/mergetrain-dashboard-demo-ui-comparison.png`
- Focused current-train comparison: `/private/tmp/mergetrain-dashboard-demo-ui-focused-comparison.png`
- Responsive capture: `/private/tmp/mergetrain-dashboard-demo-ui-responsive-v2.png`
- Local preview: `http://127.0.0.1:4173/`
- Desktop viewport: `1512 × 1055` CSS px at device pixel ratio 1.
- Responsive viewport: `820 × 1180` CSS px at device pixel ratio 1.
- Source pixels: `1505 × 1045`.
- Implementation pixels: `1512 × 1055`.
- Comparison normalization: the source was proportionally scaled and padded to
  `1512 × 1055`; the implementation remained at its native browser capture.
- State: dark theme, preview data, four-job semantic-conflict result, details
  collapsed, replay idle at the final validated state.

## Findings

- No actionable P0, P1, or P2 issue remains.
- [P3] The implementation is intentionally denser than the generated target.
  The denser rows preserve the existing operational dashboard language and keep
  the full conflict/survivor result above the fold.
- [P3] The generated target uses placeholder task and SHA values. The
  implementation uses the real demo repository tasks, branches, SHAs, train ID,
  and timestamps.

## Required Fidelity Surfaces

- Fonts and typography: bundled Inter Variable and JetBrains Mono Variable match
  the source's sans/mono split. Headings, task names, labels, branches, SHAs,
  train identity, and timestamps use the intended hierarchy without clipping.
- Spacing and layout rhythm: the implementation preserves the compact header,
  one dominant current-train card, five-step rail, grouped four-row result, and
  narrow inspector. Thin borders and 6–10 px radii remain consistent with the
  existing dashboard.
- Colors and visual tokens: near-black canvas, cool-gray borders, blue replay
  state, red conflict group, green survivor train, and amber demo-data note
  match the source. There are no decorative gradients or glow effects.
- Image quality and asset fidelity: this screen contains no photography or
  custom illustration. All UI symbols come from the existing Phosphor icon
  dependency; no emoji, handcrafted SVG, div art, or placeholder assets were
  introduced.
- Copy and content: the implementation makes the central relationship more
  explicit than the source: `One train · 4 jobs`, `Conflict pair #1 + #2`, and
  `Safe train #3 + #4`. The inspector separately states what happened and the
  two safe next actions.
- Accessibility and behavior: the train is a named region, the phase rail is an
  ordered list, the job result uses table/row/column semantics, inspector
  content is a complementary landmark, and collapsed details use native
  `details`/`summary`.

## Browser and Functional Checks

- Snapshot and SSE connection loaded from the real disposable demo repository.
- `Play demo` replayed all five presentation states and returned to the final
  conflict-isolated/validated state.
- `Logs and runner details` expanded and exposed runner/heartbeat/event content.
- Desktop viewport: `clientWidth = scrollWidth = 1512`; no horizontal overflow.
- Responsive viewport: `clientWidth = scrollWidth = 820`; no page overflow.
- Console: zero warnings and zero errors.
- Automated checks: dashboard tests passed; production build passed; full Python
  suite passed (`310 tests`, `1 skipped`).

## Comparison History

1. The first browser capture showed a P1 semantic contradiction: a red status
   badge said `2 jobs validated`. The single badge was replaced with two
   explicit summaries: red `2 need joint fix` and green `2 validated`.
2. The revised desktop, focused table, and responsive comparisons found no
   remaining P0/P1/P2 mismatch. The implementation keeps the selected visual
   hierarchy while applying the user's correction that this must be the real
   product UI rather than an explainer diagram.

## Implementation Checklist

- [x] One current train contains all four jobs.
- [x] Exactly one shared phase rail is visible.
- [x] The semantic-conflict pair is grouped and attributed both ways.
- [x] The compatible pair is shown as one validated safe train.
- [x] The inspector explains the outcome and separates deploy/recovery actions.
- [x] Logs and secondary operational history are collapsed by default.
- [x] Demo replay changes presentation state without creating a second UI.
- [x] Desktop, responsive, interactions, console, build, and tests pass.

## Follow-up Polish

- Consider a user-selectable compact/comfortable row-density preference only
  after the demo is tried on a projector or recorded at 1080p.

final result: passed
