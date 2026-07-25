#!/usr/bin/env bash
# Post-push verify hook: confirm GitHub Actions accepted the SHA we just pushed.
#
# mergetrain gates run on one machine, so they cannot see the Windows and
# Python 3.10-3.14 legs that only CI runs. Without this hook a deploy that
# breaks such a leg lands silently -- `main` stayed red for two days that way
# after the demo command landed. A failure here is recorded as a post-push
# verify warning against the deployed job rather than as a failed deploy: the
# push already happened, so the honest outcome is "landed, and CI disagrees".
#
# Skips (exit 0) when `gh` cannot answer at all, because an unavailable CLI is
# not evidence about the commit. Waits are bounded; "still running" is reported
# as needing attention, since unknown is not the same as passing.
set -euo pipefail

wait_seconds="${MERGETRAIN_CI_WAIT_SECONDS:-900}"
poll_seconds="${MERGETRAIN_CI_POLL_SECONDS:-15}"
workflow="${MERGETRAIN_CI_WORKFLOW:-ci.yml}"
sha="$(git rev-parse HEAD)"

if ! command -v gh >/dev/null 2>&1; then
  echo "verify-ci: gh is not installed; skipping the CI check for ${sha}"
  exit 0
fi
if ! gh auth token >/dev/null 2>&1; then
  echo "verify-ci: gh has no credentials; skipping the CI check for ${sha}"
  exit 0
fi

echo "verify-ci: waiting up to ${wait_seconds}s for ${workflow} on ${sha}"
deadline=$(($(date +%s) + wait_seconds))
status=""
url=""

while :; do
  # `select` keeps an empty result empty: without it `.[0]` on no runs renders
  # the literal string "null" and the report below would claim a run exists.
  run="$(
    gh run list --commit "$sha" --workflow "$workflow" --limit 1 \
      --json status,conclusion,url \
      --jq '.[0] | select(. != null) | "\(.status)\t\(.conclusion)\t\(.url)"' \
      2>/dev/null || true
  )"

  if [ -n "$run" ]; then
    status="$(printf '%s' "$run" | cut -f1)"
    conclusion="$(printf '%s' "$run" | cut -f2)"
    url="$(printf '%s' "$run" | cut -f3)"

    if [ "$status" = "completed" ]; then
      if [ "$conclusion" = "success" ]; then
        echo "verify-ci: ${workflow} passed for ${sha}"
        echo "verify-ci: ${url}"
        exit 0
      fi
      echo "verify-ci: ${workflow} concluded ${conclusion} for ${sha}"
      echo "verify-ci: ${url}"
      exit 1
    fi
  fi

  if [ "$(date +%s)" -ge "$deadline" ]; then
    if [ -n "$url" ]; then
      echo "verify-ci: ${workflow} still ${status} after ${wait_seconds}s for ${sha}"
      echo "verify-ci: ${url}"
    else
      echo "verify-ci: no ${workflow} run reported for ${sha} after ${wait_seconds}s"
    fi
    exit 1
  fi

  sleep "$poll_seconds"
done
