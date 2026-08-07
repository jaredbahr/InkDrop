#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${INKDROP_STATE_DIR:-${KAVITA_ACQUIRE_STATE_DIR:-/state}}"
LOG_DIR="${INKDROP_LOG_DIR:-$STATE_DIR/logs}"
LOG="${INKDROP_SERIES_AUTOPILOT_LOG:-$LOG_DIR/series-autopilot.log}"
PYTHON="${INKDROP_SERIES_AUTOPILOT_PYTHON:-python3}"
SCRIPT="${INKDROP_SERIES_AUTOPILOT_SCRIPT:-/app/core/inkdrop_series_autopilot.py}"
LOCK_DIR="${INKDROP_LOCK_DIR:-$STATE_DIR/locks}"
mkdir -p "$LOCK_DIR"
LOCK_PATH="${INKDROP_SERIES_AUTOPILOT_LOCK:-$LOCK_DIR/inkdrop-series-autopilot.lock}"
INKDROP_CONFIG_DIR="${INKDROP_CONFIG_DIR:-$STATE_DIR}"
INKDROP_STATE_DIR="${INKDROP_STATE_DIR:-$STATE_DIR}"
KAVITA_ACQUIRE_STATE_DIR="${KAVITA_ACQUIRE_STATE_DIR:-$INKDROP_STATE_DIR}"
MAX_SERIES="${INKDROP_SERIES_AUTOPILOT_MAX_SERIES:-6}"
MISSING_RECOVERY_ENABLED="${INKDROP_MISSING_RECOVERY_ENABLED:-0}"
MISSING_RECOVERY_MAX_PER_COHORT="${INKDROP_MISSING_RECOVERY_MAX_PER_COHORT:-2}"
MISSING_RECOVERY_CONTROL="$INKDROP_STATE_DIR/missing-recovery-control.json"
AUTOMATIC_SEARCH_ENABLED="${INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED:-0}"
MAX_ISSUES_PER_SERIES="${INKDROP_SERIES_AUTOPILOT_MAX_ISSUES_PER_SERIES:-8}"
MISSING_MAX_PER_SERIES="${INKDROP_SERIES_AUTOPILOT_MISSING_MAX_PER_SERIES:-6}"
MISSING_MAX_TOTAL="${INKDROP_SERIES_AUTOPILOT_MISSING_MAX_TOTAL:-30}"
NO_RESULT_COOLDOWN_HOURS="${INKDROP_SERIES_AUTOPILOT_NO_RESULT_COOLDOWN_HOURS:-1}"
SOURCE_LOCK_WAIT_SECONDS="${INKDROP_SERIES_AUTOPILOT_SOURCE_LOCK_WAIT_SECONDS:-5}"
RETRY_SECONDS="${INKDROP_SERIES_AUTOPILOT_RETRY_SECONDS:-1800}"
MAX_RUN_SECONDS="${INKDROP_SERIES_AUTOPILOT_MAX_RUN_SECONDS:-480}"
HARD_GRACE_SECONDS="${INKDROP_AUTOPILOT_RUNTIME_HARD_GRACE_SECONDS:-90}"
TIMEOUT_KILL_AFTER_SECONDS="${INKDROP_SERIES_AUTOPILOT_TIMEOUT_KILL_AFTER_SECONDS:-30}"
OUTER_TIMEOUT_SECONDS="${INKDROP_SERIES_AUTOPILOT_OUTER_TIMEOUT_SECONDS:-}"
PROTECTED_MINUTES="${INKDROP_SERIES_AUTOPILOT_PROTECTED_MINUTES-1,16,31,46}"
PROTECTED_RESERVE_SECONDS="${INKDROP_SERIES_AUTOPILOT_PROTECTED_RESERVE_SECONDS:-60}"
PROTECTED_MIN_RUN_SECONDS="${INKDROP_SERIES_AUTOPILOT_PROTECTED_MIN_RUN_SECONDS:-240}"

export INKDROP_CONFIG_DIR
export INKDROP_STATE_DIR
export KAVITA_ACQUIRE_STATE_DIR

mkdir -p "$(dirname -- "$LOG")"

case "$(printf '%s' "$AUTOMATIC_SEARCH_ENABLED" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
  1|true|on|yes|enabled)
    ;;
  *)
    echo "=== $(date -Is) series autopilot cron start ===" >> "$LOG"
    echo "series autopilot disabled by INKDROP_QUEUE_RUNNER_AUTOPILOT_ENABLED; scheduled Automatic Search and missing recovery did not run" >> "$LOG"
    echo "=== $(date -Is) series autopilot cron done ===" >> "$LOG"
    exit 0
    ;;
esac

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

protected_available_seconds() {
  local minutes_csv="$PROTECTED_MINUTES"
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
  reserve="$(positive_int_or_default "$PROTECTED_RESERVE_SECONDS" 60)"
  available=$((best_delta - reserve))
  if [ "$available" -lt 0 ]; then
    available=0
  fi
  echo "$available"
  return 0
}

