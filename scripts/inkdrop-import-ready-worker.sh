#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${INKDROP_STATE_DIR:-/state}"
LOG_DIR="${INKDROP_LOG_DIR:-$STATE_DIR/logs}"
LOG="${INKDROP_IMPORT_READY_LOG:-$LOG_DIR/inkdrop-import-ready-worker.log}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SCRIPT_DIR="${INKDROP_BIN_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"
RECONCILE_SCRIPT="${INKDROP_RECONCILE_IMPORTS_SCRIPT:-$SCRIPT_DIR/core/inkdrop_reconcile_imports.py}"
SAB_CLEANUP_SCRIPT="${INKDROP_SAB_FAILED_CLEANUP_SCRIPT:-$SCRIPT_DIR/core/inkdrop_sab_failed_cleanup.py}"
MAX_FILES="${INKDROP_IMPORT_READY_MAX_FILES:-12}"
LOCK_WAIT_SECONDS="${INKDROP_IMPORT_READY_LOCK_WAIT_SECONDS:-420}"
LOCK_DIR="${INKDROP_LOCK_DIR:-${INKDROP_STATE_DIR:-/state}/locks}"
COMMAND_TIMEOUT_SECONDS="${INKDROP_IMPORT_READY_TIMEOUT_SECONDS:-2700}"
QUICK_RECONCILE_TIMEOUT_SECONDS="${INKDROP_IMPORT_READY_RECONCILE_TIMEOUT_SECONDS:-180}"
QUICK_RECONCILE_CLIENTS="${INKDROP_IMPORT_READY_RECONCILE_CLIENTS:-qbittorrent sabnzbd}"
RECONCILE_LOCK_WAIT_SECONDS="${INKDROP_IMPORT_READY_RECONCILE_LOCK_WAIT_SECONDS:-240}"
export INKDROP_IMPORT_READY_IMPORT_TIMEOUT_SECONDS="${INKDROP_IMPORT_READY_IMPORT_TIMEOUT_SECONDS:-600}"
export INKDROP_IMPORT_READY_BATCH_TIMEOUT_SECONDS="${INKDROP_IMPORT_READY_BATCH_TIMEOUT_SECONDS:-1800}"
export INKDROP_IMPORT_READY_QUEUE_ONLY="${INKDROP_IMPORT_READY_QUEUE_ONLY:-1}"
export INKDROP_IMPORT_READY_APPLY_PLANNED_PATH="${INKDROP_IMPORT_READY_APPLY_PLANNED_PATH:-1}"
export INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS="${INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS:-90}"
mkdir -p "$LOG_DIR" "$LOCK_DIR"
echo "=== $(date -Is) import-ready worker start ===" >> "$LOG"
echo "import-ready config: max_files=$MAX_FILES lock_wait_seconds=$LOCK_WAIT_SECONDS reconcile_lock_wait_seconds=$RECONCILE_LOCK_WAIT_SECONDS state_lock_mode=import_lock_only timeout_seconds=$COMMAND_TIMEOUT_SECONDS import_timeout_seconds=$INKDROP_IMPORT_READY_IMPORT_TIMEOUT_SECONDS batch_timeout_seconds=$INKDROP_IMPORT_READY_BATCH_TIMEOUT_SECONDS queue_only=$INKDROP_IMPORT_READY_QUEUE_ONLY apply_planned_path=$INKDROP_IMPORT_READY_APPLY_PLANNED_PATH reconciled_import_sync_budget_seconds=$INKDROP_RECONCILED_IMPORT_SYNC_BUDGET_SECONDS quick_reconcile_timeout_seconds=$QUICK_RECONCILE_TIMEOUT_SECONDS quick_reconcile_clients=$QUICK_RECONCILE_CLIENTS script=$RECONCILE_SCRIPT" >> "$LOG"

run_quick_download_client_reconcile() {
  local client_args=()
  local client
  for client in $QUICK_RECONCILE_CLIENTS; do
    client_args+=(--download-client "$client")
  done
  echo "quick download-client reconcile before import-ready: clients=$QUICK_RECONCILE_CLIENTS" >> "$LOG"
  if /usr/bin/timeout "$QUICK_RECONCILE_TIMEOUT_SECONDS" "$PYTHON_BIN" "$RECONCILE_SCRIPT" --download-clients "${client_args[@]}" --json >> "$LOG" 2>&1; then
    :
  else
    rc=$?
    echo "quick download-client reconcile skipped or failed with rc=$rc" >> "$LOG"
  fi
}

run_quick_download_client_reconcile

echo "import-ready pass uses shared lock $LOCK_DIR/inkdrop-comics-import.lock; inkdrop_reconcile_imports.py waits briefly for its own DB lock" >> "$LOG"
if /usr/bin/flock -w "$LOCK_WAIT_SECONDS" "$LOCK_DIR/inkdrop-comics-import.lock" /usr/bin/timeout "$COMMAND_TIMEOUT_SECONDS" "$PYTHON_BIN" "$RECONCILE_SCRIPT" --skip-download-clients --import-ready --lock-wait-seconds "$RECONCILE_LOCK_WAIT_SECONDS" --max-files "$MAX_FILES" --json >> "$LOG" 2>&1; then
  :
else
  rc=$?
  if [ "$rc" -eq 124 ]; then
    echo "import-ready timed out after ${COMMAND_TIMEOUT_SECONDS}s with rc=$rc" >> "$LOG"
  else
    echo "import-ready skipped or failed with rc=$rc" >> "$LOG"
  fi
fi
if [ -f "$SAB_CLEANUP_SCRIPT" ]; then
  "$PYTHON_BIN" "$SAB_CLEANUP_SCRIPT" --skip-reconcile --history-limit 800 --max-delete 0 --max-completed-delete 20 --json >> "$LOG" 2>&1 || true
else
  echo "SAB failed cleanup script not found; skipped: $SAB_CLEANUP_SCRIPT" >> "$LOG"
fi
echo "Failed-download retries are handled by inkdrop_series_autopilot.py source ladder." >> "$LOG"
echo "=== $(date -Is) import-ready worker done ===" >> "$LOG"
