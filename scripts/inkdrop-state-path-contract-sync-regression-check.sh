#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGRESSION_SCRIPT="${SCRIPT_DIR}/inkdrop-state-path-contract-sync-regression.py"
PYTHON_BIN="${INKDROP_REGRESSION_PYTHON:-python3}"
API_HOST="${INKDROP_API_HOST:-127.0.0.1}"
API_PORT="${INKDROP_API_PORT:-8796}"
SSH_HOST="${INKDROP_SSH_HOST:-127.0.0.1}"
SSH_USER="${INKDROP_SSH_USER:-inkdrop}"
DB_PATH="${INKDROP_DB_PATH:-${INKDROP_STATE_DIR:-/state}/inkdrop-state.sqlite3}"
LOG_DIR="${INKDROP_REGRESSION_LOG_DIR:-/tmp/inkdrop-regression}"
OUTPUT_DIR="$LOG_DIR"
STRICT="${INKDROP_REGRESSION_STRICT:-1}"

mkdir -p "$OUTPUT_DIR"

TIMESTAMP="$(date -u +'%Y%m%dT%H%M%SZ')"
LOG_FILE="${OUTPUT_DIR}/inkdrop-state-path-contract-sync-regression-${TIMESTAMP}.json"
TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

set +e
"$PYTHON_BIN" "$REGRESSION_SCRIPT" \
  --api-host "$API_HOST" \
  --api-port "$API_PORT" \
  --ssh-host "$SSH_HOST" \
  --ssh-user "$SSH_USER" \
  --db-path "$DB_PATH" \
  $([ "$STRICT" -eq 1 ] && echo --strict) \
  "$@" \
  >"$TMP_FILE" 2>&1
RC=$?
set -e

cat "$TMP_FILE" > "$LOG_FILE"

if [ "$RC" -ne 0 ]; then
  echo "series-path-contract-sync-regression: FAIL (exit ${RC}) log=${LOG_FILE}" >&2
  cat "$TMP_FILE" >&2
  exit "$RC"
fi

echo "series-path-contract-sync-regression: PASS log=${LOG_FILE}"
cat "$TMP_FILE"
exit 0
