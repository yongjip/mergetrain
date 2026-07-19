#!/usr/bin/env bash
# Validate the queued merge train (never pushes). Safe to run anytime.
# Usage: scripts/mt-validate.sh [--repo PATH] [--config PATH]
# Override the binary for testing: MERGETRAIN_BIN="python3 -m mergetrain"
set -eo pipefail
TY="${MERGETRAIN_BIN:-mergetrain}"

echo "Validating queued train (no push)…"
set +e
res="$($TY run-batch --validate-only --json "$@")"
rc=$?
set -e

TY_RES="$res" python3 <<'PY'
import json, os
d = json.loads(os.environ["TY_RES"])
jobs = d.get("jobs", [])
if not jobs:
    print(d.get("error", {}).get("message") or d.get("note", "no queued jobs"))
    raise SystemExit
for j in jobs:
    note = (j.get('note') or '').splitlines()
    note = note[0] if note else ''
    print(f"#{j['id']} {j['status']:<11} {j['branch']}" + (f"  - {note}" if note else ""))
train_ids = sorted({j.get('train_id') for j in jobs if j.get('train_id')})
for train_id in train_ids:
    print(f"validated train: {train_id}")
PY
exit "$rc"
