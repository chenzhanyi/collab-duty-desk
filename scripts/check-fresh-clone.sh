#!/bin/bash
# 全新克隆自检：按源码白名单导出到临时目录 → 构建菜单栏应用 → 校验运行所需文件与导入。
#
# 用法：
#   bash scripts/check-fresh-clone.sh [仓库根目录]
#
# 导出规则（有意不使用“整棵工作树 tar”）：
#   - 仓库根有 .git 时，只导出 `git ls-files` 的已跟踪文件；
#   - 仓库根没有 .git（当前工作区就是这种情况）时，只导出
#     `scripts/release-paths.txt` 中逐行列出的源码白名单。
#   - 两种模式都会拒绝 项目/、归档/、协作/审计/、协作/roles/、协作/agents/、
#     调度台运行态、备份、日志、构建产物等禁入路径。
# 因此临时目录的输入边界就是白名单本身，不会把本机登记或运行数据带入验证树。
# 不修改源仓库；临时目录在退出时清理。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_REPO="$(cd "$HERE/.." && pwd)"
REPO="${1:-$DEFAULT_REPO}"
REPO="$(cd "$REPO" && pwd -P)"
MANIFEST="$REPO/scripts/release-paths.txt"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

[ -f "$REPO/desktop/build.sh" ] || fail "仓库缺少 desktop/build.sh：$REPO"
[ -f "$REPO/console/server.py" ] || fail "仓库缺少 console/server.py：$REPO"

LIST_MODE=""
if GIT_TOP="$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null)"; then
  GIT_TOP="$(cd "$GIT_TOP" && pwd -P)"
  [ "$GIT_TOP" = "$REPO" ] || fail "仓库根不是 Git 工作树根：${REPO}（实际 ${GIT_TOP}）"
  LIST_MODE="git"
else
  LIST_MODE="manifest"
  [ -f "$MANIFEST" ] || fail "源码树无 .git，且缺少源码白名单：$MANIFEST"
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/collab-fresh-clone.XXXXXX")"
cleanup() {
  rm -rf "$TMP"
}
trap cleanup EXIT

is_forbidden() {
  local rel="$1"
  case "$rel" in
    .git|.git/*|项目|项目/*|归档|归档/*) return 0 ;;
    协作/审计|协作/审计/*|协作/roles|协作/roles/*|协作/agents|协作/agents/*) return 0 ;;
    desktop/build|desktop/build/*|__pycache__|__pycache__/*|*/__pycache__|*/__pycache__/*) return 0 ;;
    *.pyc|*.bak|*.bak-*|*.log|.DS_Store|.env|.env.*) return 0 ;;
  esac
  case "$rel" in
    协作/调度台/*)
      case "${rel#协作/调度台/}" in
        watch.sh|ack.sh|since.sh|rounds.sh|state.py) return 1 ;;
        *) return 0 ;;
      esac
      ;;
  esac
  return 1
}

list_release_paths() {
  if [ "$LIST_MODE" = "git" ]; then
    git -C "$REPO" ls-files -z
  else
    # 白名单允许 # 注释与空行；路径本身不含换行。
    awk 'NF && $0 !~ /^[[:space:]]*#/' "$MANIFEST" | tr '\n' '\0'
  fi
}

echo "[1/5] 按源码白名单导出到临时目录：${TMP}（模式：${LIST_MODE}）"
count=0
while IFS= read -r -d '' rel; do
  rel="${rel%$'\r'}"
  [ -n "$rel" ] || continue
  case "$rel" in
    \#*) continue ;;
  esac
  case "/$rel/" in
    */../*|*/./*) fail "白名单包含非规范路径：$rel" ;;
  esac
  case "$rel" in
    /*) fail "白名单包含绝对路径：$rel" ;;
  esac
  is_forbidden "$rel" && fail "白名单包含禁入路径：$rel"
  src="$REPO/$rel"
  [ -e "$src" ] || fail "白名单文件不存在：$rel"
  [ -f "$src" ] || fail "白名单路径不是普通文件：$rel"
  mkdir -p "$TMP/$(dirname "$rel")"
  cp -p "$src" "$TMP/$rel"
  count=$((count + 1))
  echo "  - $rel"
done < <(list_release_paths)
[ "$count" -gt 0 ] || fail "源码白名单为空"
echo "OK: 已按白名单导出 $count 个源码文件，无整树复制"

for forbidden in ".git" "项目" "归档" "协作/审计" "协作/roles" "协作/agents" "desktop/build"; do
  [ ! -e "$TMP/$forbidden" ] || fail "临时树混入禁入路径：$forbidden"
done
[ -f "$TMP/scripts/release-paths.txt" ] || fail "临时树缺少源码白名单 scripts/release-paths.txt"

for required in \
  "desktop/runner.sh" \
  "协作/调度台/watch.sh" \
  "协作/调度台/ack.sh" \
  "协作/调度台/since.sh" \
  "协作/调度台/rounds.sh" \
  "协作/调度台/state.py"
do
  [ -f "$TMP/$required" ] || fail "全新克隆缺少 $required"
done
for required in "desktop/runner.sh" "协作/调度台/watch.sh" "协作/调度台/ack.sh"; do
  [ -x "$TMP/$required" ] || fail "全新克隆中 $required 不可执行"
done
while IFS= read -r -d '' state_file; do
  case "$(basename "$state_file")" in
    watch.sh|ack.sh|since.sh|rounds.sh|state.py) ;;
    *) fail "调度台混入非源码文件：${state_file#"$TMP/"}" ;;
  esac
done < <(find "$TMP/协作/调度台" -type f -print0)
for runtime_state in \
  "协作/调度台/watch.state" \
  "协作/调度台/journal.log" \
  "协作/调度台/guard.status"
do
  [ ! -e "$TMP/$runtime_state" ] || fail "全新克隆不应包含运行态 $runtime_state"
done
echo "OK: 三个运行脚本存在且可执行；调度台仅 5 个源码文件；运行态未混入"

echo "[2/5] 构建菜单栏应用：bash desktop/build.sh"
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

echo "[3/5] 校验 Python 控制台可导入"
(
  cd "$TMP"
  # console/server.py 以脚本运行时 console/ 在 sys.path；包导入时用同目录兜底。
  PYTHONPATH="$TMP/console${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -c "import console.server" \
    || fail "python3 -c 'import console.server' 失败"
)
echo "OK: import console.server"

echo "[4/5] 校验运行入口"
(
  cd "$TMP"
  python3 console/server.py --help >/dev/null
) || fail "console/server.py --help 失败"
echo "OK: console/server.py --help"

echo "[5/5] 校验共享扫描器与策略模块"
for required in "协作/调度台/state.py" "console/storage.py" "console/policy.py" "console/workspace.py" "console/task_queue.py" "console/static/queue.js"; do
  [ -f "$TMP/$required" ] || fail "缺少共享运行模块 $required"
done
COLLAB_ROOT="$TMP" python3 "$TMP/协作/调度台/state.py" watch 0.01 >/dev/null \
  || fail "临时克隆的守卫扫描器无法初始化"
echo "OK: 守卫共享模块与可迁移路径"

echo
echo "PASS: 全新克隆可构建、可导入、三个运行脚本齐全。"
echo "输入白名单：$count 个源码文件；临时克隆：${TMP}（退出后清理）"
