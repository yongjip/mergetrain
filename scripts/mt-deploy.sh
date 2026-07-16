#!/usr/bin/env bash
# Guarded deploy for phone / Dispatch use.
# Shows exactly what will ship; only deploys when you pass --confirm (or -y).
# Usage:
#   scripts/ty-deploy.sh                 # dry run: print what would ship, exit 2
#   scripts/ty-deploy.sh --confirm       # actually run: mergetrain run-batch --deploy
#   scripts/ty-deploy.sh --confirm --train-id ID  # select among multiple validated trains
# Override the binary for testing: MERGETRAIN_BIN="python3 -m mergetrain"
set -eo pipefail
TY="${MERGETRAIN_BIN:-mergetrain}"

CONFIRM=0
TRAIN_ID=""
COMMON_ARGS=()
RUN_ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --confirm|-y)
      CONFIRM=1
      shift
      ;;
    --train-id)
      [ "$#" -ge 2 ] || { echo "--train-id requires a value" >&2; exit 2; }
      TRAIN_ID="$2"
      RUN_ARGS+=("$1" "$2")
      shift 2
      ;;
    --train-id=*)
      TRAIN_ID="${1#*=}"
      RUN_ARGS+=("$1")
      shift
      ;;
    *)
      COMMON_ARGS+=("$1")
      RUN_ARGS+=("$1")
      shift
      ;;
  esac
done

doc="$($TY doctor --json "${COMMON_ARGS[@]}")"
sta="$($TY status --json "${COMMON_ARGS[@]}")"

echo "== What will ship =="
TY_DOC="$doc" TY_STA="$sta" TY_TRAIN_ID="$TRAIN_ID" python3 <<'PY'
import json, os
d = json.loads(os.environ["TY_DOC"]); s = json.loads(os.environ["TY_STA"])
c = d.get("counts", {})
print(f"integration: {d.get('git', {}).get('integration_ref')}")
print(f"queued={c.get('queued', 0)} blocked={c.get('blocked', 0)} failed={c.get('failed', 0)} "
      f"| next_action={d.get('next_action')}")
trains = [t for t in s.get('validated_trains', []) if t.get('deploy_eligible')]
selected_id = os.environ.get('TY_TRAIN_ID')
if selected_id:
    trains = [t for t in trains if t.get('train_id') == selected_id]
if selected_id and not trains:
    print(f"  selected validated train not found: {selected_id}")
elif len(trains) == 1:
    train = trains[0]
    print(f"validated train: {train['train_id']} ({train['train_size']} jobs)")
    for branch in train['branches']:
        print(f"  will merge #{branch['job_id']} {branch['branch']} @ {branch['validated_head_sha'][:10]}")
elif len(trains) > 1:
    print("  multiple validated trains are pending; deploy with an explicit --train-id")
    for train in trains:
        print(f"  {train['train_id']} ({train['train_size']} jobs)")
else:
    queued = [j for j in s.get('jobs', []) if j['status'] == 'queued']
    for j in queued:
        print(f"  will merge #{j['id']} {j['branch']}")
    if not queued:
        print("  (no queued or validated jobs to deploy)")
PY

if [ "$CONFIRM" -ne 1 ]; then
  printf '\nDRY RUN — nothing deployed. To deploy, re-run with --confirm:\n  scripts/ty-deploy.sh --confirm %s\n' "${RUN_ARGS[*]}"
  exit 2
fi

echo
echo "Deploying…"
set +e
res="$($TY run-batch --deploy --json "${RUN_ARGS[@]}")"
rc=$?
set -e
TY_RES="$res" python3 <<'PY'
import json, os
d = json.loads(os.environ["TY_RES"])
for j in d.get("jobs", []):
    sha = (j.get('deploy_sha') or '')[:10]
    note = (j.get('note') or '').splitlines()
    note = note[0] if note else ''
    print(f"#{j['id']} {j['status']:<11} {j['branch']}"
          + (f" sha={sha}" if sha else "") + (f"  - {note}" if note else ""))
if not d.get("jobs") and d.get("error"):
    print(d["error"].get("message", "deploy failed"))
PY
exit "$rc"
