# Merge Train Status Clarity Audit

## Audit scope

- Surface: Hub repository drill-down
- State: one validated train waiting for explicit deployment approval
- User goal: answer “Is it running, finished, or waiting for me?”
- Evidence: `01-validated-awaiting-approval.jpg`

## Step 1 — Read the dominant status

Health: poor.

The largest label says `Current batch`, while the same screen also says
`Ready`, `1 validated`, and `Ready to deploy`. These are implementation terms,
not one mutually exclusive lifecycle state. A reader cannot tell whether the
batch is active or complete without combining several regions.

## Step 2 — Inspect the completed phase rail

Health: ambiguous.

Every phase is green and checked, which visually communicates final completion.
The final label `Ready` does not say what it is ready for, and the screen never
states prominently that the validated commit is not on `main` yet.

## Step 3 — Find the required next action

Health: weak.

The right panel says `Ready to deploy`, but duplicates train membership and
technical identifiers instead of leading with the operator decision. The
critical statement, “explicit approval is required,” is visually subordinate.

## Highest-impact changes

1. Use one dominant, mutually exclusive lifecycle label:
   `Running`, `Awaiting deploy approval`, or `Deployed`.
2. For a validated train, headline the state as
   `Awaiting deploy approval · 1 request`.
3. Put `Tests passed · Not on main yet` directly below the headline.
4. Rename the final phase from `Ready` to `Awaiting approval`.
5. Replace the large green inspector with a compact `Next action` panel:
   `Approve deployment to origin/main`.
6. Keep Train ID and runner history in operational details.

## Accessibility risks and limits

- The current state relies heavily on green color and check icons; the
  text alternative does not distinguish “validated” from “deployed.”
- Screenshot evidence cannot confirm keyboard focus, screen-reader
  announcements, or live-state update behavior.
