# 协作值班台（本地网页控制台）

管理并显示 `项目/` 下独立任务项目的多智能体协作状态，提供「选文件夹 → 写想法 → 发布 → 总控登记/拆子任务」的新建流程，提供角色与 Agent 应用的两层配置，并可从任务详情页继续分发、审核或全自动推进到终态；审核结论为「须修正」时可继续修正（只重做问题清单涉及的子任务，最多 3 轮）。值班台不是 Agent 的工作区；Agent 的实际 cwd 是每个任务的 `workdir`（默认 `工作区/`，可在系统外）。只用 Python 3 标准库，不安装任何依赖。

## 启动

推荐由菜单栏应用托管（它会启动 / 停止控制台并常驻守卫）：

```bash
bash desktop/build.sh
open desktop/build/CollabMenuBar.app
```

也可以用菜单栏应用启动控制台后打开 <http://127.0.0.1:8787>，或手动在项目根目录执行：

```bash
python3 console/server.py
```

按 `Ctrl+C` 停止手动进程。手动启动时没有菜单栏退出清理，请注意自行停止。

验收时可以起一个不碰 8787 的临时实例：

```bash
python3 console/server.py --port 8789 --run-timeout 3600 --register-timeout 900
```

- 默认监听 `127.0.0.1:8787`；`--port` 只改变端口，仍只绑定 `127.0.0.1`，不接受局域网或公网访问。
- `--run-timeout` 是执行器单次运行的超时秒数，默认 `3600`，范围 1–604800。
- `--register-timeout` 是发布后总控登记调用的超时秒数，默认 `900`，范围 1–86400。
- `--advance-max-runtime` / `--advance-max-subtasks` / `--advance-max-revise-rounds`
  是全自动推进预算，默认 `7200` 秒（2 小时）/ `12` 个子任务 / `3` 轮修订；也可用
  `COLLAB_ADVANCE_MAX_RUNTIME_SECONDS` 等环境变量配置。超限停止为
  `stopped_with_budget` 并释放 `runner.lock`。
- 页面每次刷新都直接读取磁盘，不使用缓存。
- 如目标端口已被占用，服务会直接报错退出。
- 执行器收到 SIGTERM/`Ctrl+C` 时先终止当前 Agent 进程组并尽量释放锁；不触碰并非本实例启动的进程。总控登记进程同样会被收尾。

## 任务目录

控制台只认这个结构：

```text
项目/<编号>_<短名>/
├── 对接/
│   ├── idea.md                 # 发布时的人类原文（不改写）
│   ├── task.yaml               # id/title/workdir/created_at + status/controller/revise_round 等；未知扩展字段原样保留
│   ├── 任务单.md               # 总控登记时写入；看板只认它
│   ├── subtasks.yaml           # 总控拆出的 2–6 个子任务
│   ├── 登记完成.md             # 总控最后写下的完成信号
│   ├── 交付/
│   │   └── r1/DONE.md
│   ├── 审查.md                  # 审核角色：结论（通过 / 须修正）+ 证据 + 问题清单
│   ├── 审核完成.md              # 审核完成信号
│   └── 审查历史/
│       ├── round-1.md           # 继续修正时复制的上一轮审查快照（原件不删除）
│       ├── invalid-<时间>.md    # 空/半份/不可识别审查在重试前移入这里（不删除）
│       └── invalid-attribution-<时间>.md # 冒名/AI-A/缺署名的审查隔离（不删除）
└── 工作区/                     # 默认 workdir；外部 workdir 时只保留日志
    └── 日志/
        ├── dispatch-<子任务>-<时间戳>.log # 每个子任务的 Agent stdout+stderr
        ├── review-<时间戳>.log # 审核 Agent 的 stdout+stderr
        ├── revise-<时间戳>.log # 继续修正的策略、归档、重置与推进记录
        ├── handoff-<时间戳>.log # 交接闸门检查项/结论/补交次数/Agent
        ├── run-<时间戳>.log     # 通用执行器运行日志格式
        └── register-<时间戳>.log # 总控登记调用的输出与固定提示词
```

`对接/task.yaml` 缺失时自动生成，默认 `workdir = <项目目录>/工作区`。`workdir` 可以是系统外的绝对路径；执行器以它为 cwd 启动 Agent，但日志仍写值班台侧的 `工作区/日志/`。归档/恢复会整体移动项目目录，**只**把指向旧 `工作区/` 的默认 workdir 跟着更新；status/controller/revise_round/auto_advance/idea_sha256/register_log 及未知扩展字段逐字段保留，指向系统外的 workdir 保持不变。

T08 发布的任务在总控写出 `任务单.md` 前不会进入看板；期间可在 `/registering/<编号>` 查看登记进度。登记成功后，详情页与看板显示 `subtasks.yaml` 的子任务清单和「子任务 x/y」；子任务按角色串行分发，全部交付后可进入审核。中断后的再次分发只处理 pending/failed；完全没有子任务、但有任务级 `DONE.md` 或工作区产物的任务也能审核。审核为「须修正」的任务可在详情页继续修正，轮次记录在 `对接/task.yaml` 的 `revise_round`。

归档时，整个 `项目/<编号>_<短名>/` 目录会原样移动到 `归档/<编号>_<短名>/`；恢复时再整体移回。项目编号取目录名开头的 `T<数字>`；缺少 `对接/任务单.md` 的项目不会进入看板。若有执行器正在运行该项目，归档会返回 409，项目不会移动。

## 归档与恢复

