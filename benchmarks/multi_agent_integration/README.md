# Local multi-agent integration benchmark

This repository-local benchmark tests mergetrain with three concurrent coding
agents without requiring external users or a hosted repository. It creates a
disposable Git repository, a local bare remote, three linked worktrees, fixed
prompts, a visible cross-task invariant, and a mechanical evaluator. It adds no
product CLI, configuration, telemetry, or provider-specific core behavior.

The scenario separates two failure classes:

- `promo` and `shipping` each pass alone but fail together because a $100 order
  falls from the required $90 floor to $85;
- `reference` is independent and must remain a valid survivor.

Prepare an absent directory with the released executable under test:

```sh
python -m benchmarks.multi_agent_integration.scenario prepare \
  --run-dir /tmp/mt-multi-agent \
  --mergetrain /opt/homebrew/bin/mergetrain
```

Launch three fresh agents concurrently, one in each worktree, using the matching
file in `/tmp/mt-multi-agent/prompts/`. The prompts deliberately require a clean
commit, `doctor --json`, exact-HEAD enqueue, and a stop before deploy. Model and
permission provenance belong in the dated pilot note; the harness is
provider-neutral and does not launch an agent product itself.

After all agents stop, grade their committed state and queue handoff:

```sh
python -m benchmarks.multi_agent_integration.scenario evaluate \
  --run-dir /tmp/mt-multi-agent
```

Success requires all three individual suites to pass, all worktrees to be
clean, each latest branch job to record the exact current HEAD, `origin/main` to
remain unchanged, both independent pairs to pass, and only `promo+shipping` to
fail with the expected `$85 < $90` invariant violation.

The evaluator does not run or deploy a train. Use one runner after the handoff,
inspect `status --json`, and preserve the normal boundary: without prior
bounded unattended approval, present one human-readable summary of the tasks,
destination, gates, and reassembly risk. Keep the exact `train_id` as internal
binding evidence rather than asking the user to repeat it. A blocked branch is
fixed in its owning worktree, committed, and handed back through `retry`.
