#!/usr/bin/env bash
set -euo pipefail

ROOT="$(mktemp -d)"
cleanup() {
  if [ -n "${BLOCKER_PID:-}" ]; then
    kill "$BLOCKER_PID" 2>/dev/null || true
    wait "$BLOCKER_PID" 2>/dev/null || true
  fi
  rm -rf "$ROOT"
}
trap cleanup EXIT

LOCK_DIR="$ROOT/locks"
STATE_DIR="$ROOT/state"
LOG="$ROOT/source-worker.log"
MARKER="$ROOT/suwayomi-ran"
mkdir -p "$LOCK_DIR" "$STATE_DIR"

cat > "$ROOT/fake-service.sh" <<EOF
#!/usr/bin/env sh
printf 'ran\n' > '$MARKER'
EOF
chmod +x "$ROOT/fake-service.sh"

# Model the observed production contention: autopilot owns its run lock while
# a long SLSKD probe also owns the legacy global source-worker/probe locks.
(
  exec 20>"$LOCK_DIR/inkdrop-series-autopilot.lock"
  exec 21>"$LOCK_DIR/inkdrop-source-worker.lock"
  exec 22>"$LOCK_DIR/inkdrop-slskd-source-probe.lock"
  /usr/bin/flock 20
  /usr/bin/flock 21
  /usr/bin/flock 22
  printf 'locked\n' > "$ROOT/blocker-ready"
  sleep 8
) &
BLOCKER_PID=$!

for _ in $(seq 1 50); do
  [ -f "$ROOT/blocker-ready" ] && break
  sleep 0.02
done
[ -f "$ROOT/blocker-ready" ] || { echo "lane lock smoke: blocker did not start" >&2; exit 1; }

started="$(date +%s)"
INKDROP_STATE_DIR="$STATE_DIR" \
INKDROP_LOCK_DIR="$LOCK_DIR" \
INKDROP_SOURCE_WORKER_LOG="$LOG" \
INKDROP_SOURCE_WORKER_PYTHON=/bin/sh \
INKDROP_SOURCE_WORKER_SCRIPT="$ROOT/fake-service.sh" \
INKDROP_SOURCE_WORKER_LOCK_SCOPE=suwayomi \
INKDROP_SOURCE_WORKER_REQUIRES_AUTOPILOT_LOCK=0 \
INKDROP_SOURCE_WORKER_OPTIONAL_LOCKS=none \
INKDROP_SOURCE_WORKER_LOCK_WAIT_SECONDS=1 \
INKDROP_SOURCE_WORKER_COMMAND_TIMEOUT_SECONDS=5 \
INKDROP_SOURCE_WORKER_MAX_RUN_SECONDS=5 \
INKDROP_SOURCE_WORKER_SLOT_DEADLINE_MINUTES= \
bash "$(dirname "$0")/inkdrop-source-worker.sh"
elapsed=$(( $(date +%s) - started ))

[ -f "$MARKER" ] || { echo "lane lock smoke: Suwayomi service was not admitted" >&2; exit 1; }
[ "$elapsed" -lt 4 ] || { echo "lane lock smoke: Suwayomi waited ${elapsed}s for unrelated SLSKD work" >&2; exit 1; }
grep -q 'scope=suwayomi label=source-worker outcome=admitted' "$LOG" || {
  echo "lane lock smoke: missing truthful lane admission evidence" >&2
  exit 1
}
grep -q 'lane-scoped-source-worker-only scope=suwayomi db_safety=sqlite-transactional-retry' "$LOG" || {
  echo "lane lock smoke: missing DB safety boundary evidence" >&2
  exit 1
}

# A real same-lane blocker remains bounded and is reported truthfully so the
# scheduler can distinguish contention from provider failure.
rm -f "$MARKER" "$ROOT/lane-blocker-ready"
(
  exec 23>"$LOCK_DIR/inkdrop-source-worker-suwayomi.lock"
  /usr/bin/flock 23
  printf 'locked\n' > "$ROOT/lane-blocker-ready"
  sleep 4
) &
LANE_BLOCKER_PID=$!
for _ in $(seq 1 50); do
  [ -f "$ROOT/lane-blocker-ready" ] && break
  sleep 0.02
done
[ -f "$ROOT/lane-blocker-ready" ] || { echo "lane lock smoke: same-lane blocker did not start" >&2; exit 1; }
set +e
INKDROP_STATE_DIR="$STATE_DIR" \
INKDROP_LOCK_DIR="$LOCK_DIR" \
INKDROP_SOURCE_WORKER_LOG="$LOG" \
INKDROP_SOURCE_WORKER_PYTHON=/bin/sh \
INKDROP_SOURCE_WORKER_SCRIPT="$ROOT/fake-service.sh" \
INKDROP_SOURCE_WORKER_LOCK_SCOPE=suwayomi \
INKDROP_SOURCE_WORKER_REQUIRES_AUTOPILOT_LOCK=0 \
INKDROP_SOURCE_WORKER_OPTIONAL_LOCKS=none \
INKDROP_SOURCE_WORKER_LOCK_WAIT_SECONDS=1 \
bash "$(dirname "$0")/inkdrop-source-worker.sh"
same_lane_rc=$?
set -e
kill "$LANE_BLOCKER_PID" 2>/dev/null || true
wait "$LANE_BLOCKER_PID" 2>/dev/null || true
[ "$same_lane_rc" -eq 75 ] || { echo "lane lock smoke: same-lane contention rc=$same_lane_rc, expected 75" >&2; exit 1; }
[ ! -f "$MARKER" ] || { echo "lane lock smoke: same-lane blocker allowed duplicate execution" >&2; exit 1; }
grep -q 'scope=suwayomi label=source-worker outcome=deferred.*bound_seconds=1' "$LOG" || {
  echo "lane lock smoke: missing bounded blocker/fairness evidence" >&2
  exit 1
}

echo "inkdrop source-worker lane lock smoke: PASS"
