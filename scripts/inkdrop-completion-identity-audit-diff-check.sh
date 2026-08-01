#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIFF_SCRIPT="${SCRIPT_DIR}/inkdrop-completion-identity-audit-diff.py"

PYTHON_BIN="${INKDROP_COMPLETION_AUDIT_PYTHON:-python3}"
LOG_DIR="${INKDROP_COMPLETION_AUDIT_LOG_DIR:-/tmp/inkdrop-regression}"
BASELINE_PATH="${INKDROP_COMPLETION_AUDIT_BASELINE_PATH:-}"
CURRENT_PATH="${INKDROP_COMPLETION_AUDIT_CURRENT_PATH:-}"
BASELINE_PATTERN="${INKDROP_COMPLETION_AUDIT_BASELINE_PATTERN:-completion-identity-audit-*-baseline*.json}"
CURRENT_PATTERN="${INKDROP_COMPLETION_AUDIT_CURRENT_PATTERN:-completion-identity-audit-*.json}"
OUTPUT_PATH="${INKDROP_COMPLETION_AUDIT_DIFF_PATH:-}"
DIFF_PREFIX="${INKDROP_COMPLETION_AUDIT_DIFF_PREFIX:-inkdrop-completion-identity-audit-diff}"
STRICT="${INKDROP_COMPLETION_AUDIT_STRICT:-1}"

mkdir -p "$LOG_DIR"

resolve_latest() {
  local directory="$1"
  local pattern="$2"
  ls -1t "${directory}/${pattern}" 2>/dev/null | head -n 1 || true
}

if [ -z "$BASELINE_PATH" ]; then
  BASELINE_PATH="$(resolve_latest "$LOG_DIR" "$BASELINE_PATTERN")"
fi

if [ -z "$CURRENT_PATH" ]; then
  CURRENT_PATH="$(resolve_latest "$LOG_DIR" "$CURRENT_PATTERN")"
fi

if [ -z "$BASELINE_PATH" ] || [ ! -f "$BASELINE_PATH" ]; then
  echo "completion-identity-audit-diff-check: baseline artifact not found" >&2
  echo "  tried: ${INKDROP_COMPLETION_AUDIT_BASELINE_PATH:-<pattern fallback>}" >&2
  echo "  pattern: ${BASELINE_PATTERN}" >&2
  echo "  log_dir: ${LOG_DIR}" >&2
  exit 3
fi

if [ -z "$CURRENT_PATH" ] || [ ! -f "$CURRENT_PATH" ]; then
  echo "completion-identity-audit-diff-check: current artifact not found" >&2
  echo "  tried: ${INKDROP_COMPLETION_AUDIT_CURRENT_PATH:-<pattern fallback>}" >&2
  echo "  pattern: ${CURRENT_PATTERN}" >&2
  echo "  log_dir: ${LOG_DIR}" >&2
  exit 3
fi

if [ "$BASELINE_PATH" = "$CURRENT_PATH" ]; then
  echo "completion-identity-audit-diff-check: baseline and current artifacts are the same file: ${BASELINE_PATH}" >&2
  echo "provide distinct paths via INKDROP_COMPLETION_AUDIT_*_PATH or use distinct generated baselines" >&2
  exit 3
fi

if [ -z "$OUTPUT_PATH" ]; then
  TIMESTAMP="$(date -u +'%Y%m%dT%H%M%SZ')"
  OUTPUT_PATH="${LOG_DIR}/${DIFF_PREFIX}-${TIMESTAMP}.json"
fi

TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

set +e
"$PYTHON_BIN" "$DIFF_SCRIPT" \
  --baseline "$BASELINE_PATH" \
  --current "$CURRENT_PATH" \
  --json \
  $([ "$STRICT" = "1" ] && echo --strict) \
  "$@" \
  >"$TMP_FILE" 2>&1
RC=$?
set -e

cat "$TMP_FILE" > "$OUTPUT_PATH"

if [ "$RC" -ne 0 ]; then
  echo "completion-identity-audit-diff-check: FAIL (exit ${RC}) log=${OUTPUT_PATH}" >&2
  cat "$TMP_FILE" >&2
  exit "$RC"
fi

echo "completion-identity-audit-diff-check: PASS log=${OUTPUT_PATH}"
cat "$TMP_FILE"
exit 0