- 看板卡片和任务详情页都有「归档」按钮。按钮是 POST 表单，提交前由同源静态脚本弹出二次确认。
- `POST /archive/<编号>` 只把对应的项目目录整体移动到 `归档/`，成功后 `303` 回看板。若 `归档/` 已有同名目录，新目录名追加时间戳后缀，不覆盖旧档案。
- 归档后的项目不在看板出现，但 `GET /task/<编号>` 仍会回落到 `归档/` 读取，详情页保持只读。
- `GET /archive` 列出全部归档项目，并提供「恢复」按钮；`POST /restore/<编号>` 只把目录整体移回 `项目/`，遇到同名目录同样加时间戳后缀。
- 控制台不删除文件，也不逐个改写归档项目中的文件。

## 页面与接口

| 地址 | 用途 |
| --- | --- |
| `GET /` | 任务看板：编号、标题、mtime、状态、轮数、子任务进度 |
| `GET /task/<编号>` | 任务单、我的想法、子任务清单、最新 DONE.md、审查意见、**值班台/工作目录**、**全部对接文件**、**工作区文件列表** |
| `GET /new` | 发布任务页：原生文件夹选择器 + 手动绝对路径 + 大想法输入框 + 发布按钮 |
| `POST /publish` | 创建项目、原样保存 `idea.md`、写 `task.yaml(status: registering)`，并自动调用总控登记。表单字段含默认勾选的 `auto_advance`；登记成功后自动启动全自动推进。HTML `303` 到 `/registering/<编号>`；`Accept: application/json` 返回 `202` |
| `GET /registering/<编号>` | 登记等待页；登记完成时 `303` 到任务详情，失败时显示日志路径和重试按钮 |
| `GET /api/task/<编号>/register-status` | 登记状态 JSON：`registering` / `registered` / `register_failed`、标题、总控、错误、日志、子任务数 |
| `POST /register/<编号>/retry` | 对 `register_failed` 的任务重试登记；不会改写 `idea.md` |
| `POST /api/pick-folder` | 调用本机 `osascript` 的 `choose folder`，返回 `{path}`；取消/无 GUI 时返回 JSON 错误，可改用手动路径 |
| `POST /api/validate-folder` | 校验项目文件夹（绝对、已存在、非系统根、非 `协作/`、复用 T07 规则），发布页的实时路径状态用 |
| `POST /new` | 兼容旧版任务单表单；新页面不再使用。新发布请用 `POST /publish` |
| `POST /task/<编号>/workdir` | 修改该任务的 workdir 并写回 `对接/task.yaml`；非法路径返回 `400` 与原因；归档任务返回 `409` |
| `GET /archive` | 归档库：全部 `归档/` 项目的只读列表与恢复按钮 |
| `POST /archive/<编号>` | 把该项目目录整体移动到 `归档/`，`303` 回看板 |
| `POST /restore/<编号>` | 把该归档项目目录整体移回 `项目/`，`303` 回归档库 |
| `POST /dispatch/<编号>` | 按 `对接/subtasks.yaml` 串行分发 pending/failed 子任务，failed 先重置为 pending，delivered/passed 不重跑；无表单字段。HTML 成功 `303` 回任务详情；带 `Accept: application/json` 时返回 `202` JSON。预检失败返回 `409` + `violations`；缺清单/空清单/无 pending 或 failed 返回 `400` |
| `GET /api/task/<编号>/dispatch-status` | 分发进度 JSON：`state` / `total` / `done` / `success` / `failed` / `current_subtask` / `results` / `subtasks` |
| `POST /review/<编号>` | 已交付后启动审核：全部子任务 delivered/passed，或任务级 `DONE.md` / 无子任务时的工作区产物；不要求有子任务。`reviewer` 角色必须绑定可执行 Agent 且无活锁；HTML 成功 `303` 回详情，JSON 返回 `202`；预检失败 `409` + 原因，不启动任何进程 |
| `GET /api/task/<编号>/review-status` | 审核状态 JSON：`phase` / `state` / `running` / `conclusion` / `issues` / `log_url` / `can_start` / `disabled_reason` / `delivery_evidence` 等 |
| `POST /advance/<编号>` | 全自动推进：循环分发 pending/failed，全部交付后自动审核，到通过 / 须修正 / 失败 / 交接阻塞 / 预算用尽即停。每一步前执行交接闸门。预算默认 2 小时 / 12 个子任务 / 3 轮修订，超限写 `stopped_with_budget` 并释放锁。HTML 成功 `303` 到详情，JSON 返回 `202`；已有推进、分发、审核或登记占用时返回 `409` |
| `POST /advance/<编号>/stop` | 用户显式停止推进：终止当前 Agent 进程组、等待监视线程收尾并释放 `runner.lock`；状态写 `stopped`；手动停止后不自动拉起。JSON 返回 `200` + `advance`（含 `lock_released`），无推进时 `409` |
| `GET /api/task/<编号>/advance-status` | 全自动推进状态 JSON：`state` / `running` / `terminal` / `can_stop` / `stop_url` / `stopped_with_budget` / `budget`（limits/used/exceeded/reason）/ `auto` / `revise` / `step` / `current_subtask` / `phase` / `handoff_*` / `message` / `error` / `events` / `can_start` 等 |
| `GET /api/task/<编号>/handoff-status` | 最近一次交接体检 JSON：`state`（`checking` / `supplementing` / `passed` / `blocked`）/ `checks` / `missing` / `attempts` / `agent_runs` / `log_url` / `can_retry` |
| `POST /handoff/retry/<编号>` | 对 `handoff_blocked` 的任务重跑最近阶段的 G1–G4 体检；仍缺件返回 `409` 和最新缺件清单，通过则解除阻塞（暂停中的推进不会自动续跑，需重新点推进） |
| `POST /revise/<编号>` | 「须修正」任务的继续修正：仅当 `phase=revise` 且 G3 通过时可用；`revise_round` 达到 3 轮返回 `409` + 请人工介入。解析 `对接/审查.md` 问题清单中的 `Txx-n`，只重置命中子任务；未命中且未指定 `mode` 时返回 `409` + `needs_choice`。表单 `mode` 可选 `affected`（默认）/ `all` / `review`。旧审查复制到 `对接/审查历史/round-<n>.md`，随后启动既有 advance 流程（新一轮审核后检查 G4）。HTML 成功 `303` 到详情，JSON 返回 `202` |
| `GET /api/task/<编号>/revise-status` | 继续修正状态 JSON：`revise_round` / `next_round` / `max_rounds` / `at_limit` / `can_start` / `running` / `needs_choice` / `target_ids` / `all_ids` / `issues` / `current_subtask` / `log_url` 等 |
| `GET /log?path=<相对路径>` | 只读查看 `工作区/日志/` 下的子任务日志，最多 512 KB |
| `GET /runs` | 当前运行 + 最近 10 次运行；含状态、耗时、退出码、日志尾部 30 行 |
| `GET /dispatch` | 守卫状态（`guard.status`、watch.state 行数、journal.log 尾部）与执行器 ● 空闲/运行中 |
| `GET /api/runner` | 执行器状态、锁、当前运行和最近 10 次运行 JSON |
| `GET /api/tasks` | 任务快照 JSON（含 `desk_root`、`workdir`、`workdir_is_custom`、`meta_path`、`task_path`、`project_path`、`docking_path`、`workspace_path`、文件数、`subtask_total` / `subtask_done`（兼容旧名 `subtasks_total` / `subtasks_done`）、`status`（由 `phase` 派生）/ `phase` / `phase_label`、`has_delivery`（统一用 `_delivery_evidence()`）/ `delivery_evidence`、`handoff_blocked` / `handoff`、`has_review_done`、`review_verdict`、`revise_round` / `revise_can_start` / `revise_targets` / `revise_needs_choice` 与登记字段；只含活动项目） |
| `GET /roles` | 角色列表；看板顶部与侧栏的「角色」入口指向这里 |
| `GET /roles/new` | 新增角色表单 |
| `POST /roles/new` | 独占创建 `协作/roles/<id>.yaml`，已存在时拒绝覆盖 |
| `GET /roles/<id>` | 编辑角色：名称、说明、绑定 Agent、写入范围、启用状态、备注 |
| `POST /roles/<id>` | 更新现有角色 YAML；ID 与文件名不能改 |
| `POST /roles/<id>/delete` | 从角色列表移出：只移动到 `协作/roles/.archive/`，不删除文件 |
| `GET /agents` | 已登记 Agent + 本机检测结果 + 手动登记表单 |
| `POST /agents/new` | 独占创建 `协作/agents/<id>.yaml`；只探测一次 `--version`（3 秒超时） |
| `GET /api/roles` | 角色配置 JSON（含文件路径、绑定关系和解析错误） |
| `GET /api/agents` | 已登记 Agent 与本机检测 JSON（路径、可执行、版本前 80 字符） |

