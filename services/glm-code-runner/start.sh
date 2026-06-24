#!/usr/bin/env bash
set -euo pipefail
ROOT=/home/wzu/qqbot
PID_FILE="$ROOT/code_runner/runner.pid"
LOG_FILE="$ROOT/logs/glm_code_runner.log"
TOKEN_FILE="$ROOT/secrets/glm_code_runner.token"
GW="$(docker inspect astrbot --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}')"
if [[ -z "$GW" ]]; then
  echo "cannot detect astrbot docker gateway" >&2
  exit 1
fi
if [[ -f "$PID_FILE" ]]; then
  OLD="$(cat "$PID_FILE" || true)"
  if [[ -n "$OLD" ]] && kill -0 "$OLD" 2>/dev/null; then
    kill "$OLD" || true
    sleep 1
  fi
fi
nohup env \
  BIND_HOST="0.0.0.0" \
  PORT=8879 \
  TOKEN_FILE="$TOKEN_FILE" \
  SANDBOX_IMAGE="soulter/astrbot:latest" \
  SANDBOX_MEMORY="256m" \
  SANDBOX_CPUS="0.5" \
  DEFAULT_TIMEOUT="8" \
  MAX_TIMEOUT="15" \
  MAX_CONCURRENCY="2" \
  python3 "$ROOT/code_runner/server.py" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "$GW" > "$ROOT/code_runner/gateway.txt"
echo "started glm-code-runner pid=$(cat "$PID_FILE") bind=0.0.0.0:8879 gateway=$GW"
