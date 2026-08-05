#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${INKDROP_STATE_DIR:-/state}"
LOG_DIR="${INKDROP_LOG_DIR:-$STATE_DIR/logs}"
LOG="${INKDROP_SOURCE_WORKER_LOG:-$LOG_DIR/inkdrop-source-worker.log}"
PYTHON="${INKDROP_SOURCE_WORKER_PYTHON:-python3}"
SCRIPT="${INKDROP_SOURCE_WORKER_SCRIPT:-/app/inkdrop_source_worker_service.py}"
LOCK_WAIT_SECONDS="${INKDROP_SOURCE_WORKER_LOCK_WAIT_SECONDS:-180}"
LOCK_DIR="${INKDROP_LOCK_DIR:-${INKDROP_STATE_DIR:-/state}/locks}"
STATE_LOCK_WAIT_SECONDS="${INKDROP_STATE_WRITER_LOCK_WAIT_SECONDS:-1800}"
AUTOPILOT_LOCK_WAIT_SECONDS="${INKDROP_SOURCE_WORKER_AUTOPILOT_LOCK_WAIT_SECONDS:-600}"
AUTOPILOT_LOCK_BEFORE_SOURCE_LOCK="${INKDROP_SOURCE_WORKER_AUTOPILOT_LOCK_BEFORE_SOURCE_LOCK:-0}"
LOCK_SCOPE="${INKDROP_SOURCE_WORKER_LOCK_SCOPE:-global}"
REQUIRES_AUTOPILOT_LOCK="${INKDROP_SOURCE_WORKER_REQUIRES_AUTOPILOT_LOCK:-1}"
OPTIONAL_LOCKS="${INKDROP_SOURCE_WORKER_OPTIONAL_LOCKS:-manual-source,status-refresh,slskd-source-probe}"
COMMAND_TIMEOUT_SECONDS="${INKDROP_SOURCE_WORKER_COMMAND_TIMEOUT_SECONDS:-900}"
MAX_RUN_SECONDS="${INKDROP_SOURCE_WORKER_MAX_RUN_SECONDS:-$COMMAND_TIMEOUT_SECONDS}"
SLOT_DEADLINE_MINUTES="${INKDROP_SOURCE_WORKER_SLOT_DEADLINE_MINUTES:-}"
SLOT_DEADLINE_RESERVE_SECONDS="${INKDROP_SOURCE_WORKER_SLOT_DEADLINE_RESERVE_SECONDS:-30}"
SLOT_DEADLINE_MIN_RUN_SECONDS="${INKDROP_SOURCE_WORKER_SLOT_DEADLINE_MIN_RUN_SECONDS:-120}"
SLOT_DEADLINE_TIMEOUT_GRACE_SECONDS="${INKDROP_SOURCE_WORKER_SLOT_DEADLINE_TIMEOUT_GRACE_SECONDS:-15}"
INKDROP_SOURCE_WORKER_DONE_LOGGED=0

case "$LOCK_SCOPE" in
  ''|*[!a-zA-Z0-9_-]*)
    echo "invalid source worker lock scope: $LOCK_SCOPE" >&2
    exit 64
    ;;
esac
LOCK_SCOPE="$(printf '%s' "$LOCK_SCOPE" | tr '[:upper:]_' '[:lower:]-')"
if [ "$LOCK_SCOPE" = "global" ]; then
  SOURCE_WORKER_LOCK_PATH="$LOCK_DIR/inkdrop-source-worker.lock"
else
  SOURCE_WORKER_LOCK_PATH="$LOCK_DIR/inkdrop-source-worker-$LOCK_SCOPE.lock"
fi

mkdir -p "$(dirname -- "$LOG")" "$LOCK_DIR"

finish_source_worker_log() {
  local rc="${1:-0}"
  if [ "$INKDROP_SOURCE_WORKER_DONE_LOGGED" -eq 0 ]; then
    echo "source worker wrapper exiting with rc=$rc" >> "$LOG"
    echo "=== $(date -Is) source worker done ===" >> "$LOG"
    INKDROP_SOURCE_WORKER_DONE_LOGGED=1
  fi
}

trap 'finish_source_worker_log $?' EXIT