`/dispatch` 没有启动 / 停止守卫的按钮，也没有对应后端。守卫进程只由 macOS 菜单栏应用管理；网页不调用 `watch.sh`，不写 `watch.state`，不追加 journal。执行器只从 `/task/<编号>` 的「开始/继续分发」「开始审核」「全自动推进」按钮启动（或发布后的自动推进），不接受在本页直接选择 Agent。

### 执行器接口示例

```bash
# 分发 pending/failed 子任务（failed 会重置为 pending；角色绑定在 协作/roles/*.yaml 中维护）
curl -i -X POST -H 'Accept: application/json' \
  http://127.0.0.1:8789/dispatch/T99

# 轮询进度
curl -s -H 'Accept: application/json' \
  http://127.0.0.1:8789/api/task/T99/dispatch-status
```

任一 pending/failed 子任务的角色未绑定、Agent 未登记、`installed=false` 或命令不可执行时，预检返回 `409` 和 `{"ok": false, "violations": [...]}`，**一个子任务都不会启动**。已有活锁/已有分发或正在全自动推进时返回 `409`。成功后用 `GET /api/task/<编号>/dispatch-status`、`GET /api/runner` 或 `/runs` 查看状态、进度、日志与历史。

## 发布与总控登记（T08）

1. `/new` 提交到 `POST /publish`。服务端先校验项目文件夹与控制器，再在 `TASK_WRITE_LOCK` 下分配编号、建 `项目/T{NN}_{slug}/对接/`。
2. 人类原文按提交内容原样写入 `对接/idea.md`（二进制写入，不做 trim/改写），同时写 `task.yaml`（`status: registering`、`controller`、`idea_sha256`）。
3. 控制器解析顺序：`协作/roles/controller.yaml` 的 `agent_id` → `openclaw-main` → `deepseek-harness`。角色绑定不可用会回退；发布页会显示最终使用的总控与来源。
4. 总控调用由 `Runner.launch_registration` 启动：**复用同一把 `runner.lock`**，`stdin=/dev/null`，有超时，cwd 固定为任务项目目录，日志写 `工作区/日志/register-<时间戳>.log`。它不写 `runner.status` / `runner.history`，避免与普通执行混淆；登记运行期间 `POST /dispatch` 会因锁占用返回 409。
5. 固定提示词要求总控：读 `idea.md` → 起标题 → 写 `对接/任务单.md`、`对接/subtasks.yaml`（2–6 个）→ 更新 `task.yaml` 的 `status: registered` → 最后写 `对接/登记完成.md`。禁止改写 idea、写审查、改其它项目或执行子任务。
6. 调用结束后，控制台校验：进程退出成功、`任务单.md`/`subtasks.yaml`/`登记完成.md` 在本次登记时间内写入、`subtasks.yaml` 可解析且数量 2–6、`idea.md` 哈希未变。全部通过才收口为 `registered`；否则写 `register_failed` 并保留错误和日志路径。
7. `/registering/<编号>` 每 2 秒轮询 `GET /api/task/<编号>/register-status`；`registered` 自动跳转详情，`register_failed` 显示错误/日志并提供 `POST /register/<编号>/retry`。重试会沿用同一个项目与 `idea.md`，并要求本次重写的登记文件满足时间校验。
8. 服务启动时若发现残留的 `registering` 且执行器锁不活跃，会把它们标为 `register_failed`，避免永久卡在登记中。

