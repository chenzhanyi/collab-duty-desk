#!/bin/bash
# 被叫醒后【立刻】跑这个（先记录、再干活）
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECTS="$ROOT/项目"
STATE="$ROOT/协作/调度台/watch.state"
{
  if [ -d "$PROJECTS" ]; then
    for d in "$PROJECTS"/*/对接; do
      [ -d "$d" ] || continue
      find "$d" -type f 2>/dev/null
    done | LC_ALL=C sort | \
      while IFS= read -r f; do
        printf '%s\t%s\t%s\n' "$f" "$(stat -f %m "$f" 2>/dev/null)" "$(stat -f %z "$f" 2>/dev/null)"
      done
  fi
} > "$STATE"
SNAP="$ROOT/协作/调度台/snapshots/$(date '+%Y%m%d-%H%M%S')"; mkdir -p "$SNAP"
for d in "$PROJECTS"/*/对接; do
  [ -d "$d" ] || continue
  project="$(basename "$(dirname "$d")")"
  mkdir -p "$SNAP/项目/$project"
  cp -pR "$d" "$SNAP/项目/$project/对接"
done
echo "ACKED $(date '+%F %T') 共 $(wc -l < "$STATE" | tr -d ' ') 个文件"
echo "SNAPSHOT $SNAP"
