#!/bin/bash
# 让子命令运行在自己的进程组里；收到 TERM 时连同孙进程一起收走。
# 这是菜单栏应用退出后不残留 watch.sh / sleep / python 子进程的关键。
set -u

child=0

forward_term() {
  if [ "$child" -gt 0 ]; then
    kill -TERM -- "-$child" 2>/dev/null || kill -TERM "$child" 2>/dev/null || true
    wait "$child" 2>/dev/null || true
  fi
  exit 143
}

trap forward_term TERM INT HUP

# 非交互 bash 里打开 job control 后，后台子命令会自成一个进程组。
set -m
"$@" &
child=$!
wait "$child"
status=$?

# 子命令主进程退出后，组内可能还有 sleep/find 等孙进程。
# 收掉它们：既避免孤儿，也让继承的输出管道及时关闭。
if [ "$child" -gt 0 ]; then
  kill -TERM -- "-$child" 2>/dev/null || true
fi
exit "$status"
