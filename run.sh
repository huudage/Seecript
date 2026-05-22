#!/usr/bin/env bash
# Seecript launcher (FastAPI + uvicorn). One process serves the static frontend AND /api/*.
#
# Usage:
#   ./run.sh                  # bootstrap venv if missing, install deps, start server
#   PORT=8091 ./run.sh
#   SKIP_INSTALL=1 ./run.sh   # skip pip install on subsequent restarts
#
# Env overrides:
#   PORT             default 8090
#   HOST             default 127.0.0.1
#   PYTHON           override interpreter discovery (default: python3 -> python)
#   SKIP_INSTALL     1 = skip pip install
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$ROOT/server"
VENV_DIR="$SERVER_DIR/venv"
LOG_DIR="${LOG_DIR:-$ROOT/logs}"
PID_FILE="$ROOT/.server.pid"
OUT_LOG="$LOG_DIR/uvicorn.log"
ERR_LOG="$LOG_DIR/uvicorn.err.log"

DEFAULT_PORT=8090
DEFAULT_HOST="127.0.0.1"
PORT="${PORT:-$DEFAULT_PORT}"
HOST_BIND="${HOST:-$DEFAULT_HOST}"

mkdir -p "$LOG_DIR" "$SERVER_DIR"

# --- Pre-flight: refuse to double-start ---
if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(tr -d ' \n\r' < "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Already running (PID $OLD_PID). Run ./stop.sh first." >&2
    exit 1
  fi
  rm -f "$PID_FILE"
fi

# --- Resolve system python (only used to bootstrap venv) ---
resolve_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    if command -v "$PYTHON" >/dev/null 2>&1; then
      echo "$PYTHON"; return 0
    fi
    echo "PYTHON='$PYTHON' not found in PATH" >&2
    return 1
  fi
  if command -v python3 >/dev/null 2>&1; then echo python3; return 0; fi
  if command -v python  >/dev/null 2>&1; then echo python;  return 0; fi
  return 1
}

# --- Bootstrap venv if missing ---
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "venv missing -- bootstrapping at $VENV_DIR ..."
  SYS_PY="$(resolve_python)" || { echo "Python 3 not found." >&2; exit 1; }
  "$SYS_PY" -m venv "$VENV_DIR"
fi
VENV_PY="$VENV_DIR/bin/python"

# --- Install / refresh dependencies ---
if [[ "${SKIP_INSTALL:-0}" != "1" ]]; then
  echo "pip install -r server/requirements.txt ..."
  "$VENV_PY" -m pip install --upgrade pip --quiet
  "$VENV_PY" -m pip install -r "$SERVER_DIR/requirements.txt" --quiet
else
  echo "SKIP_INSTALL=1 -- skipping pip install."
fi

# --- Seed .env from .env.example ---
ENV_FILE="$SERVER_DIR/.env"
if [[ ! -f "$ENV_FILE" && -f "$SERVER_DIR/.env.example" ]]; then
  cp "$SERVER_DIR/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  echo "Created server/.env from .env.example. Edit to add your DeepSeek key (LLM_PROVIDER=mock by default)."
fi

# --- Port-in-use warning ---
if command -v lsof >/dev/null 2>&1; then
  if lsof -i :"$PORT" -sTCP:LISTEN -t -Pn >/dev/null 2>&1; then
    echo "WARN: port $PORT already in use:" >&2
    lsof -i :"$PORT" -sTCP:LISTEN -Pn 2>/dev/null | head -1 >&2
    echo "Set PORT=<other> and retry." >&2
    exit 1
  fi
fi

cd "$SERVER_DIR"

echo "Working dir : $SERVER_DIR"
echo "Frontend    : http://$HOST_BIND:$PORT/"
echo "API base    : http://$HOST_BIND:$PORT/api/"
echo "Docs (dev)  : http://$HOST_BIND:$PORT/docs"
echo "Logs        : $OUT_LOG / $ERR_LOG"
echo ""

nohup "$VENV_PY" -m uvicorn app.main:app \
    --host "$HOST_BIND" --port "$PORT" --log-level info \
    >"$OUT_LOG" 2>"$ERR_LOG" &

echo $! | tr -d '\n' > "$PID_FILE"

sleep 0.8
SAVED="$(tr -d ' \n\r' < "$PID_FILE")"
if ! kill -0 "$SAVED" 2>/dev/null; then
  echo "Startup failed. Last 40 lines of $ERR_LOG :" >&2
  tail -40 "$ERR_LOG" >&2 || true
  exit 1
fi

echo "Started PID: $SAVED. Stop with ./stop.sh"
