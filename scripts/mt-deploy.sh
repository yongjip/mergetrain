#!/usr/bin/env bash
# Guarded Git integration for phone / Dispatch use.
# Shows exactly what will be pushed; only mutates refs with --confirm (or -y).
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
operation="$(TY_DOC="$doc" python3 -c 'import json, os; print(json.loads(os.environ["TY_DOC"])["config"]["terminology"]["action"])')"
in_progress="$(TY_DOC="$doc" python3 -c 'import json, os; print(json.loads(os.environ["TY_DOC"])["config"]["terminology"]["in_progress"])')"
completed="$(TY_DOC="$doc" python3 -c 'import json, os; print(json.loads(os.environ["TY_DOC"])["config"]["terminology"]["completed"])')"

echo "== What will ship =="
TY_DOC="$doc" TY_STA="$sta" TY_TRAIN_ID="$TRAIN_ID" python3 <<'PY'
import json, os
d = json.loads(os.environ["TY_DOC"]); s = json.loads(os.environ["TY_STA"])
c = d.get("counts", {})
config = d.get("config", {})
words = config.get("terminology", {})
action = words.get("action", "deploy")
print(f"integration: {d.get('git', {}).get('integration_ref')}")
remote = config.get("git", {}).get("remote", "origin")
specs = [f"HEAD:{ref}" for ref in config.get("git", {}).get("push_refs", [])]
print(f"atomic push target: {remote}: {', '.join(specs)}")
next_action = str(d.get('next_action')).replace('deploy', action)
print(f"queued={c.get('queued', 0)} blocked={c.get('blocked', 0)} failed={c.get('failed', 0)} "
      f"| next_action={next_action}")
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
    print(f"  multiple validated trains are pending; {action} with an explicit --train-id")
    for train in trains:
        print(f"  {train['train_id']} ({train['train_size']} jobs)")
else:
    queued = [j for j in s.get('jobs', []) if j['status'] == 'queued']
    for j in queued:
        print(f"  will merge #{j['id']} {j['branch']}")
    if not queued:
        print(f"  (no queued or validated jobs to {action})")
PY

if [ "$CONFIRM" -ne 1 ]; then
  printf '\nDRY RUN — nothing %s. To %s, re-run with --confirm:\n  scripts/ty-deploy.sh --confirm %s\n' "$completed" "$operation" "${RUN_ARGS[*]}"
  exit 2
fi

echo
echo "$in_progress…"
set +e
res="$($TY run-batch --"$operation" --json "${RUN_ARGS[@]}")"
rc=$?
set -e
TY_RES="$res" TY_COMPLETED="$completed" TY_OPERATION="$operation" python3 <<'PY'
import json, os
d = json.loads(os.environ["TY_RES"])
completed = os.environ["TY_COMPLETED"]
for j in d.get("jobs", []):
    sha = (j.get('deploy_sha') or '')[:10]
    note = (j.get('note') or '').splitlines()
    note = note[0] if note else ''
    status = completed if j['status'] == 'deployed' else j['status']
    print(f"#{j['id']} {status:<11} {j['branch']}"
          + (f" sha={sha}" if sha else "") + (f"  - {note}" if note else ""))
if not d.get("jobs") and d.get("error"):
    print(d["error"].get("message", f"{os.environ['TY_OPERATION']} failed"))
PY
exit "$rc"