echo "=== $(date -Is) source worker start ===" >> "$LOG"
echo "source worker config: execute=${INKDROP_SOURCE_WORKER_EXECUTE:-0} write=${INKDROP_SOURCE_WORKER_WRITE:-0} stage_direct=${INKDROP_SOURCE_WORKER_STAGE_DIRECT:-0} stage_managed_folders=${INKDROP_SOURCE_WORKER_STAGE_MANAGED_FOLDERS:-0} handoff_download_clients=${INKDROP_SOURCE_WORKER_HANDOFF_DOWNLOAD_CLIENTS:-0} allow_network=${INKDROP_SOURCE_WORKER_ALLOW_NETWORK:-0} allow_direct_network=${INKDROP_SOURCE_WORKER_ALLOW_DIRECT_NETWORK:-0} queue_limit=${INKDROP_SOURCE_WORKER_QUEUE_LIMIT:-50} eligible_limit=${INKDROP_SOURCE_WORKER_ELIGIBLE_LIMIT:-unset} import_backlog_priority_min=${INKDROP_SOURCE_WORKER_IMPORT_BACKLOG_PRIORITY_MIN:-unset} attempt_cooldown=${INKDROP_SOURCE_WORKER_ATTEMPT_COOLDOWN_SECONDS:-0} provider_timeout_window=${INKDROP_SOURCE_WORKER_PROVIDER_TIMEOUT_WINDOW_SECONDS:-0} provider_timeout_threshold=${INKDROP_SOURCE_WORKER_PROVIDER_TIMEOUT_THRESHOLD:-0} provider_timeout_cooldown=${INKDROP_SOURCE_WORKER_PROVIDER_TIMEOUT_COOLDOWN_SECONDS:-0} provider_fetch_failure_window=${INKDROP_SOURCE_WORKER_PROVIDER_FETCH_FAILURE_WINDOW_SECONDS:-0} provider_fetch_failure_threshold=${INKDROP_SOURCE_WORKER_PROVIDER_FETCH_FAILURE_THRESHOLD:-0} provider_fetch_failure_cooldown=${INKDROP_SOURCE_WORKER_PROVIDER_FETCH_FAILURE_COOLDOWN_SECONDS:-0} record_lock_retry_attempts=${INKDROP_SOURCE_WORKER_RECORD_LOCK_RETRY_ATTEMPTS:-unset} record_lock_retry_initial_delay=${INKDROP_SOURCE_WORKER_RECORD_LOCK_RETRY_INITIAL_DELAY_SECONDS:-unset} source_lock_wait=${LOCK_WAIT_SECONDS} state_lock_wait=${STATE_LOCK_WAIT_SECONDS} autopilot_lock_wait=${AUTOPILOT_LOCK_WAIT_SECONDS} autopilot_lock_before_source_lock=${AUTOPILOT_LOCK_BEFORE_SOURCE_LOCK} lock_scope=${LOCK_SCOPE} requires_autopilot_lock=${REQUIRES_AUTOPILOT_LOCK} optional_locks=${OPTIONAL_LOCKS} command_timeout=${COMMAND_TIMEOUT_SECONDS} max_run=${MAX_RUN_SECONDS}" >> "$LOG"

lock_evidence() {
  local label="$1"
  local outcome="$2"
  local waited_seconds="$3"
  local bound_seconds="$4"
  local path="$5"
  echo "source_worker_lock_evidence scope=$LOCK_SCOPE label=$label outcome=$outcome waited_seconds=$waited_seconds bound_seconds=$bound_seconds path=$path fairness=bounded_retry" >> "$LOG"
}

acquire_state_lock() {
  local fd="$1"
  local path="$2"
  local label="$3"
  local wait_seconds="${4:-$STATE_LOCK_WAIT_SECONDS}"
  eval "exec ${fd}>\"$path\""
  echo "waiting for $label lock: $path" >> "$LOG"
  local started_at contended
  started_at="$(date +%s)"
  contended=0
  if ! /usr/bin/flock -n "$fd"; then
    contended=1
  fi
  if [ "$contended" -eq 0 ] || /usr/bin/flock -w "$wait_seconds" "$fd"; then
    echo "acquired $label lock: $path" >> "$LOG"
    lock_evidence "$label" "admitted" "$(( $(date +%s) - started_at ))" "$wait_seconds" "$path"
    return 0
  fi
  echo "state writer lock wait timed out for $label after ${wait_seconds}s; skipping source-worker pass" >> "$LOG"
  lock_evidence "$label" "deferred" "$(( $(date +%s) - started_at ))" "$wait_seconds" "$path"
  return 1
}