`对接/subtasks.yaml` 展示结构：

```yaml
subtasks:
  - id: T09-1
    title: 子任务标题
    goal: 要达成什么
    acceptance: 可检查的验收标准
    status: pending          # pending|running|delivered|passed|failed
    agent_role: designer     # 可选
```

任务详情页展示所有子任务与状态徽标；看板显示「子任务 x/y」（`delivered` / `passed` 计入完成）与当前阶段。详情页按阶段切换下一步：开始/继续分发 → 审核 → 已通过/须修正；所有阶段还提供「全自动推进」。

发布成功时可拿到 JSON：

```json
{
  "ok": true,
  "id": "T09",
  "status": "registering",
  "controller": "openclaw-main",
  "status_url": "/api/task/T09/register-status",
  "register_url": "/registering/T09",
  "task_url": "/task/T09"
}
```

## 角色与 Agent 配置

两层配置都是人类可读、可手改的 YAML：

```text
协作/roles/<role-id>.yaml     # id · name · description · agent_id · write_scope[] · enabled · notes
协作/agents/<agent-id>.yaml   # id · name · command · args_template · cwd · installed · version · notes
```

- 首次运行且 `协作/roles/` 尚不存在时，自动生成 `controller`（总控）、`designer`（设计师）、`reviewer`（审核）三个默认角色；已存在的目录与文件不会被覆盖，也不会复活已移出的角色。
- 角色可新增、编辑、启用/停用，并绑定一个已登记的 Agent。删除动作只把 YAML 移到 `协作/roles/.archive/`，保留原件。
- Agent 检测扫描 `~/claude-model/bin/*`，以及 `command -v claude claude-ds claude-doubao codex openclaw`。每个可执行候选只运行 `<command> --version`，超时 3 秒；版本截取前 80 字符。检测结果不会被自动写入登记。
- 手动登记要求 CLI 绝对路径；参数模板必须包含且只包含一个 `{prompt}` 占位符。执行器按 shell 词法拆分后只替换这个占位符，不经过 shell 执行，也不会二次拆词。
- 角色/Agent 的登记与编辑动作本身不调用 Agent。人工分发在任务详情页，自动登记在发布/重试后调用 `controller`；绑定和 `installed=true`（且命令当前可执行）是分发预检或解析总控的前置条件。

控制台支持的小型 YAML 子集包括平面 `key: value`、布尔、单/双引号字符串、块列表/行内列表与 `|` 块文本；遇到无法解析的文件会跳过并在页面/JSON 中提示，不自动改写。

详情页的「对接文件」逐个列出 `对接/` 下的全部文件（含历史轮次），并可展开文本文件原文；「工作区文件」列出 `工作区/` 内的全部产物文件。

看板 `status` 由工作流 `phase` 派生：`passed` → 已通过/结案，`revise` → 须修正，`review_ready` / `reviewing` → 已交付，`handoff_blocked` → 需处理，其余 → 待处理。因此「5/5 子任务已交付但没有任务级 DONE.md」也会显示已交付/待审核，不再误标待处理。

工作流阶段判定（详情页顶部、看板卡片与 `/api/tasks` 的 `phase`）：

1. 最近交接体检为 `blocked`，或全自动推进停在 `handoff_blocked` → `handoff_blocked`（交接阻塞）
2. 继续修正推进中 → 按当前步骤显示 `dispatching`（分发中）/ `reviewing`（审核中）；结束后回到审查结论
3. **运行中判定优先**：审核进程在跑 → `reviewing`；分发进程在跑 → `dispatching`，即使磁盘上还留着上一轮终态文件
4. 审核终态：`对接/审查.md` 有明确结论（通过 / 须修正）**且** `对接/审核完成.md` 存在 → `passed` / `revise`；空、半份（缺完成信号）或结论不可识别一律不进终态
5. 已有交付（`对接/交付/**/DONE.md`、全部子任务 delivered/passed，或无子任务时 workdir 产物）→ `review_ready`（待审核），并保留重新审核入口
6. 有 pending / failed 子任务或其它情况 → `undispatched`（未分发）

## 交接闸门（T18）

每次阶段推进前先检查必要文件，全部存在才放行：

| 闸门 | 检查内容 | 责任角色 |
| --- | --- | --- |
| G1 登记完成 | `对接/task.yaml`（`status=registered`）、`任务单.md`、`subtasks.yaml`（可解析且 ≥1 个子任务）、`登记完成.md` | `controller` |
| G2 子任务交付 | 每个 delivered/passed 子任务的 `<workdir>/子任务/<id>/DONE.md`（缺省 workdir 即 `工作区/`；含「做了什么 / 怎么验 / 结果」）＋该目录至少 1 个非 DONE 产物 | 该子任务的 `agent_role` |
| G3 审核完成 | `对接/审查.md` 有可识别结论（通过 / 须修正）＋ `对接/审核完成.md` | `reviewer` |
| G4 修正完成 | `对接/审查历史/round-<n>.md` ＋ 新一轮 `审查.md` 有可识别结论 | `reviewer` |

