#!/usr/bin/env bash
set -eu
TARGET="${1:?target path required}"
BASELINE="${2:?baseline path required}"
cp -- "$BASELINE" "$TARGET"
printf 'rollback restored %s from %s\n' "$TARGET" "$BASELINE"
