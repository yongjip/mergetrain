#!/usr/bin/env bash
# Phone/Dispatch convenience entrypoint. The CLI owns all status rendering.
# Usage: scripts/mt-status.sh [--repo PATH] [--config PATH] [--diagnose]
# Override the binary for testing: MERGETRAIN_BIN="python3 -m mergetrain"
set -eo pipefail
MT="${MERGETRAIN_BIN:-mergetrain}"

# MERGETRAIN_BIN intentionally supports a command prefix such as
# "python3 -m mergetrain".
# shellcheck disable=SC2086
exec $MT status "$@"
