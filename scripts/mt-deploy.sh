#!/usr/bin/env bash
# Guarded deploy for phone / Dispatch use.
# Shows exactly what will ship; only deploys when you pass --confirm (or -y).
# Usage:
#   scripts/ty-deploy.sh                 # dry run: print what would ship, exit 2
#   scripts/ty-deploy.sh --confirm       # actually run: mergetrain run-batch --deploy
# Override the binary for testing: MERGETRAIN_BIN="python3 -m mergetrain"
set -eo pipefail
TY="${MERGETRAIN_BIN:-mergetrain}"

CONFIRM=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --confirm|-y) CONFIRM=1 ;;
    *) ARGS+=("$a") ;;
  esac
done

doc="$($TY doctor --json "${ARGS[@]}")"
sta="$($TY status --json "${ARGS[@]}")"

echo "== What will ship =="
TY_DOC="$doc" TY_STA="$sta" python3 <<'PY'
import json, os
d = json.loads(os.environ["TY_DOC"]); s = json.loads(os.environ["TY_STA"])
c = d.get("counts", {})
print(f"integration: {d.get('git', {}).get('integration_ref')}")
print(f"queued={c.get('queued', 0)} blocked={c.get('blocked', 0)} failed={c.get('failed', 0)} "
      f"| next_action={d.get('next_action')}")
q = [j for j in s.get('jobs', []) if j['status'] == 'queued']
for j in q:
    print(f"  will merge #{j['id']} {j['branch']}")
if not q:
    print("  (no queued jobs to deploy)")
PY

if [ "$CONFIRM" -ne 1 ]; then
  printf '\nDRY RUN — nothing deployed. To deploy, re-run with --confirm:\n  scripts/ty-deploy.sh --confirm %s\n' "${ARGS[*]}"
  exit 2
fi

echo
echo "Deploying…"
res="$($TY run-batch --deploy --json "${ARGS[@]}")"
TY_RES="$res" python3 <<'PY'
import json, os
for j in json.loads(os.environ["TY_RES"]).get("jobs", []):
    sha = (j.get('deploy_sha') or '')[:10]
    note = (j.get('note') or '').splitlines()
    note = note[0] if note else ''
    print(f"#{j['id']} {j['status']:<11} {j['branch']}"
          + (f" sha={sha}" if sha else "") + (f"  - {note}" if note else ""))
PY