执行点：自动推进在分发前跑 G1/G2、审核前跑 G2、审核完成后跑 G3（修正轮次再加 G4）；手动分发同样先跑 G1/G2，手动审核先跑 G2，继续修正前跑 G3。缺件时按责任角色回叫补交，提示词固定为「交接检查未通过：缺少 …。只补这些文件，不要重做其它工作；补完即退出。不得写 `对接/审查.md`（除非你是 reviewer）。」；补完复检，每道闸门最多 2 次补交。G3 还要求 `审查.md` / `审核完成.md` 本轮确实被重写，避免把上一轮结论误当本轮。仍缺则写 `handoff-*.log`、状态 `handoff_blocked`、缺件清单和 `agent_runs`，停止推进；详情页显示「需要人工处理」与「重试体检」，`POST /handoff/retry/<编号>` 会重新检查并按需补交。补交运行 `kind=handoff`，与分发/审核复用 `runner.lock`，绝不并发写。

阶段决定详情页的主按钮：需要分发时显示「开始分发实现」；已有部分交付时显示「继续分发（余 n）」；存在失败项时显示「继续分发（重试 m）」或同时标出余量；全部交付后切换为「开始审核」；没有子任务但有任务级交付时也直接显示审核面板；审核中显示进度；通过后显示「已通过」与问题清单摘要；须修正时显示「继续修正（第 n 轮）」与问题清单、本轮将重做的子任务，未解析到子任务编号时提供「全部重做 / 只重新审核」二选一，达到 3 轮后禁用并提示人工介入。有可推进步骤时还可以点「全自动推进」。

轮数读取 `协作/调度台/rounds.tsv`。文件缺失或没有该任务记录时显示 `0 轮`。

## 执行器（`console/runner.py`）

人工分发的唯一入口是任务详情页的「开始分发实现 / 继续分发」；发布后的总控登记是第二个受控入口，它只能由 `POST /publish` / 重试触发，不能从 UI 传入任意提示词。执行器自己做四件事：拼固定子任务提示词、抢全局锁、启动子进程、记录日志与状态。

