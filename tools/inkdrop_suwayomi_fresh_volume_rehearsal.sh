#!/usr/bin/env bash
set -euo pipefail

IMAGE="${SUWAYOMI_IMAGE:-ghcr.io/suwayomi/suwayomi-server@sha256:e59f212ccf91b26de8676a063a2db90256c148c6261535c7c477d47a43d9751b}"
case "$IMAGE" in
  *@sha256:????????????????????????????????????????????????????????????????) ;;
  *) echo "SUWAYOMI_IMAGE must be an immutable repository@sha256 reference" >&2; exit 2 ;;
esac

ROOT="$(mktemp -d /tmp/inkdrop-suwayomi-fresh-volume.XXXXXX)"
case "$ROOT" in /tmp/inkdrop-suwayomi-fresh-volume.*) ;; *) exit 3 ;; esac
NAME="inkdrop-suwayomi-fresh-${ROOT##*.}"

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  case "$ROOT" in /tmp/inkdrop-suwayomi-fresh-volume.*) rm -rf -- "$ROOT" ;; esac
}
trap cleanup EXIT

mkdir -p "$ROOT/data" "$ROOT/downloads"
chown -R 1000:1000 "$ROOT"
chmod 0750 "$ROOT/data" "$ROOT/downloads"
docker pull "$IMAGE" >/dev/null
docker run -d --name "$NAME" -P \
  -v "$ROOT/data:/home/suwayomi/.local/share/Tachidesk" \
  -v "$ROOT/downloads:/home/suwayomi/.local/share/Tachidesk/downloads" \
  "$IMAGE" >/dev/null

PORT="$(docker port "$NAME" 4567/tcp | head -n 1 | awk -F: '{print $NF}')"
test -n "$PORT"
ready=0
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/api/v1/settings/about" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
test "$ready" = 1
docker exec "$NAME" test -f /home/suwayomi/.local/share/Tachidesk/server.conf
test "$(stat -c '%u:%g' "$ROOT/data")" = "1000:1000"
test "$(stat -c '%a' "$ROOT/data")" = "750"
test "$(stat -c '%u:%g' "$ROOT/downloads")" = "1000:1000"
test "$(stat -c '%a' "$ROOT/downloads")" = "750"
test "$(docker inspect "$NAME" --format '{{.State.Status}}')" = "running"

printf '{"ok":true,"image":"%s","config_owner":"1000:1000","directory_mode":"0750","server_conf":true,"health":true}\n' "$IMAGE"
