#!/usr/bin/env bash
# Build a single-file .pex deployer for the living-ai agent.
# Usage: ./build.sh [output-path]
set -euo pipefail

cd "$(dirname "$0")"

OUT="${1:-../../living-ai-deploy.pex}"

# Refresh bundled source from the parent agent dir into the package.
BUNDLE_DIR="living_ai_deploy/bundle_files"
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR"

cp ../databricks.yml "$BUNDLE_DIR/"
cp -r ../resources "$BUNDLE_DIR/"
cp -r ../src "$BUNDLE_DIR/"
cp -r ../sql "$BUNDLE_DIR/"

# Build pex with the deployer as entry point.
PEX_BIN="${PEX:-pex}"
if ! command -v "$PEX_BIN" >/dev/null 2>&1; then
  for c in pex "$HOME/Library/Python/3.11/bin/pex" "$HOME/Library/Python/3.12/bin/pex" "$HOME/Library/Python/3.13/bin/pex" "$HOME/Library/Python/3.14/bin/pex" "$HOME/.local/bin/pex"; do
    if [ -x "$c" ]; then PEX_BIN="$c"; break; fi
  done
fi

if ! command -v "$PEX_BIN" >/dev/null 2>&1; then
  echo "pex not found. Install with: pip install --user pex" >&2
  exit 1
fi

# Allow callers to override the package index (corp proxies, local mirrors)
# and the pip version used by pex's bootstrap.
EXTRA_PEX_ARGS=()
if [ -n "${PIP_INDEX_URL:-}" ]; then
  EXTRA_PEX_ARGS+=(--no-pypi --index "$PIP_INDEX_URL")
fi
if [ -n "${PEX_PIP_VERSION:-}" ]; then
  EXTRA_PEX_ARGS+=(--pip-version "$PEX_PIP_VERSION")
fi

"$PEX_BIN" \
  --venv prepend \
  -D . \
  -P "living_ai_deploy=living_ai_deploy" \
  databricks-sdk==0.105.0 \
  -e living_ai_deploy.deployer:main \
  "${EXTRA_PEX_ARGS[@]}" \
  -o "$OUT"

echo "Built: $OUT"
ls -lh "$OUT"
