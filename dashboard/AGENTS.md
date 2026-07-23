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
- Do not reserve permanent width for explanatory sidebar copy. Let the current
  train use the full workspace during normal queue and running states; show a
  contextual inspector only for blocked work that needs repair or a validated
  train that needs explicit approval. Keep logs, heartbeat, and history in the
  lower operational-detail drawer.
- Name the runner-claimed cohort `Current batch`. Its membership freezes when
  the runner starts. Requests enqueued afterward must remain visibly separate
  as `Next batch`; never imply that they joined the running or validated train.
- In Hub, keep per-project state scannable and include current/next batch counts
  on each project card when a batch exists.
- Describe repos excluded from Hub daemon execution as `manual deploy`, not
  `daemon off`: Hub monitoring remains active while unattended `--auto`
  execution is disabled.
- Keep the interface read-only, single-repository, desktop-first, and local-only for v0.1.
- Keep logs and secondary runner detail collapsed by default so the current
  train, blocked request, surviving validated train, and next action remain the
  first read.
- Show actual runner phases, heartbeat freshness, blocked reason, activity, and the next safe action.
- Prefer the dark operational canvas for the demo, with blue active state,
  green success, amber attention, red failure, thin borders, and restrained
  radii. Preserve the existing light theme as an optional user preference.
- Avoid literal train illustrations and action controls; the track is an information model, not decoration.
- Use one dominant lifecycle state at a time: `Running`, `Awaiting deploy
  approval`, or `Deployed`.
- A validated train must say `Tests passed · Not on main yet` and use amber for
  its pending approval. Reserve green for completed merge and test outcomes.
- Keep the status workspace at its natural content height. Do not reserve a
  permanent sidebar or fixed vertical space; collapse operational detail until
  the user asks for it.
- Derive the live workspace step from the runner snapshot. Never default a real
  running batch to the final or validated step.
- Hub repository cards must answer four questions without drill-down: what
  state the repository is in, which current or latest request that state refers
  to, when workflow activity last changed, and where details can be opened.
- In Hub, label a deploy-eligible validated train as amber `APPROVAL`, never
  green `READY`. Keep the completed validation chip green.
- Let small repository sets fill the Hub grid with `auto-fit`; do not leave an
  empty phantom column beside sparse cards.