1. **角色解析与预检**：`POST /dispatch/<编号>` 先读取 `对接/subtasks.yaml` 的全部 pending/failed 子任务，逐个检查 `agent_role`：角色必须存在、已启用、`agent_id` 非空，绑定的 Agent 必须已登记、`installed=true` 且命令当前可执行。任一不满足就返回 `409` 和完整 `violations` 清单，**不启动任何进程**。任务详情页不再提供 Agent 下拉直选。
2. **固定提示词**：每个子任务使用按角色与子任务内容生成的固定模板：「你是 <角色名>。执行子任务 <id>：<title> … 工作目录（cwd）：<workdir> … 输出目录（绝对路径）：`<workdir>/子任务/<id>` … 完成后写 DONE.md（绝对路径）… ★ 不得写 `对接/审查.md`；不得改其它子任务；不删文件」。提示词、G2、审核清单与执行器日志共用同一个绝对 output_dir；默认与外部 workdir 规则一致，不再要求多套一层 `工作区/`。UI 不传任意提示词。
3. **串行执行与命令**：按 subtasks.yaml 顺序逐个启动，前一个收尾后才启动下一个。`agent.command` + `args_template` 经 `shlex` 拆分，唯一的 `{prompt}` token 替换为提示词；不经过 shell。普通运行与分发的 cwd 固定为该任务的 **workdir**（来自 `对接/task.yaml`，不是 `Agent.cwd`），`stdin=DEVNULL`，stdout/stderr 合并进日志。workdir 必须绝对、非系统根、非 `协作/`；系统根内只允许 `项目/<编号>_<短名>/工作区/`，系统外目录必须已存在或可创建。任务目录必须是编号匹配的 `项目/` 直接子目录，符号链接会被拒绝。审核运行的 cwd 是本任务的项目目录，且执行器只接受这一处 cwd 覆盖。
4. **单实例锁**：`协作/调度台/runner.lock` 使用原子独占创建，写入执行器 PID、Agent PID、任务、Agent、开始时间和 token。已有活锁时 `POST /dispatch` 返回 409，绝不启动第二个写手。正常退出/失败/超时均释放；硬崩溃留下的失效锁在下次抢锁时按 PID 判死并清理。服务启动时的 `reconcile` 也先抢锁再判定遗留状态，不会改写活锁对应的运行。
5. **原子状态回写与崩溃恢复**：启动子任务前把该条 `status` 改为 `running`；拉起执行器后再落本轮 `log` 与 `run_id`。结束后改为 `delivered`（退出码 0）或 `failed`（非零/超时/启动失败），并写入 `log`、`started_at`、`finished_at`、`error`。每次只改原文中的目标字段，保留其它字段、注释与块文本，用同目录临时文件 `os.replace` 原子替换。失败默认停止后续子任务。继续分发时 failed 条目先原子重置为 pending（清空本轮时间/错误/日志引用，不删日志文件），delivered/passed 不重跑。控制台启动时额外执行子任务 reconcile：无活锁且内存中无对应分发/审核/推进线程的 `running` 子任务改为 `failed`（`error: interrupted`），保留原 `log`/`run_id`；有活锁的任务不动。恢复后「继续分发」会选中这些 failed 项。
6. **超时**：默认 3600 秒，`--run-timeout` 可配。超时会向子进程组发 SIGTERM，宽限后 SIGKILL，状态记为 `timeout` 并回写为子任务 `failed`。
7. **收尾**：即使主 CLI 正常退出，也会先排空其会话进程组，杀掉遗留的后台子进程，再写结束状态、释放锁。避免“日志结束但后台仍在写”的第二个写手。
8. **输出**：每个子任务新建值班台侧 `项目/<编号>_<短名>/工作区/日志/dispatch-<子任务>-<时间戳>.log`，不覆盖既有日志；日志包含 kind=dispatch、subtask、workdir、提示词、Agent 输出和收尾信息。日志不会写进外部 workdir。详情页子任务表格的「日志」列链接到 `/log?path=...`。
9. **状态**：`协作/调度台/runner.status` 是平面 YAML，含 `state` / `task` / `agent` / `kind` / `subtask` / `pid` / `started` / `log` / `exit`（另附 owner_pid、run_id、round、finished、duration、timeout 等）。分发运行写 `kind: dispatch`。
10. **历史**：`协作/调度台/runner.history.jsonl` 只追加每次收尾记录；`/runs` 取最近 10 条，归档项目的日志会自动从 `归档/` 回读尾部。
11. **结束状态**：`success`（退出码 0）、`failed`（非零退出、崩溃或启动失败）、`timeout`。锁释放后才可开始下一次运行。控制台正常退出会终止它自己启动的 Agent 进程组并尽量完成收尾。
12. **分发进度**：分发状态以 JSON 持久化在 `协作/调度台/dispatch-<编号>.status.json`；页面轮询 `GET /api/task/<编号>/dispatch-status`。同一时刻只允许一个分发；分发期间归档该项目或修改其 workdir 会返回 409。状态另含 `failed_subtasks` / `delivered` / `retried` / `action_label` 供继续分发按钮与页面使用。
13. **总控登记复用**：`launch_registration` 使用同一个 `runner.lock`、空 stdin、超时和进程组收尾，但用单独提示词与 `register-*.log`；不写 `runner.status` / `runner.history`。登记监控结束前会先写回 `task.yaml` 终态，再释放锁，避免登记结果与其他写手交错。
14. **审核**：`POST /review/<编号>` 先预检所有已有子任务均为 `delivered`/`passed`，并且存在任务级交付（`对接/交付/**/DONE.md`，无子任务时也接受工作区产物）；没有 `subtasks.yaml` 也可审核。再解析 `reviewer` 角色绑定的 Agent；任一不满足返回 409，不启动进程。启动时 `kind=review`；因为审核要同时读 `对接/` 与 `工作区/`，进程 cwd 是本任务的项目目录（`runner.launch(cwd=项目目录)`，仍是该任务自己的目录，不是值班台根），日志为 `工作区/日志/review-<时间戳>.log`，`runner.status` 记 `kind: review`。审核状态持久化在 `协作/调度台/review-<编号>.status.json`，页面轮询 `GET /api/task/<编号>/review-status`。**署名治理**：新审核的提示词强制审查.md 头部含 `- 审核角色：<role.name>（<agent.id>）`、`- 审查时间：…` 与结论行；终态校验要求角色名与本次绑定 Agent 一致。出现 `AI-A` 字样视为冒名，连同 `审核完成.md` 移入 `对接/审查历史/invalid-attribution-<时间>.md`，状态 `review_failed`。新规则作用于 T24 起（2026-10-27 02:51）之后写入的审查，以及本次 review 状态带 `attribution_required` 的审查；早于该时刻的历史文件不改，继续按旧规则判定。审核线程以 **`对接/审查.md` + `对接/审核完成.md` 落盘且结论可识别** 作为完成判据；失败写 `review_failed`、错误与日志路径，可重试。若本轮产出空/半份/结论不可识别，该组文件会移入 `对接/审查历史/invalid-<时间>.md`（不删除）；手工重试一个已有无效审查的任务时也先隔离再启动，避免旧文件挡路。审核与分发共用单实例锁，互斥运行。

15. **全自动推进**：`POST /advance/<编号>` 启动一个推进线程，按「继续分发（pending/failed）→ 自动审核」循环，直到审核通过、须修正或任一步失败。推进状态写 `协作/调度台/advance-<编号>.status.json`，可由 `GET /api/task/<编号>/advance-status` 轮询；分发/审核本身仍分别复用 `runner.lock`，不额外启动进程。**停止与预算**：`POST /advance/<编号>/stop` 由用户显式触发，写 `stop_requested`，终止当前 Agent 进程组并等待锁释放；停止后不会自动重新拉起。推进预算默认最长 2 小时、最多 12 个子任务、最多 3 轮修订（可用命令行/环境变量调整），每次启动子任务前检查；超限停止为 `stopped_with_budget`，页面显示原因与已用额度，锁同样释放。发布页勾选 `auto_advance`（默认开启）时，登记成功回调会在登记锁释放后自动启动推进；进度/失败原因写入同一状态文件。

