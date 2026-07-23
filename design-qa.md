# Current / Next Batch Dashboard Design QA

## Evidence

- Source visual truth:
  `dashboard/design/dashboard-contextual-inspector-target.png`
- Scoped user override:
  - Rename the runner-claimed cohort to `Current batch`.
  - Freeze that cohort when the runner starts.
  - Show requests queued afterward in a separate `Next batch`.
  - Show current/next batch counts on Hub project cards.
- Browser-rendered implementation:
  `dashboard/design/dashboard-current-next-batch-final.jpg`
- Full-page implementation:
  `dashboard/design/dashboard-current-next-batch-full-final.jpg`
- Same-frame source/implementation comparison:
  `dashboard/design/dashboard-batch-comparison-final.png`
- Hub overview:
  `dashboard/design/hub-multi-project-final.jpg`
- Local previews:
  - Single-repo demo: `http://127.0.0.1:4173/`
  - Multi-repo Hub: `http://127.0.0.1:4175/`
- Source pixels: `1505 × 1045`.
- Implementation pixels: `1505 × 1045`.
- CSS viewport: `1505 × 1045`; density `1`.
- State: dark theme, batch `#1–#4` fixed and validated with `#2`
  blocked, request `#5` queued afterward for the next batch.

## Full-view comparison

- The current-batch card and contextual inspector retain the target's desktop
  grid, proportions, dark operational palette, borders, radii, phase rail,
  FIFO table, conflict treatment, and validated-train treatment.
- The intentional copy change from `FIFO train` to `Current batch` explains
  the fixed boundary without changing the hierarchy.
- `Next batch · 1 waiting` sits below the fixed batch as a separate blue
  waiting-state region. It does not visually or semantically join the green
  validated train.
- Hub uses the same tokens and adds a compact `Validated batch 4 → Next batch
  1` strip to the project card.

## Focused region comparison

- Current batch boundary: the eyebrow, title, and boundary sentence establish
  when membership freezes.
- Next batch: request `#5` has its task, branch, waiting state, and FIFO order
  visible without competing with the blocked/ready inspector.
- Contextual inspector: the Git-conflict label is a separate status row, so the
  request title no longer wraps against it.
- Hub: the demo project opens to the full single-project view and `← All repos`
  returns to the overview.

## Required fidelity surfaces

- Fonts and typography: Inter Variable and JetBrains Mono Variable remain the
  implementation fonts. Headings, status text, task names, and machine
  identifiers preserve the target hierarchy and truncation behavior.
- Spacing and layout rhythm: the current card is `742px` minimum height at the
  desktop target; 72px request rows and flexible inspector panels align the two
  primary columns. The next batch begins on the following visual band.
- Colors and tokens: existing blue/green/red semantic tokens are reused;
  no new palette or decorative treatment was introduced.
- Image and icon fidelity: the UI has no raster content assets. Existing
  Phosphor icons and the product's current brand mark remain unchanged.
- Copy and content: batch terminology matches the queue policy. Requests added
  after the locked cohort explicitly say `Not in current batch`.

## Interaction and runtime checks

- Demo snapshot loaded over SSE with current batch `#1–#4` and queued `#5`.
- Hub rendered three project cards, including isolated configuration/schema
  errors and the healthy demo project.
- Hub project drill-down and return-to-overview both worked.
- Browser console errors: none on the demo or Hub.
- The narrow layout was observed with stacked inspector/next-batch regions and
  the existing internally scrollable request table. No separate mobile visual
  target was supplied, so mobile was not used for pixel-fidelity grading.

## Comparison history

- Initial implementation made the main batch card too vertically dense and
  left unused space below the inspector content.
- Fix: increased desktop card/row rhythm and allowed both contextual panels to
  share the full inspector height.
- Post-fix evidence:
  `dashboard/design/dashboard-batch-comparison-final.png`.
- The conflict status initially shared a line with the job title.
- Fix: moved it to its own icon/status row.
- Post-fix evidence:
  `dashboard/design/dashboard-current-next-batch-final.jpg`.

## Findings

- No actionable P0, P1, or P2 differences remain for the selected desktop
  target plus the approved batch-boundary override.
- P3: a dedicated mobile mock would allow a stricter small-screen typography
  comparison.

final result: passed
