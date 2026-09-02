#!/usr/bin/env bash
# Guarded Git integration for phone / Dispatch use.
# The CLI owns destination resolution and the deploy-plan hash; this wrapper
# only displays that canonical preview and carries the same hash into deploy.
# Usage:
#   scripts/mt-deploy.sh
#   scripts/mt-deploy.sh --confirm
#   scripts/mt-deploy.sh --confirm --train-id ID
# Override the binary for testing: MERGETRAIN_BIN="python3 -m mergetrain"
set -eo pipefail
MT="${MERGETRAIN_BIN:-mergetrain}"

CONFIRM=0
TRAIN_ID=""
PASS_ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --confirm|-y)
      CONFIRM=1
      shift
      ;;
    --train-id)
      [ "$#" -ge 2 ] || { echo "--train-id requires a value" >&2; exit 2; }
      TRAIN_ID="$2"
      shift 2
      ;;
    --train-id=*)
      TRAIN_ID="${1#*=}"
      shift
      ;;
    --preview|--deploy|--validate-only|--json|--expected-plan|--expected-plan=*)
      echo "$1 is managed by mt-deploy.sh" >&2
      exit 2
      ;;
    *)
      PASS_ARGS+=("$1")
      shift
      ;;
  esac
done

PREVIEW_ARGS=(run-batch --deploy --preview --json)
if [ -n "$TRAIN_ID" ]; then
  PREVIEW_ARGS+=(--train-id "$TRAIN_ID")
fi
PREVIEW_ARGS+=("${PASS_ARGS[@]}")

set +e
preview="$($MT "${PREVIEW_ARGS[@]}")"
preview_rc=$?
set -e
if [ "$preview_rc" -ne 0 ]; then
  if [ -n "$preview" ]; then
    MT_PREVIEW="$preview" python3 <<'PY'
import json
import os

payload = json.loads(os.environ["MT_PREVIEW"])
print(payload.get("error", {}).get("message") or payload)
PY
  fi
  exit "$preview_rc"
fi

MT_PREVIEW="$preview" python3 <<'PY'
import json
import os

payload = json.loads(os.environ["MT_PREVIEW"])
push = payload["push_plan"]
targets = ", ".join(item["spec"] for item in push.get("refs", []))
print("== Canonical deploy preview ==")
print(f"destination: {push['remote']} ({push.get('url') or 'URL unavailable'})")
print(f"atomic refs: {targets}")
print(f"validated train: {payload['train_id']} ({len(payload.get('jobs', []))} jobs)")
for job in payload.get("jobs", []):
    print(f"  #{job['id']} {job['task']} — {job['branch']} @{job['validated_head_sha'][:12]}")
print(f"deploy plan: {payload['deploy_plan_sha']}")
print(f"confirmed command: {payload['confirmed_command']}")
PY

if [ "$CONFIRM" -ne 1 ]; then
  printf '\nDRY RUN — nothing deployed. Re-run with --confirm to execute this exact plan.\n'
  exit 2
fi

read -r SELECTED_TRAIN PLAN_SHA < <(
  MT_PREVIEW="$preview" python3 -c \
    'import json, os; p=json.loads(os.environ["MT_PREVIEW"]); print(p["train_id"], p["deploy_plan_sha"])'
)

echo
echo "Deploying the confirmed plan…"
set +e
result="$($MT run-batch --deploy --train-id "$SELECTED_TRAIN" \
  --expected-plan "$PLAN_SHA" --json "${PASS_ARGS[@]}")"
rc=$?
set -e
MT_RESULT="$result" python3 <<'PY'
import json
import os

payload = json.loads(os.environ["MT_RESULT"])
for job in payload.get("jobs", []):
    sha = (job.get("deploy_sha") or "")[:12]
    note = (job.get("note") or "").splitlines()
    note = note[0] if note else ""
    print(
        f"#{job['id']} {job['status']:<11} {job['branch']}"
        + (f" sha={sha}" if sha else "")
        + (f" — {note}" if note else "")
    )
if not payload.get("jobs"):
    print(payload.get("error", {}).get("message") or payload.get("note", "no jobs"))
PY
exit "$rc"
