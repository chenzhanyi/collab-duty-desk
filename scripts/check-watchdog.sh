#!/bin/bash
# 控制台看护自检：用 8790 假实例验证 kill 后按退避恢复、次数/时间落日志、手动停止不复活。
# 绝不触碰 8787；只写系统临时目录。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
APP="$ROOT/desktop/build/CollabMenuBar.app"
BIN="$APP/Contents/MacOS/CollabMenuBar"

if [ ! -x "$BIN" ]; then
  echo "[看护自检] 未找到已构建应用，先执行 desktop/build.sh"
  (cd "$ROOT" && bash desktop/build.sh)
fi

before_8787="$(lsof -nP -tiTCP:8787 -sTCP:LISTEN 2>/dev/null | sort | tr '\n' ' ' || true)"
if lsof -nP -iTCP:8790 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "FAIL: 8790 已被占用；自检需要独立端口。" >&2
  exit 1
fi

LOG="$(mktemp "${TMPDIR:-/tmp}/collab-watchdog-check.XXXXXX")"
trap 'rm -f "$LOG"' EXIT

set +e
"$BIN" --watchdog-selftest 2>&1 | tee "$LOG"
status=${PIPESTATUS[0]}
set -e

after_8787="$(lsof -nP -tiTCP:8787 -sTCP:LISTEN 2>/dev/null | sort | tr '\n' ' ' || true)"
if [ "$before_8787" != "$after_8787" ]; then
  echo "FAIL: 自检期间 8787 监听进程发生变化（$before_8787 → $after_8787）" >&2
  exit 1
fi
if ! grep -q "WATCHDOG-SELFTEST PASS" "$LOG"; then
  echo "FAIL: 看护自检未通过" >&2
  exit 1
fi
if [ "$status" -ne 0 ]; then
  echo "FAIL: 看护自检退出码 $status" >&2
  exit 1
fi

echo
echo "PASS: 8790 看护自检通过；8787 未被测试触碰（PID: ${before_8787:-无}）。"
