#!/usr/bin/env bash
# Phone/Dispatch convenience entrypoint. The CLI owns preview, confirmation,
# plan binding, validation, push, and verification.
# Usage: scripts/mt-deploy.sh [--repo PATH] [--config PATH]
# Override the binary for testing: MERGETRAIN_BIN="python3 -m mergetrain"
set -eo pipefail
MT="${MERGETRAIN_BIN:-mergetrain}"

# shellcheck disable=SC2086
exec $MT deploy "$@"