acquire_optional_state_lock() {
  local fd="$1"
  local path="$2"
  local label="$3"
  local wait_seconds="${4:-$STATE_LOCK_WAIT_SECONDS}"
  eval "exec ${fd}>\"$path\""
  echo "waiting for optional $label lock: $path" >> "$LOG"
  if /usr/bin/flock -w "$wait_seconds" "$fd"; then
    echo "acquired optional $label lock: $path" >> "$LOG"
    return 0
  fi
  echo "optional state writer lock wait timed out for $label after ${wait_seconds}s; continuing source-worker pass without that lock" >> "$LOG"
  return 0
}

acquire_source_worker_lock() {
  exec 7>"$SOURCE_WORKER_LOCK_PATH"
  echo "waiting for source-worker lock: $SOURCE_WORKER_LOCK_PATH" >> "$LOG"
  local started_at contended
  started_at="$(date +%s)"
  contended=0
  if ! /usr/bin/flock -n 7; then
    contended=1
  fi
  if [ "$contended" -eq 0 ] || /usr/bin/flock -w "$LOCK_WAIT_SECONDS" 7; then
    echo "acquired source-worker lock: $SOURCE_WORKER_LOCK_PATH" >> "$LOG"
    lock_evidence "source-worker" "admitted" "$(( $(date +%s) - started_at ))" "$LOCK_WAIT_SECONDS" "$SOURCE_WORKER_LOCK_PATH"
    return 0
  fi
  echo "source-worker lock wait timed out after ${LOCK_WAIT_SECONDS}s; skipping source-worker pass" >> "$LOG"
  lock_evidence "source-worker" "deferred" "$(( $(date +%s) - started_at ))" "$LOCK_WAIT_SECONDS" "$SOURCE_WORKER_LOCK_PATH"
  return 1
}

optional_lock_enabled() {
  local wanted="$1"
  case ",${OPTIONAL_LOCKS// /}," in
    *",$wanted,"*) return 0 ;;
    *) return 1 ;;
  esac
}

positive_int_or_default() {
  local value="$1"
  local default="$2"
  case "$value" in
    ''|*[!0-9]*)
      echo "$default"
      ;;
    *)
      echo "$value"
      ;;
  esac
}

