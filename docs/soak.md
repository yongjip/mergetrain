# Real-remote soak

The issue #179 soak is the evidence gate between the 0.9 API freeze and 1.0.
`scripts/soak_sim.py` accelerates repetitive work, but it deliberately refuses
to create or guess its target. The operator owns target setup, release
selection, and intervention triage.

## Use a disposable repository

Create a dedicated GitHub repository that can safely have `main` reset locally
and updated by mergetrain. Do not point the harness at mergetrain itself, a
production repository, a local bare remote, or a clone with uncommitted work.

Commit this exact sentinel on `main`, replacing `owner/name` with the target's
lowercase GitHub identity:

```json
{
  "version": 1,
  "purpose": "mergetrain-soak-target",
  "repository": "owner/name"
}
```

The file name is `.mergetrain-soak-target.json`. The harness checks both the
local file and `origin/main`, then requires the same identity through
`--confirm-repo`. HTTPS remotes with inline credentials are refused; use Git's
credential helper with a clean remote URL. An authenticated `gh api` read of
that exact repository must also succeed; this prevents a GitHub verify hook
from silently running in an environment with no forge credentials.

## Target contract

The target needs:

- `src/soaktarget/core.py` with a single-line
  `def add(a: int, b: int) -> int:` implementation;
- a `tests/` directory and a CI workflow for `main`;
- Ruff and unittest available to local gate commands;
- a committed `.mergetrain.yaml` using `origin`, integration branch `main`,
  push refs `[main]`, named `ruff` and `tests` gates, and at least one
  post-push verify hook that fails when the target CI run is missing, times
  out, or concludes unsuccessfully; and
- `.mergetrain/` ignored. A normal `mergetrain init`/first database open creates
  that local ignore boundary.

Keep direct `main` updates available to the soak identity. A protected branch
that rejects direct pushes tests a different, valid failure mode but cannot
land the required trains.

## Pin the released wheel and baseline

Run the soak against the published artifact rather than a source checkout:

```sh
python3.12 -m venv /tmp/mergetrain-090-soak
/tmp/mergetrain-090-soak/bin/pip install mergetrain==0.9.0
/tmp/mergetrain-090-soak/bin/mergetrain version --json
```

Choose an explicit UTC baseline at or after the release. The first invocation
persists it under `.mergetrain/soak-state.json`; later invocations fail if a
different baseline is supplied. Every `stats` read then uses `--since` with
that persisted value, so pre-release history cannot satisfy the soak.

## Smoke first

Exercise success, gate failure/retry, and conflict/dismiss/resubmit without
killing a push:

```sh
python3 scripts/soak_sim.py \
  --repo /path/to/target \
  --confirm-repo owner/name \
  --expected-version 0.9.0 \
  --baseline 2026-07-26T01:56:04Z \
  --mt /tmp/mergetrain-090-soak/bin/mergetrain \
  --skip-crash \
  --target-landed 6
```

A successful smoke exits zero but leaves the report's overall crash criterion
unchecked. Run it again at a clean checkpoint to prove the persisted namespace,
baseline, recovery count, and branch numbering resume without collisions.

## Full soak and crash exercise

Remove `--skip-crash` and use the default target of 22 landed trains (two above
the issue's minimum):

```sh
python3 scripts/soak_sim.py \
  --repo /path/to/target \
  --confirm-repo owner/name \
  --expected-version 0.9.0 \
  --mt /tmp/mergetrain-090-soak/bin/mergetrain
```

The full mode refuses a target below 20. It attempts to observe and SIGKILL only
a `git push --atomic` process descended from the runner it launched. After one
real kill it independently reads `origin/main`, runs `recover` (which includes
`reconcile --apply`), compares the recorded job with the remote SHA, and
re-runs post-push verify when recovery proves the deploy landed. A queued
not-landed outcome is shipped through a later normal train, never by recovery.

Three timing misses leave `crash_status=failed` and make the run fail. Inspect
the JSONL evidence before explicitly using `--reset-crash-attempts`. An
ambiguous or post-kill exception becomes `needs_triage` and cannot be reset
blindly.

## Record unplanned interventions

Planned harness recovery actions are recorded as `classification=expected`.
Every unplanned operator action needs a bug or documentation-gap issue:

```sh
python3 scripts/soak_sim.py \
  --repo /path/to/target \
  --confirm-repo owner/name \
  --expected-version 0.9.0 \
  --mt /tmp/mergetrain-090-soak/bin/mergetrain \
  --record-intervention reconcile \
  --classification bug \
  --reason "remote and queue required manual comparison" \
  --issue-url https://github.com/owner/name/issues/123
```

The command records evidence but performs no queue mutation. Run the actual
operator command separately, inspect remote truth, then record what happened.
If a deliberate crash already produced a matching remote verdict and the
operator clears its remaining attention state, the ledger update can close the
persisted crash criterion.

## Evidence and exit

Defaults stay under the ignored target state directory:

- `.mergetrain/soak-state.json` — baseline, namespace, scenario numbering,
  recovery events, crash status, and final completion flag;
- `.mergetrain/soak-log.jsonl` — append-only CLI calls, remote snapshots,
  verdicts, and classified interventions; and
- `.mergetrain/soak-report.md` — the current issue #179 checklist plus the
  baseline-scoped `mergetrain stats --json` payload.

Full mode exits zero only when the session target is reached, a recovery path
was exercised, the deliberate crash is resolved, every recorded intervention
is classified, and no queue/runner/verify-recovery attention remains. Any
local-vs-remote deployed verdict mismatch stops immediately: that is the 1.0
gate, not a warning to waive.
