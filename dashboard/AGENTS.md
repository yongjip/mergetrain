# Prototype Instructions

Run the local server yourself and open the preview in the browser available to this environment. Do not give the user server-start instructions when you can run it.

Before making substantial visual changes, use the Product Design plugin's `get-context` skill when the visual source is unclear or no longer matches the current goal. When the user gives durable prototype-specific design feedback, preferences, or decisions, record them in `AGENTS.md`.

When implementing from a selected generated mock, treat that image as the source of truth for layout, component anatomy, density, spacing, color, typography, visible content, and hierarchy.

## Selected direction

- Make the real dashboard itself the demo; do not add a separate marketing,
  explainer, or before/after surface.
- Use the selected Live Lanes direction only as a readability reference. Model
  the work as one current train containing all selected jobs, not as independent
  branch pipelines.
- Use exactly one shared phase rail. Show conflict attribution and the surviving
  validated train as grouped outcomes beneath that rail so the relationship is
  unambiguous.
- Demo mode may seed realistic data and replay presentation states, but it must
  use the same product UI and information architecture as normal operation.
- Make the primary demo the ordinary FIFO policy: enqueue several committed
  merge requests, merge them into the candidate train in order, skip a later
  request that hits a real Git conflict, continue with the remaining requests,
  then validate the surviving train for one atomic update to `main`.
- Keep semantic-conflict bisection as an advanced scenario, not the default
  product explanation. Never label a semantic incompatibility as a generic Git
  merge conflict.
- Display the current request rows newest/highest-number first so active and
  pending work stays above completed work. Preserve the real oldest-first FIFO
  sequence in the group header, order badges, and replay timing.
- Keep the interface read-only, single-repository, desktop-first, and local-only for v0.1.
- Keep logs and secondary runner detail collapsed by default so the current
  train, blocked request, surviving validated train, and next action remain the
  first read.
- Show actual runner phases, heartbeat freshness, blocked reason, activity, and the next safe action.
- Prefer the dark operational canvas for the demo, with blue active state,
  green success, amber attention, red failure, thin borders, and restrained
  radii. Preserve the existing light theme as an optional user preference.
- Avoid literal train illustrations and action controls; the track is an information model, not decoration.
