#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${INKDROP_COMPLETION_AUDIT_PYTHON:-python3}"
LOG_DIR="${INKDROP_COMPLETION_AUDIT_LOG_DIR:-/tmp/inkdrop-regression}"
PATTERN="${INKDROP_COMPLETION_AUDIT_DIFF_PATTERN:-inkdrop-completion-identity-audit-diff-*.json}"
ALERT_CMD="${INKDROP_COMPLETION_ALERT_CMD:-}"

if [ "${1-}" != "" ]; then
  REPORT_PATH="$1"
else
  REPORT_PATH="$(ls -1t "${LOG_DIR}/${PATTERN}" 2>/dev/null | head -n 1 || true)"
fi

if [ -z "${REPORT_PATH:-}" ] || [ ! -f "$REPORT_PATH" ]; then
  echo "completion-identity-audit-diff alert: no report found in ${LOG_DIR}/${PATTERN}" >&2
  exit 2
fi

readarray -t parsed < <("$PYTHON_BIN" - "$REPORT_PATH" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fp:
    data = json.load(fp)

status = data.get("status", "unknown")
reductions = int((data.get("status_summary") or {}).get("regressions", 0) or 0)
warnings = int((data.get("status_summary") or {}).get("warnings", 0) or 0)
baseline = data.get("baseline", {})
current = data.get("current", {})
print(status)
print(int(reductions))
print(int(warnings))
print(baseline.get("path", ""))
print(baseline.get("generated_at", ""))
print(current.get("path", ""))
print(current.get("generated_at", ""))
print(path)
PY
)

status="${parsed[0]}"
reductions="${parsed[1]}"
warnings="${parsed[2]}"
baseline_path="${parsed[3]}"
baseline_generated_at="${parsed[4]}"
current_path="${parsed[5]}"
current_generated_at="${parsed[6]}"

if [ "$status" = "regression" ] || [ "$reductions" -gt 0 ]; then
  echo "completion-identity-audit-diff alert: REGRESSION status=${status} reductions=${reductions} warnings=${warnings} baseline=${baseline_path}@${baseline_generated_at} current=${current_path}@${current_generated_at} report=${REPORT_PATH}"
  if [ -n "$ALERT_CMD" ]; then
    INKDROP_COMPLETION_DIFF_STATUS="$status" \
    INKDROP_COMPLETION_DIFF_REDUCTIONS="$reductions" \
    INKDROP_COMPLETION_DIFF_WARNINGS="$warnings" \
    INKDROP_COMPLETION_DIFF_REPORT="$REPORT_PATH" \
    bash -c "$ALERT_CMD"
    exit $?
  fi
  exit 3
fi

echo "completion-identity-audit-diff alert: OK status=${status} reductions=${reductions} warnings=${warnings} baseline=${baseline_path}@${baseline_generated_at} current=${current_path}@${current_generated_at} report=${REPORT_PATH}"
exit 0