MAX_RUN_SECONDS="$(positive_int_or_default "$MAX_RUN_SECONDS" 480)"
HARD_GRACE_SECONDS="$(positive_int_or_default "$HARD_GRACE_SECONDS" 90)"
TIMEOUT_KILL_AFTER_SECONDS="$(positive_int_or_default "$TIMEOUT_KILL_AFTER_SECONDS" 30)"
PROTECTED_MIN_RUN_SECONDS="$(positive_int_or_default "$PROTECTED_MIN_RUN_SECONDS" 240)"

case "$(printf '%s' "$PROTECTED_MINUTES" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
  none|off|false|disabled|0)
    PROTECTED_MINUTES=""
    ;;
esac

echo "=== $(date -Is) series autopilot cron start ===" >> "$LOG"

if AVAILABLE_SECONDS="$(protected_available_seconds)"; then
  runnable_seconds=$((AVAILABLE_SECONDS - HARD_GRACE_SECONDS - TIMEOUT_KILL_AFTER_SECONDS))
  if [ "$runnable_seconds" -lt "$PROTECTED_MIN_RUN_SECONDS" ]; then
    echo "series autopilot protected source-worker window leaves ${AVAILABLE_SECONDS}s before minute(s) ${PROTECTED_MINUTES}; skipping pass" >> "$LOG"
    echo "=== $(date -Is) series autopilot cron done ===" >> "$LOG"
    exit 75
  fi
  if [ "$runnable_seconds" -lt "$MAX_RUN_SECONDS" ]; then
    echo "series autopilot capped max_run from ${MAX_RUN_SECONDS}s to ${runnable_seconds}s before protected minute(s) ${PROTECTED_MINUTES}" >> "$LOG"
    MAX_RUN_SECONDS="$runnable_seconds"
  fi
fi

if [ -z "$OUTER_TIMEOUT_SECONDS" ]; then
  OUTER_TIMEOUT_SECONDS=$((MAX_RUN_SECONDS + HARD_GRACE_SECONDS))
else
  OUTER_TIMEOUT_SECONDS="$(positive_int_or_default "$OUTER_TIMEOUT_SECONDS" "$((MAX_RUN_SECONDS + HARD_GRACE_SECONDS))")"
fi

echo "series autopilot cron config: max_run=${MAX_RUN_SECONDS} outer_timeout=worker-managed kill_after=disabled protected_minutes=${PROTECTED_MINUTES:-unset}" >> "$LOG"

case "$(printf '%s' "$MISSING_RECOVERY_ENABLED" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
  0|false|off|no|disabled)
    MISSING_RECOVERY_FLAG="--no-missing-recovery-enabled"
    ;;
  *)
    MISSING_RECOVERY_FLAG="--missing-recovery-enabled"
    ;;
esac
MISSING_RECOVERY_MAX_PER_COHORT="$(positive_int_or_default "$MISSING_RECOVERY_MAX_PER_COHORT" 2)"
echo "series autopilot recovery config: enabled=${MISSING_RECOVERY_FLAG#--} max_per_cohort=${MISSING_RECOVERY_MAX_PER_COHORT} max_series=${MAX_SERIES}" >> "$LOG"
if [ -e "$MISSING_RECOVERY_CONTROL" ]; then
  echo "series autopilot recovery control: $MISSING_RECOVERY_CONTROL will be enforced by the worker; ordinary Automatic Search remains enabled" >> "$LOG"
fi

cron_rc=0
exec 9>"$LOCK_PATH"
if /usr/bin/flock -n 9; then
  if "$PYTHON" "$SCRIPT" \
    --sync \
    --max-series "$MAX_SERIES" \
    "$MISSING_RECOVERY_FLAG" \
    --missing-recovery-max-per-cohort "$MISSING_RECOVERY_MAX_PER_COHORT" \
    --max-issues-per-series "$MAX_ISSUES_PER_SERIES" \
    --missing-max-per-series "$MISSING_MAX_PER_SERIES" \
    --missing-max-total "$MISSING_MAX_TOTAL" \
    --no-result-cooldown-hours "$NO_RESULT_COOLDOWN_HOURS" \
    --source-lock-wait-seconds "$SOURCE_LOCK_WAIT_SECONDS" \
    --retry-seconds "$RETRY_SECONDS" \
    --max-run-seconds "$MAX_RUN_SECONDS" >> "$LOG" 2>&1; then
    :
  else
    cron_rc=$?
    echo "series autopilot skipped or failed with rc=$cron_rc" >> "$LOG"
  fi
else
  rc=$?
  if [ "$rc" -eq 1 ]; then
    cron_rc=75
    echo "series autopilot lock busy: $LOCK_PATH; deferring pass with rc=$cron_rc" >> "$LOG"
  else
    cron_rc="$rc"
    echo "series autopilot lock failed with rc=$rc" >> "$LOG"
  fi
fi

echo "=== $(date -Is) series autopilot cron done ===" >> "$LOG"
exit "$cron_rc"
