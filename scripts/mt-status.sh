#!/usr/bin/env bash
# One-glance mergetrain status, formatted for phone / Dispatch use.
# Usage: scripts/ty-status.sh [--repo PATH] [--config PATH]
# Override the binary for testing: MERGETRAIN_BIN="python3 -m mergetrain"
set -eo pipefail
TY="${MERGETRAIN_BIN:-mergetrain}"

doc="$($TY doctor --json "$@")"
sta="$($TY status --json "$@")"

TY_DOC="$doc" TY_STA="$sta" python3 <<'PY'
import json, os
d = json.loads(os.environ["TY_DOC"])
s = json.loads(os.environ["TY_STA"])
g = d.get("git", {}); c = d.get("counts", {}); lock = d.get("lock")
print(f"repo: {g.get('repo_root') or '?'}")
print(f"integration: {g.get('integration_ref')} (exists={g.get('integration_ref_exists')}) | "
      f"config: {'found' if d.get('config_exists') else 'default'}")
print(f"lock: {lock['owner'] + ' [' + lock['liveness'] + ']' if lock else 'none'}")
order = ['queued', 'validated', 'in_progress', 'blocked', 'failed', 'deployed', 'canceled']
cs = " ".join(f"{k}={c[k]}" for k in order if c.get(k))
print(f"counts: {cs or 'empty'}" + (f" | auto-queued={c['auto_queued']}" if c.get('auto_queued') else ""))
print(f"next_action: {d.get('next_action')}")
for train in s.get('validated_trains', []):
    state = "deployable" if train.get('deploy_eligible') else "incomplete"
    print(f"validated train: {train.get('train_id') or 'legacy'} "
          f"jobs={train.get('train_size')} [{state}]")
jobs = s.get("jobs", [])[:10]
if jobs:
    print("recent jobs:")
    for j in jobs:
        note = (j.get('note') or '').splitlines()
        note = note[0] if note else ''
        print(f"  #{j['id']} {j['status']:<11} {j['branch']}" + (f"  - {note}" if note else ""))
PY