16. **继续修正（重修）**：`POST /revise/<编号>` 只在 `phase=revise` 时通过。预检包括：问题清单解析、受影响子任务角色绑定、reviewer 绑定、执行器空闲、无其它推进/分发/审核/登记、G3 审核完成闸门。通过后把本轮 `对接/审查.md` 复制为 `对接/审查历史/round-<n>.md`（同时快照 `审核完成.md`；原件不删除），把问题清单命中的 **delivered** 子任务重置为 pending（已通过项默认不重跑；`mode=all` 时才连同 passed 一起重置；未命中时按 `mode=all|review` 处理），把 `revise_round=n` 写回 `task.yaml`，生成**修订上下文**（父任务单绝对路径与禁止事项、本轮问题清单全文及命中本子任务的原文、`审查历史/round-n.md` 绝对快照路径与轮次、回归验收要求），随 advance/dispatch 状态传给每个被重置子任务的固定提示词，然后直接调用既有推进流程：分发剩余 → 自动审核。结果通过则阶段变为 `passed`；仍须修正则停在 `revise`，`revise_round` 已 +1；任一失败则停止。默认上限 3 轮，超过后按钮禁用，`POST` 返回 `409` + 请人工介入。修订记录写 `工作区/日志/revise-<时间戳>.log`，状态写同一 `advance-<编号>.status.json`（`revise=true`），可由 `GET /api/task/<编号>/revise-status` 轮询。审核线程只接受本轮新写入的 `审查.md` / `审核完成.md`（mtime+sha256 与基线不同），避免把上一轮结论误认为修正结果；本轮审核成功后还会检查 G4（`round-<n>.md` 与新一轮结论）。

17. **交接闸门与补交**：G1–G4 在任何分发、审核、修正前后执行（见「交接闸门」）。每次检查/补交开启一条 `工作区/日志/handoff-<时间戳>.log`，状态持久化在 `协作/调度台/handoff-<编号>.status.json`；每次补交复用 `runner.launch(kind=handoff)` 与 `runner.lock`，最多 2 次。仍缺则 `advance` 状态写 `handoff_blocked`（页面阶段 `handoff_blocked`、任务状态「需处理」），缺件与责任 Agent 写入状态/日志，避免静默继续。补交提示词固定要求只补缺件，非 reviewer 一律禁止写 `对接/审查.md`。

执行器不写 `对接/交付/`、`对接/审查.md` 或可执行产物；普通运行与分发只写上述日志/状态/历史/锁，分发还会更新 `对接/subtasks.yaml` 的 status/log/时间/错误字段。总控登记进程按固定提示词写 `对接/任务单.md`、`subtasks.yaml`、`task.yaml` 与 `登记完成.md`；控制台只额外写 `idea.md`、`task.yaml`。运行中、正在登记或正在分发的项目不能被归档。执行器不是系统级沙箱：被启动的 CLI 继承控制台环境，并拥有该 Agent CLI 自身的工具权限；请只登记可信 Agent，保持最小权限。

隔离测试：

```bash
python3 -m unittest -v console/test_runner.py console/test_registration.py console/test_dispatch.py console/test_review.py console/test_advance.py console/test_revise.py console/test_handoff.py console/test_audit_p1.py
```

`test_runner.py` 覆盖成功退出、并发拒绝、超时进程组清理、正常退出后的后台子进程清理、崩溃/非零退出、失效锁回收、活锁不被 reconcile 改写、任务编号与目录一致性、状态与日志、历史截取和归档日志回退、**cwd=workdir、产物落外部目录、日志留值班台、相对/系统根/协作 workdir 拒绝**，以及总控登记复用锁、超时释放锁。`test_registration.py` 在临时仓库中把一次登记运行到 `register_failed`，再重试到 `registered`，校验 2 个子任务、完成信号、日志和看板读取。`test_dispatch.py` 覆盖详情页无 Agent 选择器、预检 409 且零启动、两子任务串行与状态回写、单实例互斥、失败停止后续、日志链接与 runner.status `kind=dispatch`。`test_review.py` 覆盖全部交付后详情页切换为「开始审核」、无子任务但存在任务级 DONE/工作区产物的审核、`/api/tasks` 阶段与 x/y 字段、审核串行与 `kind=review`、通过/须修正两种结论与问题清单摘要、未绑定 reviewer 的 409、审核失败日志与重试、审核期间拒绝分发，以及 T24 署名三例（正确头部通过、AI-A 冒名拒收并隔离到 `invalid-attribution-*.md`、缺头部不进终态且可重试）。`test_advance.py` 用假总控/假 Agent 覆盖全自动推进到通过、须修正即停、分发失败即停、审核失败即停，以及发布时默认勾选 `auto_advance` 后无需人工点击即可完成登记→分发→审核；并校验串行锁与无并发 Agent；T24 另覆盖用户停止（终止 Agent + 释放锁）、子任务/运行时长/修订轮次三类预算停止为 `stopped_with_budget` 且无半锁。`test_revise.py` 覆盖须修正详情页按钮/问题清单/目标子任务、只重置受影响子任务并自动推进到通过（未受影响的子任务不重跑）、问题清单无编号时 409 二选一、`review`/`all` 两种策略、affected 模式跳过已通过项 / all 模式连已通过项一起重做、3 轮上限禁用与 409、审查历史 round-1/2/3 落盘、`revise_round` 写回 `task.yaml`、`revise-*.log`、HTML 表单 303 回详情与 `/api/tasks` 扩展字段。`test_handoff.py` 用假 Agent 覆盖 G2 缺 DONE.md 时自动回叫补齐后继续审核、连续两次补不齐进入 `handoff_blocked` 并停止、页面缺件清单与「重试体检」、G1/G3/G4 缺件样例的责任方、审核后 G3 自动补交、`/api/tasks` 阶段/status 派生与 `_delivery_evidence()`，并校验串行锁无重叠。`test_audit_p1.py` 是 T19 外部审计回归：F01 空/半份/不可识别审查不进终态、运行中优先、重试隔离 invalid 文件；F02 归档→恢复逐字段保留（含未知扩展）；F03 无活锁 running 置 `interrupted` 且保留 log/run_id、活锁不动、恢复后可继续分发；F04 修订提示词包含父任务单/禁止事项/问题原文/快照/轮次/回归验收且不同于首次提示词；F05 默认与外部 workdir 都按提示词绝对路径交付并通过 G2。测试都不触碰真实的 `项目/`、`协作/` 或 8787。

