#!/usr/bin/env bash
# 优雅停止 run.sh 启动的 http.server，5 个 200ms 周期内未退则 SIGKILL
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT/.server.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "未找到 $PID_FILE，认为未由 run.sh 启动" >&2
  exit 1
fi

PID="$(tr -d ' \n\r' < "$PID_FILE")"
if ! kill -0 "$PID" 2>/dev/null; then
  echo "进程 $PID 已不存在，清理 $PID_FILE"
  rm -f "$PID_FILE"
  exit 0
fi

kill -TERM "$PID" 2>/dev/null || true
for _ in 1 2 3 4 5; do
  if ! kill -0 "$PID" 2>/dev/null; then
    break
  fi
  sleep 0.2
done

if kill -0 "$PID" 2>/dev/null; then
  echo "SIGTERM 未结束，使用 SIGKILL"
  kill -KILL "$PID" 2>/dev/null || true
fi

rm -f "$PID_FILE"
echo "已停止 (PID: $PID)"
