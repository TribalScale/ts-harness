#!/usr/bin/env bash
set -euo pipefail

ARG="${1:-workspace}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$ARG" = /* ]]; then
    TARGET="$ARG"
else
    TARGET="$REPO_ROOT/$ARG"
fi

if [[ ! -d "$TARGET" ]]; then
    echo "ERROR: directory not found: $TARGET"
    exit 1
fi

echo "Checking for unfilled ALL_CAPS {{PLACEHOLDER}} tokens in ${TARGET}/ ..."
MATCHES="$(grep -RIEn \
    --include='*.md' \
    --include='*.json' \
    --include='*.yaml' \
    --include='*.yml' \
    --include='*.tmpl' \
    --include='*.env' \
    '\{\{[A-Z][A-Z0-9_]*\}\}' "$TARGET" 2>/dev/null || true)"

if [[ -n "$MATCHES" ]]; then
    echo "$MATCHES"
    echo "Found unfilled {{PLACEHOLDER}} patterns in markdown and config files. Fill them before deploying."
    exit 1
fi

echo "No unfilled ALL_CAPS placeholder patterns found in markdown and config files under ${TARGET}/."