## 写入边界

控制台的服务端写入口：

- 发布任务：`POST /publish` 创建 `项目/<编号>_<短名>/对接/`，只写 `idea.md`（原样字节）与 `task.yaml`（含 `auto_advance`），然后启动总控。总控按固定提示词写 `任务单.md`、`subtasks.yaml`、`task.yaml`、`登记完成.md`；控制台只在校验完成后收口 `task.yaml`，并在失败时写 `register_failed`/错误/日志路径，不代写任务单或子任务。
- 文件夹：`POST /api/pick-folder` 只调用本机 `osascript` 原生选择框，不写文件；`POST /api/validate-folder` 只校验并返回路径，不创建目录。发布页选择的目录必须已存在。
- 旧版新建任务：`POST /new` 保留，只写 `对接/任务单.md` 与 `task.yaml`（独占创建）；新页面不再提交到它。
- 工作目录：`POST /task/<编号>/workdir` 只更新该任务的 `对接/task.yaml`；可以创建尚不存在的系统外目录。拒绝相对路径、系统根本身、`协作/` 及其子目录、系统根内非 `项目/.../工作区` 的路径，以及不可创建的路径。
- 归档 / 恢复：只用 `shutil.move` 整体移动一个项目目录（`项目/` ↔ `归档/`），不逐个文件改写；同名冲突时改名目录并加时间戳后缀。
- 角色配置：只写 `协作/roles/*.yaml`；删除是移动到 `协作/roles/.archive/*.yaml`，不调用删除。首次运行可能自动建 `协作/roles/` 并播种三个默认角色。
- Agent 登记：只写 `协作/agents/*.yaml`；登记与页面检测只运行 `--version`，超时 3 秒。
- 执行：`POST /dispatch/<编号>` 是人工分发/继续分发入口。它只按 `subtasks.yaml` 的 `agent_role` 解析 Agent，串行启动每个 pending/failed 子任务；failed 先重置为 pending，启动前原子更新该条 `status=running`，结束后写 `delivered/failed`、`log`、`started_at`、`finished_at`、`error`。执行器在任务 workdir 内启动 CLI，并在值班台侧 `工作区/日志/`、`协作/调度台/` 写入运行记录；它会释放自己创建的锁。发布登记是自动入口，复用同一把锁，只写 `register-*.log` 与任务项目内的登记文件。审核入口 `POST /review/<编号>` 复用同一把锁与执行器，由 `reviewer` 角色写 `对接/审查.md` 与 `对接/审核完成.md`，控制台只写 `review-*.log`、`review-<编号>.status.json` 与 `runner.status`。`POST /advance/<编号>` 只编排分发/审核并写 `advance-<编号>.status.json`，不直接启动 Agent；发布时勾选自动推进会在登记成功后调用同一套推进逻辑。`POST /revise/<编号>` 在预检后复制旧审查到 `对接/审查历史/`、重置指定子任务并写 `revise_round` / `revise-*.log`，然后复用同一套 advance 编排；它不删除旧审查原件、不写新的 `对接/审查.md`（新结论仍由审核角色写）。`POST /handoff/retry/<编号>` 只发起交接体检与有上限的补交，不直接写任何 `对接/` 文件；缺件补交由责任角色 Agent 在 `kind=handoff` 运行中完成，控制台只写 `handoff-*.log`、`handoff-<编号>.status.json` 和运行记录。这些入口都不删除用户文件，**分发路径不写 `对接/审查.md`**，也不代写 `对接/交付/`。外部 workdir 只接收该任务的产物，运行日志不写进去。

角色/Agent 页面不写任何任务数据；它们及调度台页面不会因刷新而启动进程。自动检测只读本机 CLI 元信息。由控制台启动的 Agent 进程只来自任务详情页显式发起的「开始/继续分发」「开始审核」「全自动推进」，或发布/重试触发的固定总控登记及发布后自动推进。

## 守卫状态

`GET /dispatch` 只读 `协作/调度台/guard.status`。该文件由菜单栏应用（`desktop/`）写入：

```text
state=running        # running | stopped
updated=2026-10-24 01:23:45
updated_epoch=1792776225
app_pid=12345
watch_pid=12346      # 当前守卫运行器进程（进程组内含 watch.sh），等待期间为 -
note=常驻中 · 正在观察 项目/*/对接/
```

- 守卫的启停、崩溃后 3 秒重挂、检测到变化后写 journal 并刷新基线，全部由菜单栏应用负责。
- 网页不提供守卫启停；`/dispatch` 的守卫区域只读。由网页启动的进程只有三类：任务详情页显式发起的执行器子进程（分发/审核/全自动推进）、发布/重试触发的固定总控登记进程，以及发布后自动推进串行启动的分发/审核子进程。
- `watch.state` 若为空，下一轮守卫会以空基线比较，现有档案可能被报告为“消失”；菜单栏应用会在检测到变化后自动运行 `ack.sh` 刷新基线。
- `journal.log` 只追加；控制台从不写它。
- `watch.sh` / `ack.sh` 观察 `项目/*/对接/` 下全部文件（递归，包含各轮交付）；不观察 `工作区/`。

验收时的 curl 输出与隔离测试记录见 [`验收记录.md`](验收记录.md)（迁移前）与 [`验收记录-T01.md`](验收记录-T01.md)（项目目录迁移后）。执行器验收证据保存在 `项目/T06_接入执行器调用DeepSeekHarness/工作区/证据/`。

## 已弃用目录

`协作/任务单/`、`协作/交付/`、`协作/审查/` **已弃用**，按“旧版永不删除”原样保留，不参与看板、状态判定或调度台扫描。新数据只写 `项目/`。
