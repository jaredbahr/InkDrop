#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${INKDROP_REGRESSION_PYTHON:-python3}"
LOG_DIR="${INKDROP_REGRESSION_LOG_DIR:-/tmp/inkdrop-regression}"
PATTERN="${INKDROP_REGRESSION_ALERT_PATTERN:-inkdrop-state-path-contract-sync-regression-*.json}"
ALERT_CMD="${INKDROP_ALERT_CMD:-}"

if [ "${1-}" != "" ]; then
  REPORT_PATH="$1"
else
  REPORT_PATH="$(ls -1t "${LOG_DIR}/${PATTERN}" 2>/dev/null | head -n 1 || true)"
fi

if [ -z "$REPORT_PATH" ] || [ ! -f "$REPORT_PATH" ]; then
  echo "regression alert: no regression artifact found in ${LOG_DIR}/${PATTERN}" >&2
  exit 2
fi

readarray -t parsed < <("$PYTHON_BIN" - "$REPORT_PATH" <<'PY'
import json
import os
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fp:
    data = json.load(fp)

observed = data.get("observed_vs_reported", {})
mismatch = bool(observed.get("mismatch"))
sync_payload = data.get("sync_payload", {})
print("true" if mismatch else "false")
print(int(observed.get("sync_reported_series_path_contracts", 0) or 0))
print(int(observed.get("observed_updated_native_root_template", 0) or 0))
print(sync_payload.get("synced_at", ""))
print(path)
PY
)

mismatched="${parsed[0]}"
reported="${parsed[1]}"
observed="${parsed[2]}"
synced_at="${parsed[3]}"

if [ "$mismatched" = "true" ]; then
  echo "regression alert: MISMATCH synced=${reported} observed=${observed} synced_at=${synced_at} report=${REPORT_PATH}"
  if [ -n "$ALERT_CMD" ]; then
    INKDROP_REGRESSION_LAST_LOG="$REPORT_PATH" \
    INKDROP_REGRESSION_MISMATCH="$mismatched" \
    INKDROP_REGRESSION_REPORTED="$reported" \
    INKDROP_REGRESSION_OBSERVED="$observed" \
    INKDROP_REGRESSION_SYNCED_AT="$synced_at" \
    bash -c "$ALERT_CMD"
    exit $?
  fi
  exit 3
fi

echo "regression alert: OK synced=${reported} observed=${observed} synced_at=${synced_at} report=${REPORT_PATH}"
exit 0
