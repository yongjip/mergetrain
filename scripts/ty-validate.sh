#!/usr/bin/env bash
# Validate the queued merge train (never pushes). Safe to run anytime.
# Usage: scripts/ty-validate.sh [--repo PATH] [--config PATH]
# Override the binary for testing: TRAINYARD_BIN="python3 -m trainyard"
set -eo pipefail
TY="${TRAINYARD_BIN:-trainyard}"

echo "Validating queued train (no push)…"
res="$($TY run-batch --validate-only --json "$@")"

TY_RES="$res" python3 <<'PY'
import json, os
d = json.loads(os.environ["TY_RES"])
jobs = d.get("jobs", [])
if not jobs:
    print(d.get("note", "no queued jobs"))
    raise SystemExit
for j in jobs:
    note = (j.get('note') or '').splitlines()
    note = note[0] if note else ''
    print(f"#{j['id']} {j['status']:<11} {j['branch']}" + (f"  - {note}" if note else ""))
PY
