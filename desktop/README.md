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
打开任务看板                 ⌘O
发布新任务…                  ⌘N
查看运行记录                 ⌘R
──────────────────────────────
控制台：就绪 / 正在启动… / 正在停止… / 未启动
文件守卫：正在监测 / 正在记录变更 / 已暂停
──────────────────────────────
项目管理                    ▸ 角色配置
                             Agent 应用
                             归档库
                             ────────────
                             打开项目文件夹
运行与维护                  ▸ 查看调度台
                             ────────────
                             启动 / 停止 / 取消控制台启动
                             重启控制台
                             ────────────
                             暂停 / 恢复文件守卫
                             ────────────
                             打开控制台日志
                             打开文件守卫日志
                             打开日志文件夹
──────────────────────────────
退出协作菜单栏               ⌘Q
```

- **日常入口**：看板、新任务、运行记录直接打开对应页面。服务未启动时先自动启动，最多等待 30 秒就绪后再打开浏览器；连续选择只打开最后一个目标。停止或退出会取消等待；超时提供控制台日志入口。快捷键在菜单获得焦点时使用，不注册系统全局快捷键。
- **项目管理**：集中维护角色与 Agent，查看归档，或打开本地项目目录；仅进入页面，不会自动发布或执行任务。
- **按状态管理**：启停合并为一个条目，等待重启时可取消启动；启动中、重启中、停止清理中会显示对应状态。停止清理期间禁用网页入口；外部启动的服务可访问，但不能通过菜单停止或重启。
- **文件守卫**：暂停/恢复只影响文件变化记录，不影响任务执行。菜单区分正在监测、正在记录变更及等待重新监测；自动恢复次数显示在控制台状态中。
- **启动控制台**：以 `/usr/bin/env python3 console/server.py` 起子进程，日志追加到 `协作/调度台/console.log`。
- **停止控制台**：先 SIGTERM，最多等待 12 秒让执行器清理；必要时先清理锁中明确属于该控制台的 Agent 进程组，再 SIGKILL。只停止自己启动的进程。若 8787 被外部启动的进程占用，菜单显示「已连接外部服务」，不会贸然终止。
- **控制台看护**：应用内 `DispatchSourceTimer` 每 **2 秒**做一次 TCP 健康检查（`127.0.0.1:8787`，测试可用 `COLLAB_CONSOLE_PORT` 指向假实例）。端口不可达时终止仍在运行但无响应的受控进程，按 **1 / 2 / 4 / 8 / 16 / 30 秒**退避重启 `python3 console/server.py --port <端口>`；每次自动重启都写 `console.log`（次数 + 时间），菜单显示「已自动恢复 N 次」。连续崩溃不会空转。
- **手动停止优先**：点「停止控制台」后 `wantsToRun=false`，看护循环不再自动拉起；重新点「启动控制台」或「重启控制台」才恢复。8787 若由外部进程监听，应用不会终止它。
- **恢复文件守卫**：应用启动时会自动开始常驻守卫；菜单可以暂停 / 恢复。
- **守卫循环**：每轮运行 `bash 协作/调度台/watch.sh 7200`。
  - 有变化（`RESULT=CHANGE`）：追加时间 + watch 原样输出到 journal.log，自动运行 `ack.sh` 刷新 `watch.state` 基线（同时留下快照；未变文件在相邻快照间复用硬链接），然后**立即**重挂。
  - 超时或异常退出：追加日志，等 **3 秒**后重挂。
  - 启动时 `watch.state` 不存在时，watch.sh 会初始化它；下一轮继续常驻。
- **退出协作菜单栏**：停止守卫与受控控制台；runner 负责让 bash / sleep / python 子进程一并退出，不留后台进程。控制台清理宽限期统一为 12 秒；退出兜底会核对 runner.lock 的 owner_pid 后清理所属 Agent 进程组。没有 launchd，也不会开机自启。

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
open "collab-menubar://new-task?token=$TOKEN"
open "collab-menubar://open-runs?token=$TOKEN"
open "collab-menubar://open-roles?token=$TOKEN"
open "collab-menubar://open-agents?token=$TOKEN"
open "collab-menubar://open-archive?token=$TOKEN"
open "collab-menubar://open-dispatch?token=$TOKEN"
open "collab-menubar://open-project?token=$TOKEN"
open "collab-menubar://open-log?token=$TOKEN"          # 文件守卫日志（保留原命令）
open "collab-menubar://open-console-log?token=$TOKEN"
open "collab-menubar://open-log-folder?token=$TOKEN"
open "collab-menubar://dump-menu?token=$TOKEN"   # 递归记录主菜单与子菜单的标题和启用状态
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

## 菜单交互自检

```bash
./desktop/build/CollabMenuBar.app/Contents/MacOS/CollabMenuBar --menu-selftest
```

使用真实 AppKit 菜单验证入口顺序、分组、七个页面地址、快捷键、各服务状态下的启用规则，以及页面启动等待、连续选择、取消和超时。输出 JSON，`result=PASS` 表示通过。不启动服务或守卫，不读写项目运行状态，也不触碰 8787。

另有 `python3 scripts/check-menu-navigation.py`：从当前源码编译隔离的集成测试，启动延迟就绪的假控制台，验证重复点击不启动第二个进程、等待就绪后打开正确页面及停止取消等待。只使用临时目录和随机端口；通过注入记录器替代打开浏览器。

## 源码

- `Sources/main.swift`：AppKit 状态项、菜单、控制台进程托管、守卫循环。
- `runner.sh`：让每条命令自成进程组，TERM 时整组收走。
- `build.sh`：编译并组装 `.app`。

另有 `python3 scripts/check-agent-cleanup.py`：从当前源码编译隔离控制器，验证停止会清理忽略 SIGTERM 的假 Agent，不触碰 8787。
