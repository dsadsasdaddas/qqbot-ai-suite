#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
export HOME="${HOME:-/home/dora}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-dora}"
export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-1}"
PROJECT_DIR="${PROJECT_DIR:-/workspace/project}"
BUILTIN_ASSET_DIR="${BUILTIN_ASSET_DIR:-/usr/share/dora-ssr}"
MERGED_ASSET_DIR="${MERGED_ASSET_DIR:-/tmp/dora-assets}"
WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_PORT="${VNC_PORT:-5900}"
LOG_DIR="${LOG_DIR:-/tmp/dora-runtime}"
mkdir -p "$LOG_DIR" "$MERGED_ASSET_DIR" /workspace/runtime "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR" || true

overlay_assets() {
  local src="$1"
  [ -d "$src" ] || return 0
  (cd "$src" && find . -type d -print0) | while IFS= read -r -d '' d; do
    mkdir -p "$MERGED_ASSET_DIR/$d"
  done
  (cd "$src" && find . -type f -print0) | while IFS= read -r -d '' f; do
    mkdir -p "$(dirname "$MERGED_ASSET_DIR/$f")"
    ln -sf "$src/${f#./}" "$MERGED_ASSET_DIR/$f"
  done
}

find_dora() {
  if [ -n "${DORA_BIN:-}" ] && command -v "$DORA_BIN" >/dev/null 2>&1; then
    command -v "$DORA_BIN"
    return 0
  fi
  for c in Dora dora dora-ssr Dora-SSR /usr/bin/Dora /usr/bin/dora /usr/bin/dora-ssr /opt/Dora/Dora /opt/dora/Dora; do
    if command -v "$c" >/dev/null 2>&1; then command -v "$c"; return 0; fi
    if [ -x "$c" ]; then echo "$c"; return 0; fi
  done
  return 1
}

DORA_BIN_PATH="$(find_dora || true)"
if [ -z "$DORA_BIN_PATH" ]; then
  echo "Dora executable not found. Set DORA_BIN or adjust the runtime image." >&2
  sleep infinity
fi

echo "[dora-runtime] using Dora binary: $DORA_BIN_PATH"
echo "[dora-runtime] project: $PROJECT_DIR"

rm -rf "$MERGED_ASSET_DIR"
mkdir -p "$MERGED_ASSET_DIR"
overlay_assets "$PROJECT_DIR"
for builtin_subdir in Font Image; do
  if [ ! -e "$MERGED_ASSET_DIR/$builtin_subdir" ] && [ -d "$BUILTIN_ASSET_DIR/$builtin_subdir" ]; then
    ln -s "$BUILTIN_ASSET_DIR/$builtin_subdir" "$MERGED_ASSET_DIR/$builtin_subdir"
  fi
done
echo "[dora-runtime] merged assets: $MERGED_ASSET_DIR"

Xvfb "$DISPLAY" -screen 0 "${WIDTH}x${HEIGHT}x24" -nolisten tcp >"$LOG_DIR/xvfb.log" 2>&1 &
sleep 1
if [ "${START_WM:-false}" = "true" ]; then
  fluxbox >"$LOG_DIR/fluxbox.log" 2>&1 &
  sleep "${WM_STARTUP_DELAY:-3}"
else
  : >"$LOG_DIR/fluxbox.log"
fi

# VNC is loopback-only inside the container. Gateway talks to noVNC/websockify.
x11vnc -display "$DISPLAY" -localhost -nopw -forever -shared -rfbport "$VNC_PORT" >"$LOG_DIR/x11vnc.log" 2>&1 &
websockify --web=/usr/share/novnc/ "$NOVNC_PORT" "127.0.0.1:$VNC_PORT" >"$LOG_DIR/websockify.log" 2>&1 &

"$DORA_BIN_PATH" --asset "$MERGED_ASSET_DIR" >"$LOG_DIR/dora.log" 2>&1 &
DORA_PID=$!

# Readiness marker for gateway health checks.
touch /tmp/dora-runtime-ready

tail -F "$LOG_DIR"/*.log &
TAIL_PID=$!

set +e
wait "$DORA_PID"
DORA_EXIT=$?
set -e
echo "[dora-runtime] Dora exited with code $DORA_EXIT" >>"$LOG_DIR/dora.log"
wait "$TAIL_PID"