slot_deadline_available_seconds() {
  local minutes_csv="$SLOT_DEADLINE_MINUTES"
  if [ -z "$minutes_csv" ]; then
    return 1
  fi
  local current_minute current_second best_delta raw minute minute_delta delta reserve available
  current_minute="$(date +%M)"
  current_second="$(date +%S)"
  current_minute=$((10#$current_minute))
  current_second=$((10#$current_second))
  best_delta=3600
  IFS=',' read -r -a deadline_minutes <<< "$minutes_csv"
  for raw in "${deadline_minutes[@]}"; do
    raw="${raw//[[:space:]]/}"
    case "$raw" in
      ''|*[!0-9]*)
        continue
        ;;
    esac
    minute=$((10#$raw))
    if [ "$minute" -lt 0 ] || [ "$minute" -gt 59 ]; then
      continue
    fi
    minute_delta=$(( (minute - current_minute + 60) % 60 ))
    delta=$(( minute_delta * 60 - current_second ))
    if [ "$delta" -le 0 ]; then
      delta=$((delta + 3600))
    fi
    if [ "$delta" -lt "$best_delta" ]; then
      best_delta="$delta"
    fi
  done
  if [ "$best_delta" -eq 3600 ]; then
    return 1
  fi
  reserve="$(positive_int_or_default "$SLOT_DEADLINE_RESERVE_SECONDS" 30)"
  available=$((best_delta - reserve))
  if [ "$available" -lt 0 ]; then
    available=0
  fi
  echo "$available"
  return 0
}

if [ "$REQUIRES_AUTOPILOT_LOCK" = "0" ] || [ "$REQUIRES_AUTOPILOT_LOCK" = "false" ]; then
  echo "source worker lock order: lane-scoped-source-worker-only scope=$LOCK_SCOPE db_safety=sqlite-transactional-retry" >> "$LOG"
  if ! acquire_source_worker_lock; then
    finish_source_worker_log 75
    exit 75
  fi
elif [ "$AUTOPILOT_LOCK_BEFORE_SOURCE_LOCK" = "1" ] || [ "$AUTOPILOT_LOCK_BEFORE_SOURCE_LOCK" = "true" ]; then
  echo "source worker lock order: autopilot-before-source-worker" >> "$LOG"
  if ! acquire_state_lock 10 "$LOCK_DIR/inkdrop-series-autopilot.lock" series-autopilot "$AUTOPILOT_LOCK_WAIT_SECONDS"; then
    finish_source_worker_log 75
    exit 75
  fi
  if ! acquire_source_worker_lock; then
    finish_source_worker_log 75
    exit 75
  fi
else
  echo "source worker lock order: source-worker-before-autopilot" >> "$LOG"
  if ! acquire_source_worker_lock; then
    finish_source_worker_log 75
    exit 75
  fi
  if ! acquire_state_lock 10 "$LOCK_DIR/inkdrop-series-autopilot.lock" series-autopilot "$AUTOPILOT_LOCK_WAIT_SECONDS"; then
    finish_source_worker_log 75
    exit 75
  fi
fi
if optional_lock_enabled manual-source; then
  acquire_optional_state_lock 8 "$LOCK_DIR/inkdrop-manual-source-autoresolve.lock" manual-source
fi
if optional_lock_enabled status-refresh; then
  acquire_optional_state_lock 9 "$LOCK_DIR/inkdrop-series-status-refresh.lock" status-refresh
fi
if optional_lock_enabled slskd-source-probe; then
  acquire_optional_state_lock 11 "$LOCK_DIR/inkdrop-slskd-source-probe.lock" slskd-source-probe
fi

EFFECTIVE_COMMAND_TIMEOUT_SECONDS="$COMMAND_TIMEOUT_SECONDS"
if SLOT_AVAILABLE_SECONDS="$(slot_deadline_available_seconds)"; then
  SLOT_MIN_RUN_SECONDS="$(positive_int_or_default "$SLOT_DEADLINE_MIN_RUN_SECONDS" 120)"
  if [ "$SLOT_AVAILABLE_SECONDS" -lt "$SLOT_MIN_RUN_SECONDS" ]; then
    echo "slot deadline leaves ${SLOT_AVAILABLE_SECONDS}s before protected minute(s) ${SLOT_DEADLINE_MINUTES}; skipping source-worker pass" >> "$LOG"
    finish_source_worker_log 75
    exit 75
  fi
  if [ "$SLOT_AVAILABLE_SECONDS" -lt "$MAX_RUN_SECONDS" ]; then
    echo "slot deadline capped source worker max_run from ${MAX_RUN_SECONDS}s to ${SLOT_AVAILABLE_SECONDS}s before protected minute(s) ${SLOT_DEADLINE_MINUTES}" >> "$LOG"
    MAX_RUN_SECONDS="$SLOT_AVAILABLE_SECONDS"
  fi
  SLOT_TIMEOUT_GRACE_SECONDS="$(positive_int_or_default "$SLOT_DEADLINE_TIMEOUT_GRACE_SECONDS" 15)"
  SLOT_TIMEOUT_CAP_SECONDS=$((MAX_RUN_SECONDS + SLOT_TIMEOUT_GRACE_SECONDS))
  if [ "$SLOT_TIMEOUT_CAP_SECONDS" -lt "$EFFECTIVE_COMMAND_TIMEOUT_SECONDS" ]; then
    EFFECTIVE_COMMAND_TIMEOUT_SECONDS="$SLOT_TIMEOUT_CAP_SECONDS"
  fi
fi

echo "effective source worker runtime: command_timeout=${EFFECTIVE_COMMAND_TIMEOUT_SECONDS} max_run=${MAX_RUN_SECONDS} slot_deadline_minutes=${SLOT_DEADLINE_MINUTES:-unset}" >> "$LOG"

if INKDROP_SOURCE_WORKER_MAX_RUN_SECONDS="$MAX_RUN_SECONDS" /usr/bin/timeout "$EFFECTIVE_COMMAND_TIMEOUT_SECONDS" "$PYTHON" "$SCRIPT" --pretty >> "$LOG" 2>&1; then
  :
else
  rc=$?
  if [ "$rc" -eq 124 ]; then
    echo "source worker timed out after ${EFFECTIVE_COMMAND_TIMEOUT_SECONDS}s with rc=$rc" >> "$LOG"
  else
    echo "source worker skipped or failed with rc=$rc" >> "$LOG"
  fi
fi
echo "Source activation remains settings-backed; this worker only consumes enabled provider_configs through the source-worker CLI." >> "$LOG"
finish_source_worker_log 0
