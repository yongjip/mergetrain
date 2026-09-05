# 2048 throughput diagnostic

Question: does delegating several features reduce time to an accepted combined
result, and how much time does mergetrain add or save at integration?

Upstream: https://github.com/gabrielecirulli/2048 (MIT), frozen commit
`478b6ec346e3787f589e4af751378d06ded4cbbc`. This is a small game-logic pilot,
not a Teratorn reconstruction, visual/gameplay review, or market validation.

All implementation and integration agents use GPT-5.6 Luna with max reasoning.
The parent sets up and measures the experiment; it does not implement features
or repair conflicts. Authoring/design overhead is separate from execution.
Native subagent token usage is not available here, so do not claim actual cost
savings or convert API list prices into this account's bill.

## Frozen tasks

1. Undo: `GameManager.undo()` returns a boolean and restores board, score,
   over/won/keepPlaying before the most recent effective move, including its
   random spawn. One undo only; no-op moves retain the snapshot; restart clears
   it; best score never decreases. If moveCount exists, undo restores it too.
2. Moves: `moveCount` starts at zero, increments once per effective move only,
   persists in serialize/restore (legacy missing count defaults zero), appears
   in actuator metadata, and resets on restart.
3. Analysis: `Grid.maxTileValue()` (zero for empty), `emptyCellCount()`, and
   `availableDirections()` (sorted 0 up, 1 right, 2 down, 3 left). Directions
   include slides or merges, exclude no-ops, and do not mutate the board.
4. Storage: safely reject malformed JSON and structurally invalid saves from
   `LocalStorageManager.getGameState()`, clear the invalid entry, and return
   null. Validate square cells, positive integer size, consistent coordinates,
   power-of-two tile values >=2, finite nonnegative score, and boolean game
   flags. Valid legacy saves remain compatible.

The interface contracts and cross-feature undo/count invariant are visible to
every agent. No UI redesign is requested. Tests are frozen before implementation
and run outside the target repository; agents may read but not edit them.

## Comparison

- Sequential: one fresh Luna agent completes the four tasks in listed order in
  one worktree, verifying each, then the combined acceptance suite.
- Parallel: four fresh Luna agents start from the same upstream commit, one
  task each; at most three run concurrently because this session has three
  worker slots. Start the fourth as a slot becomes available. This is not a
  four/five-simultaneous-agent measurement.
- Fixed-artifact integration: reuse the exact parallel commits, in the same
  order, for an AI-managed Git integration and mergetrain 3.0.6 integration.
  Both use isolated local repositories/remotes and the same acceptance suite.
  Keep mechanical command timings separate from any Luna conflict repair.

The two integration replays share implementation work; they are not independent
end-to-end model trials. Sequential vs parallel has model-output variance and
only one sample. Never infer a general speedup from this pilot.

Start execution timing at agent dispatch, finish at a clean combined commit
passing every acceptance group. Record coding time, handoff/wait time, gate
count/time, conflict repair, parent orchestration, and user decisions separately.
Report failed/unfinished arms as such, without replacing them or stopping their
clock early. A user decision is distinct from a parent/tool approval check.

One initial attempt per coding worker; one bounded repair turn if acceptance
fails. Investigate correctness before latency. Bound each feature implementation and each integration attempt to ten minutes
of active execution (the four-feature sequential worker receives forty minutes); stop at unresolved
semantic ambiguity rather than inventing a requirement. Do not modify
mergetrain to improve its result during this run. Do not push to upstream or
publish these experimental game features. Preserve failed traces and patches.

Raw run evidence lives outside Git in the project's `.mergetrain/benchmarks/`
directory. Commit compact findings, frozen test hashes and exact commit IDs.

The queue gate runs baseline regression checks only, because a partial train
cannot yet satisfy all four feature contracts. Both integrations must pass the
external strict all-feature grader before being counted complete. Branch-level
feature checks are separate; an API missing from the final result is a failure.
