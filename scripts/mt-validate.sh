#!/usr/bin/env bash
# Phone/Dispatch convenience entrypoint. Validation never pushes.
# Usage: scripts/mt-validate.sh [--repo PATH] [--config PATH] [--json]
# Override the binary for testing: MERGETRAIN_BIN="python3 -m mergetrain"
set -eo pipefail
MT="${MERGETRAIN_BIN:-mergetrain}"

# shellcheck disable=SC2086
exec $MT validate "$@"
