# Hub Repository Card Audit

## Audit scope

- Surface: multi-repository Hub overview.
- User goal: understand what is happening in every registered repository and
  decide where attention is needed without opening each card.
- Evidence: `01-current-hub.jpg`, captured from the live local Hub.

## Strengths

- Repository identity, path, target branch, and manual-deploy policy are easy
  to scan.
- The two-column layout supports quick comparison between repositories.
- State uses both text and color, and cards have visible keyboard focus styles.

## UX risks

1. `READY` conflicts with the actual mergetrain state. The validated train is
   still waiting for deploy approval, so the green label can be read as done.
2. The card omits the current request identity. A user cannot tell what train
   `1 validated` refers to without opening the repository.
3. There is no meaningful state timestamp. Hub connection freshness is visible,
   but the age of the repository's last workflow event is not.
4. `2 ready or idle` combines approval-pending and queue-clear repositories,
   hiding the only action currently required.
5. The idle card has no useful history. `Enqueue a committed task branch` does
   not say what most recently shipped or when it completed.
6. A clickable card has no explicit `Open details` affordance, so the available
   drill-down depends on hover or experimentation.

## Accessibility risks

- The card is keyboard-operable, but the absence of a visible action label
  makes its button role less apparent.
- Small muted metadata may become difficult to read at zoom; implementation
  should keep essential state and action copy above the metadata size.
- Screenshot evidence cannot confirm screen-reader announcement order or
  high-zoom reflow; those require runtime checks.

## Opportunity areas

- Promote one operational headline per card: `Awaiting deploy approval`,
  `Running tests`, `Queued`, `Needs attention`, or `Queue clear`.
- Show one relevant work item: current train/request when active, otherwise the
  latest deployment.
- Add workflow recency, batch membership, target ref, runner state, and a
  visible detail affordance.
- Split Hub rollup counts into approval, running, attention, and clear states.

## Recommendations

1. Change validated repository state from green `READY` to amber `APPROVAL`.
2. Add a compact current/latest work block with job number, title, branch, and
   event age.
3. Replace the generic batch strip with three comparable operational facts:
   current batch, next batch, and last activity.
4. For idle repositories, show the latest deployed request instead of an empty
   middle section.
5. Add `Open details` to the footer while preserving the whole-card hit target.
