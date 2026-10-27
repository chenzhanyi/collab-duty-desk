#!/bin/bash
# 守卫：盯住每个项目的 对接/ 文件夹，一有新增/修改就立刻结束（结束 = 叫醒 agent）
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECTS="$ROOT/项目"
STATE="$ROOT/协作/调度台/watch.state"
MAX="${1:-7200}"; POLL=3
snapshot() {
  local d f
  [ -d "$PROJECTS" ] || return 0
  # 项目/*/对接/ 下的全部普通文件，包含 交付/r1、r2 等嵌套层级。
  for d in "$PROJECTS"/*/对接; do
    [ -d "$d" ] || continue
    find "$d" -type f 2>/dev/null
  done | LC_ALL=C sort | \
    while IFS= read -r f; do
      printf '%s\t%s\t%s\n' "$f" "$(stat -f %m "$f" 2>/dev/null)" "$(stat -f %z "$f" 2>/dev/null)"
    done
}
if [ ! -f "$STATE" ]; then snapshot > "$STATE"; echo "RESULT=INITIALIZED"; exit 0; fi
CUR="/tmp/dispatch_cur.$$"; trap 'rm -f "$CUR"' EXIT
elapsed=0
while [ "$elapsed" -lt "$MAX" ]; do
  snapshot > "$CUR"
  if ! cmp -s "$STATE" "$CUR"; then
    echo "RESULT=CHANGE"
    awk -F'\t' 'FNR==NR{old[$1]=$2"\t"$3;next}{new[$1]=$2"\t"$3}
      END{for(p in new){if(!(p in old))print "新增\t"p;else if(old[p]!=new[p])print "修改\t"p}
      for(p in old)if(!(p in new))print "消失\t"p}' "$STATE" "$CUR" | LC_ALL=C sort -k1,1 -k2,2
    exit 0
  fi
  sleep "$POLL"; elapsed=$((elapsed+POLL))
done
echo "RESULT=TIMEOUT_${MAX}s"
