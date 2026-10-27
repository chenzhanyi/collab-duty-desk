# CollabMenuBar / 协作菜单栏

macOS 右上角的常驻菜单栏应用，用 Swift + AppKit 编写，**零第三方依赖**。
它负责两件事：

1. 启动 / 停止 / 重启本地控制台 `console/server.py`，并看护控制台崩溃后的退避重启；
2. 常驻运行 `协作/调度台/watch.sh 7200`，发现变化后写 journal、刷新基线并立刻重挂。

网页 `/dispatch` 不再管理守卫进程，只读取菜单栏应用写的 `协作/调度台/guard.status`。

## 构建与启动

在项目根目录：

```bash
bash desktop/build.sh
open "desktop/build/CollabMenuBar.app"
```

也可以在 Finder 中双击 `desktop/build/CollabMenuBar.app`。应用使用 `LSUIElement`，不会出现在 Dock。

- 构建只调用系统自带的 `swiftc`、`codesign`，不安装任何包。
- `.app` 必须留在项目内，或把 `COLLAB_ROOT` 环境变量指向项目根目录；应用会从自身位置向上寻找同时含 `console/server.py` 与 `协作/调度台/watch.sh` 的目录。

## 菜单

```text
控制台：● 运行中 · 已重启 N 次 / ○ 已停止 · 已重启 N 次 / ◌ 重启中…
启动控制台 · 停止控制台 · 重启控制台
打开网页（http://127.0.0.1:8787）
守卫：● 常驻中 / ○ 已停止
启动守卫 · 停止守卫
打开项目文件夹 · 打开日志
退出
```

- **启动控制台**：以 `/usr/bin/env python3 console/server.py` 起子进程，日志追加到 `协作/调度台/console.log`。
- **停止控制台**：先 SIGTERM，宽限后 SIGKILL；只停止自己启动的进程。若 8787 被外部启动的进程占用，菜单显示「运行中（外部）」，不会贸然终止。
- **控制台看护**：应用内 `DispatchSourceTimer` 每 **2 秒**做一次 TCP 健康检查（`127.0.0.1:8787`，测试可用 `COLLAB_CONSOLE_PORT` 指向假实例）。端口不可达时终止仍在运行但无响应的受控进程，按 **1 / 2 / 4 / 8 / 16 / 30 秒**退避重启 `python3 console/server.py --port <端口>`；每次自动重启都写 `console.log`（次数 + 时间），菜单显示「已重启 N 次」。连续崩溃不会空转。
- **手动停止优先**：点「停止控制台」后 `wantsToRun=false`，看护循环不再自动拉起；重新点「启动控制台」或「重启控制台」才恢复。8787 若由外部进程监听，应用不会终止它。
- **启动守卫**：应用启动时会自动开始常驻守卫；菜单可以停止 / 恢复。
- **守卫循环**：每轮运行 `bash 协作/调度台/watch.sh 7200`。
  - 有变化（`RESULT=CHANGE`）：追加时间 + watch 原样输出到 journal.log，自动运行 `ack.sh` 刷新 `watch.state` 基线（同时留下快照），然后**立即**重挂。
  - 超时或异常退出：追加日志，等 **3 秒**后重挂。
  - 启动时 `watch.state` 不存在时，watch.sh 会初始化它；下一轮继续常驻。
- **退出**：停止守卫与受控控制台；runner 负责让 bash / sleep / python 子进程一并退出，不留后台进程。没有 launchd，也不会开机自启。

## 状态与日志

| 路径 | 内容 |
| --- | --- |
| `协作/调度台/guard.status` | `state=running/stopped`、`updated`、`updated_epoch`、`app_pid`、`watch_pid`（当前守卫运行器 PID，进程组内含 watch.sh）、`note` |
| `协作/调度台/journal.log` | 守卫启动、变化事件（时间 + watch 输出）、基线刷新、停止；只追加 |
| `协作/调度台/console.log` | 控制台 stdout/stderr、崩溃与退避事件、菜单快照；只追加 |

应用只写 `协作/调度台/` 下的状态与日志，不写、不删 `项目/` 内任何文件；任务数据始终只由 `watch.sh` / `ack.sh` 读取（ack 仅复制快照）。

## 自动化控制接口（验收 / Shortcuts 用）

macOS 辅助功能（Accessibility）未授权时，无法用 System Events 点击菜单项。应用额外提供本地 URL scheme，命令直接调用与菜单项相同的 target/action 方法。**控制命令必须带上应用本次启动生成的令牌**：

```bash
TOKEN=$(cat 协作/调度台/menubar.control-token)
open "collab-menubar://start-console?token=$TOKEN"
open "collab-menubar://stop-console?token=$TOKEN"
open "collab-menubar://restart-console?token=$TOKEN"
open "collab-menubar://start-guard?token=$TOKEN"
open "collab-menubar://stop-guard?token=$TOKEN"
open "collab-menubar://open-web?token=$TOKEN"
open "collab-menubar://open-project?token=$TOKEN"
open "collab-menubar://open-log?token=$TOKEN"
open "collab-menubar://dump-menu?token=$TOKEN"   # 把实时菜单标题与启用状态写入 console.log
open "collab-menubar://quit?token=$TOKEN"
```

- 令牌文件权限为 `0600`，每次应用启动重新生成，位于 `协作/调度台/menubar.control-token`，不由网页服务提供。
- 未携带有效令牌的 URL 命令会被忽略并记入 `console.log`，网页无法借此启动/停止/退出应用。
- 这不是网络监听，也不启动开机自启；正常使用直接点菜单。

## 看护循环自检（8790 假实例）

`--watchdog-selftest` 使用同一个 `ConsoleController` 和看护循环，对 8790 端口上的
Python 假实例做两次 `kill -9`，验证 ≤10 秒恢复（TCP 可达且 `GET /` 返回 200）、
重启次数/时间落日志，再验证手动停止后不再自动拉起；不会触碰 8787：

```bash
bash desktop/build.sh
./desktop/build/CollabMenuBar.app/Contents/MacOS/CollabMenuBar --watchdog-selftest
# 看到 WATCHDOG-SELFTEST PASS 即通过
```

自检只写系统临时目录，退出时清理。可用 `COLLAB_SELFTEST_PORT` 换端口（默认 8790）。

## 源码

- `Sources/main.swift`：AppKit 状态项、菜单、控制台进程托管、守卫循环。
- `runner.sh`：让每条命令自成进程组，TERM 时整组收走。
- `build.sh`：编译并组装 `.app`。
