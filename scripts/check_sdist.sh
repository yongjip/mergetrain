#!/usr/bin/env bash
# Prove that the published source archive can install and run its own tests.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: scripts/check_sdist.sh DIST.tar.gz" >&2
  exit 2
fi

archive="$1"
python="${MERGETRAIN_SDIST_PYTHON:-python3}"
root="$(mktemp -d)"
extract="$root/source"
venv="$root/venv"
mkdir -p "$extract"
tar -xzf "$archive" -C "$extract"
package_root="$(find "$extract" -mindepth 1 -maxdepth 1 -type d -print -quit)"
if [ -z "$package_root" ]; then
  echo "sdist did not contain one package root" >&2
  exit 1
fi

for required in \
  benchmarks \
  .github/workflows/demo-gif.yml \
  .github/workflows/mcp-registry.yml \
  .github/release-allowed-signers \
  SECURITY.md \
  .mergetrain.yaml; do
  if [ ! -e "$package_root/$required" ]; then
    echo "sdist is missing required test input: $required" >&2
    exit 1
  fi
done

"$python" -m venv "$venv"
"$venv/bin/python" -m pip install --upgrade pip
"$venv/bin/python" -m pip install -e "${package_root}[dev]"
(
  cd "$package_root"
  "$venv/bin/python" -m pytest --collect-only -q
  "$venv/bin/python" -m pytest -q
)
echo "sdist self-test OK: $package_root"
