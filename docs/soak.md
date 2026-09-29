# Real-remote soak evidence

The issue #179 soak was the evidence gate between the 0.9 API freeze and 1.0.
It is complete, and its one-off simulator has been retired from the tree.

## What it established

The soak ran the published 0.9.0 wheel against a dedicated, disposable GitHub
repository with a CI-backed post-push verify hook. The 0.9.1 changelog entry
records the result:

- 20 landed trains at a 100% land rate;
- planned gate-failure and merge-conflict recovery;
- one real `git push --atomic` killed with SIGKILL mid-flight, whose remote
  truth matched recovery's queued verdict before the work deployed through a
  normal verified train; and
- no mergetrain runtime defect.

The harness failed closed: it required an exact target sentinel, a clean and
idle queue, authenticated GitHub access, a post-push verify hook, and an
installed wheel matching the requested version, and it classified every
unplanned operator intervention. Its state, JSONL log, and report stayed in the
target repository's ignored `.mergetrain/` directory. This is owner-operated
evidence from one repository, not a claim of external-user adoption.

The crash windows it exercised against a real remote remain covered against
local bare remotes by the [fault matrix](development.md#the-fault-matrix), which
runs on every CI leg.

## Rerunning it

The simulator, its tests, and its full operator guide remain at the `v3.1.1`
tag:

- [`scripts/soak_sim.py`](https://github.com/yongjip/mergetrain/blob/v3.1.1/scripts/soak_sim.py)
- [`tests/test_soak_sim.py`](https://github.com/yongjip/mergetrain/blob/v3.1.1/tests/test_soak_sim.py)
- [operator guide](https://github.com/yongjip/mergetrain/blob/v3.1.1/docs/soak.md):
  target contract, smoke run, full crash mode, and intervention ledger

Check them out together, then follow that guide against a released wheel:

```sh
git worktree add ../mergetrain-soak v3.1.1
```
