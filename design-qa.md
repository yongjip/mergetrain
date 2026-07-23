# Hub Priority Ledger Design QA

## Evidence

- Source visual truth:
  `dashboard/design/hub-table-target-option-1.png`
- Normalized source:
  `dashboard/design/hub-table-target-normalized.png`
- Browser-rendered implementation:
  `dashboard/design/hub-table-implementation-v3.png`
- Full-view comparison:
  `dashboard/design/hub-table-comparison-v3.png`
- Focused rollup and table comparison:
  `dashboard/design/hub-table-focused-v3.png`
- Responsive evidence:
  `dashboard/design/hub-table-responsive-760.png`
- Local preview: `http://127.0.0.1:4175/#`
- Source pixels: `1672 × 941`; normalized to `1280 × 720`.
- Implementation pixels: `1280 × 720`.
- CSS viewport: `1280 × 720`; device density `1`.
- Responsive viewport: `760 × 720`; device density `1`.
- State: dark theme, live local registry with two real repositories.
  Mergetrain has validated train `#34` awaiting deploy approval. Teratorn is
  queue-clear and its latest real deployment had advanced to `#58` at capture.

## Full-view comparison

- The implementation matches the selected unified-table direction: compact
  rollup and controls, one shared column header, two comparable repository rows,
  action-first sorting, recent outcomes, runner state, and direct drill-down.
- The top product header, dark operational canvas, semantic colors, thin
  borders, restrained radii, and footer remain consistent with the source and
  the existing product.
- The final table begins and ends at the same vertical positions as the
  normalized source. Repository rows use the source's approximately 128px
  density instead of the earlier 148px implementation.
- Live data intentionally differs from the mock: teratorn advanced from
  deployment `#53` to `#58`, and relative timestamps advanced. This is expected
  for a real status surface.

## Focused region comparison

- Rollup metrics, search, and status filter align in one band and preserve the
  source's information order.
- Column alignment closely follows the source for repository identity, current
  train, queue, recent activity, runner, state, and the row disclosure arrow.
- Typography was increased after the first comparison so repository identity,
  work title, metadata, and state affordances remain readable at the compact row
  height.
- Canceled historical jobs are neutral gray in the implementation. The mock
  showed a red mark, but mapping cancellation to failure would be semantically
  incorrect; failed and blocked outcomes remain red.

## Required fidelity surfaces

- Fonts and typography: Inter Variable and JetBrains Mono Variable match the
  existing product and target. Final table headings are 13px, repository names
  17px, work titles 14px, and metadata 12px with matched weight, wrapping, and
  truncation behavior.
- Spacing and layout rhythm: 22px page margins, 18px section gap, 50px header,
  and 128px repository rows reproduce the selected compact composition.
- Colors and visual tokens: existing blue, green, amber, red, canvas, surface,
  and border tokens are reused. Approval remains amber, completed work green,
  running blue, failure red, and cancellation neutral.
- Image quality and asset fidelity: the target has no raster content inside the
  product UI. Existing Phosphor icons supply brand, search, branch, runner,
  disclosure, theme, and connection affordances. Recent-outcome marks are a
  data visualization driven by actual job state, not a decorative image.
- Copy and content: column labels and operational terminology match the target.
  Repository names, train IDs, work titles, target refs, counts, runner state,
  and timestamps come from the live snapshot rather than static mock data.

## Interaction and runtime checks

- Repository search reduced the list to the matching teratorn row.
- `Needs action` combined with a mergetrain query returned only the approval
  row; `Queue clear` returned only teratorn; resetting to `All` restored both.
- Mergetrain opened the real `#34` approval detail and returned to Hub.
- Teratorn opened its real queue-clear detail and returned to Hub.
- The `760px` layout had no horizontal overflow (`clientWidth` and
  `scrollWidth` both `760`) and retained both repository rows and filters.
- Browser console errors: none in the final run.
- Dashboard frontend tests: 7 passed.
- Dashboard backend tests: 13 passed.
- Production dashboard build: passed.

## Comparison history

- Initial P1: implementation rows were 148px high and page margins 36px, making
  the table visibly taller and narrower than the selected compact source.
  Fix: rows reduced to 128px, page margins to 22px, and section gap to 18px.
  Post-fix evidence: `dashboard/design/hub-table-comparison-v3.png`.
- Initial P2: canceled jobs were rendered as green successful history marks.
  Fix: canceled outcomes now use neutral marks while failures remain red.
  Post-fix evidence: `dashboard/design/hub-table-focused-v3.png`.
- Initial P2: table metadata and work text read smaller than the source after
  density normalization. Fix: increased table headings and row typography by
  one pixel while retaining the 128px row height.
  Post-fix evidence: `dashboard/design/hub-table-focused-v3.png`.

## Findings

- No actionable P0, P1, or P2 differences remain.
- P3: the generated target has a slightly stronger surface vignette than the
  live product. The implementation preserves the existing product canvas token
  instead of introducing a Hub-only effect.
- P3: very long repository paths and titles truncate or wrap within their
  column; full information remains available in repository detail.

final result: passed
