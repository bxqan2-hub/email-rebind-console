#!/usr/bin/env bash
set -euo pipefail
TARGET="${1:-verification/rollback-test/store.py}"
BASELINE="${2:-verification/ui_baseline_store.py}"
mkdir -p "$(dirname "$TARGET")"
cp "$BASELINE" "$TARGET"
printf 'ROLLBACK_TARGET=%s\nRESTORED_HASH=' "$TARGET"
sha256sum "$TARGET" | awk '{print $1}'
