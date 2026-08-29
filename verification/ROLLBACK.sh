#!/usr/bin/env bash
set -euo pipefail
TARGET="${1:-verification/rollback-test/app.js}"
BASELINE="${2:-verification/ui_baseline_app.js}"
mkdir -p "$(dirname "$TARGET")"
cp "$BASELINE" "$TARGET"
printf 'ROLLBACK_TARGET=%s\nRESTORED_HASH=' "$TARGET"
sha256sum "$TARGET" | awk '{print $1}'
