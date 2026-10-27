#!/bin/bash
# 全新克隆自检：复制仓库到临时目录 → 构建菜单栏应用 → 校验运行所需文件与导入。
#
# 用法：
#   bash scripts/check-fresh-clone.sh [仓库根目录]
#
# 默认检查本脚本所在仓库。复制时排除 .git、构建产物与运行态
# （项目/、归档/、协作/调度台/ 的锁/日志/快照/状态文件），模拟公开仓库全新克隆。
# 不修改源仓库；临时目录在退出时清理。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO="$(cd "$HERE/.." && pwd)"
REPO="${1:-$DEFAULT_REPO}"
REPO="$(cd "$REPO" && pwd)"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

[ -f "$REPO/desktop/build.sh" ] || fail "仓库缺少 desktop/build.sh：$REPO"
[ -f "$REPO/console/server.py" ] || fail "仓库缺少 console/server.py：$REPO"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/collab-fresh-clone.XXXXXX")"
cleanup() {
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "[1/4] 复制仓库到临时目录：$TMP"
(
  cd "$REPO"
  tar -cf - \
    --exclude='.git' \
    --exclude='.DS_Store' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='desktop/build' \
    --exclude='项目' \
    --exclude='归档' \
    --exclude='协作/调度台/watch.state' \
    --exclude='协作/调度台/journal.log' \
    --exclude='协作/调度台/console.log' \
    --exclude='协作/调度台/guard.status' \
    --exclude='协作/调度台/runner.lock' \
    --exclude='协作/调度台/runner.status' \
    --exclude='协作/调度台/runner.history.jsonl' \
    --exclude='协作/调度台/rounds.tsv' \
    --exclude='协作/调度台/menubar.control-token' \
    --exclude='协作/调度台/*.status.json' \
    --exclude='协作/调度台/snapshots' \
    --exclude='协作/调度台/.locks' \
    . | (cd "$TMP" && tar -xf -)
)

for required in \
  "desktop/runner.sh" \
  "协作/调度台/watch.sh" \
  "协作/调度台/ack.sh"
do
  [ -f "$TMP/$required" ] || fail "全新克隆缺少 $required"
  [ -x "$TMP/$required" ] || fail "全新克隆中 $required 不可执行"
done
for runtime_state in \
  "协作/调度台/watch.state" \
  "协作/调度台/journal.log" \
  "协作/调度台/guard.status"
do
  [ ! -e "$TMP/$runtime_state" ] || fail "全新克隆不应包含运行态 $runtime_state"
done
echo "OK: 三个运行脚本存在且可执行；运行态未混入"

echo "[2/4] 构建菜单栏应用：bash desktop/build.sh"
(
  cd "$TMP"
  bash desktop/build.sh
) || fail "全新克隆构建失败"

APP="$TMP/desktop/build/CollabMenuBar.app"
BIN="$APP/Contents/MacOS/CollabMenuBar"
RUNNER_COPY="$APP/Contents/Resources/runner.sh"
[ -d "$APP" ] || fail "构建后没有生成 .app：$APP"
[ -x "$BIN" ] || fail ".app 主程序不可执行：$BIN"
[ -x "$RUNNER_COPY" ] || fail ".app 内 runner.sh 缺失或不可执行：$RUNNER_COPY"
echo "OK: .app 已生成且可执行（含 Resources/runner.sh）"

echo "[3/4] 校验 Python 控制台可导入"
(
  cd "$TMP"
  # console/server.py 以脚本运行时 console/ 在 sys.path；包导入时用同目录兜底。
  PYTHONPATH="$TMP/console${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -c "import console.server" \
    || fail "python3 -c 'import console.server' 失败"
)
echo "OK: import console.server"

echo "[4/4] 校验运行入口"
(
  cd "$TMP"
  python3 console/server.py --help >/dev/null
) || fail "console/server.py --help 失败"
echo "OK: console/server.py --help"

echo
echo "PASS: 全新克隆可构建、可导入、三个运行脚本齐全。"
echo "临时克隆：${TMP}（退出后清理）"
