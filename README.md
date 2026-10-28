# 多智能体协作系统

执行队列：看板右上角或侧栏点击「执行队列」，从看板选择已有任务，或在「批量布置」一次写下多张任务（每批最多 20 张、共用项目文件夹）。保存后只排队；调整顺序并点击「开始执行」，系统才逐张登记、分发和审核，审核通过后继续下一张。可在当前任务完成后暂停，失败或须修正会自动暂停后续任务。关闭抽屉不影响执行；控制台重启后保留顺序并等待手动继续。

原型：AI 协作方案（独立项目目录 + 文件夹交接 + 背景守卫）。

- 任务项目：`项目/<编号>_<短名>/`
- 本地控制台：`console/`，<http://127.0.0.1:8787>
- 菜单栏控制器：`desktop/`，控制控制台启停并常驻运行守卫
- 调度台与规则：`协作/调度台/`、`协作/协作说明.md`
- 角色与 Agent 配置：`协作/roles/*.yaml`、`协作/agents/*.yaml`（页面 `/roles`、`/agents`）
- 发布任务：`/new` 选本机项目文件夹 → 写想法 → 「发布并登记」；系统保存人类原文，自动调用总控拆出 2–6 个子任务
- 执行器：`console/runner.py`，任务详情页「开始分发实现 / 继续分发」→ 按 `subtasks.yaml` 的 `agent_role` 串行执行 pending/failed 子任务，已交付项不重跑；全部交付后切换为「开始审核」→ 由 `reviewer` 角色复核并写 `对接/审查.md` / `对接/审核完成.md`。新审查必须在头部署名 `- 审核角色：<角色名>（<Agent ID>）` 与 `- 审查时间：…`；出现 `AI-A` 冒名字样会整份拒收并隔离到 `对接/审查历史/invalid-attribution-*.md`。审核结论为「须修正」时详情页出现「继续修正（第 n 轮）」：只重置问题清单涉及的子任务，旧审查复制到 `对接/审查历史/round-n.md`，然后自动继续分发并重新审核，默认最多 3 轮。页面另有「全自动推进」，发布时也可勾选「发布后自动推进」（默认开启）；运行记录在 `/runs`
- 交接闸门：分发、审核、修正前后自动检查 G1–G4（登记四件套、每个子任务的 DONE.md + 非 DONE 产物、审查结论 + 审核完成、修正历史 + 新一轮结论）。缺件时按责任角色回叫 Agent 补交，每道闸门最多 2 次；仍缺则停止为 `handoff_blocked`，详情页列出缺件与责任方并提供「重试体检」。检查与补交日志在 `工作区/日志/handoff-*.log`；补交与分发/审核共用 `runner.lock` 串行执行
- 2026-10-27 修复：停止操作等待全部工作线程与进程组收尾；结果按唯一 run_id 读取；失效锁回收与编号分配使用跨进程事务锁。中断的修订可恢复原轮次与上下文。界面新增搜索筛选、发布草稿、单一主操作与手动分步入口。详见 `协作/审计/2026-10-27-修复/修复与验收记录.md`。
- 技术约束：只用 Python 3 标准库、系统 bash 与 Swift/AppKit，不安装第三方依赖；控制台只监听本机回环地址

## 目录约定

每个任务拥有一个独立项目目录；任务相关的全部交接文件都收在 `对接/`：

```text
项目/
└── T01_改为任务项目目录/
    ├── 对接/
    │   ├── idea.md                ← 发布时的人类原文（不改写）
    │   ├── task.yaml              ← 任务元数据：id/title/workdir/created_at/status/revise_round…
    │   ├── 任务单.md              ← 总控登记时写入；控制台看板据此显示
    │   ├── subtasks.yaml          ← 总控拆出的 2–6 个子任务
    │   ├── 登记完成.md            ← 总控最后写入的登记完成信号
    │   ├── 交付/
    │   │   └── r1/DONE.md         ← AI-B 写入；DONE.md 是完成唯一信号
    │   ├── 审查.md                ← 审核角色写入：结论（通过 / 须修正）+ 证据 + 问题清单
    │   ├── 审核完成.md            ← 审核角色的完成信号
    │   └── 审查历史/
    │       └── round-1.md         ← 继续修正时复制的上一轮审查快照（原件不删除）
    └── 工作区/                    ← 任务工作区；独立产物或源码任务的交付记录
        └── 日志/
            ├── dispatch-<子任务>-*.log ← 每个子任务的 Agent stdout+stderr
            ├── review-*.log       ← 审核 Agent 的 stdout+stderr
            ├── handoff-*.log      ← 交接闸门的检查项、结论、补交次数与 Agent
            ├── run-*.log          ← 通用执行器运行日志
            └── register-*.log     ← 总控登记调用日志
```

- 控制台的任务来源是 `项目/*/对接/任务单.md`；登记阶段还没有任务单，因此先显示在 `/registering/<编号>`。
- 发布任务（`/new` → `POST /publish`）先分配编号，只写 `对接/idea.md` 与 `对接/task.yaml`（`status: registering`），再通过执行器单实例锁调用总控。总控写 `任务单.md`、`subtasks.yaml`、更新 `task.yaml` 并最后写 `登记完成.md`；控制台校验这些文件后把状态收口为 `registered`。
- 登记失败时 `task.yaml` 为 `register_failed` 并带日志路径；`/registering/<编号>` 可重试，不丢失人类原文。
- 看板状态由工作流阶段派生（`review_ready`/`reviewing` → 已交付，`handoff_blocked` → 需处理），交付依据统一为任务级 `DONE.md`、全部子任务已交付或（无子任务时）工作区产物；即使没有任务级 DONE.md，5/5 子任务已交付也会显示「已交付 / 待审核」。看板另显示「子任务 x/y」与工作流阶段（未分发 / 分发中 / 待审核 / 审核中 / 已通过 / 须修正 / 交接阻塞），已交付/已通过计入完成数。
- 详情页列出该任务的全部 `对接/` 文件、`idea.md`、子任务清单，以及 `工作区/` 文件列表。
- 归档只把整个项目目录移动到 `归档/<编号>_<短名>/`；恢复再整体移回。运行中或正在登记的项目不能归档；归档项目仍可只读打开详情。
- 角色与 Agent 配置只写入 `协作/roles/`、`协作/agents/`。角色删除会保留原件到 `.archive/`；Agent 检测只执行 `--version`（3 秒超时）。
- 分发时由 `console/runner.py` 逐个启动子进程：cwd 固定为该任务的 `workdir`，stdin 为空，全局单实例锁保证同一时刻只有一个写手；主进程退出后先清理遗留进程组再释放锁。每个子任务结束后原子回写 `subtasks.yaml` 的 `status` / `log` / 时间 / 错误，失败默认停止后续。再次点击分发只处理 pending/failed，失败项先重置为 pending，delivered/passed 不重跑。全部交付（或存在任务级 DONE / 无子任务工作区产物）后详情页切换为「开始审核」，审核同样复用这把锁与进程组收尾，日志写 `review-*.log`、`runner.status` 记 `kind=review`。总控登记复用同一把锁、同样使用空 stdin 与超时。
- 执行器不是系统级沙箱：Agent 继承控制台环境并受其自身 CLI 权限约束，只登记可信 Agent。
- 控制台和执行器**不写** `对接/交付/`、`对接/审查.md`；总控登记只写任务项目内的 `对接/` 登记文件与日志。控制台本身只写 `idea.md`、`task.yaml`，分发时只改 `subtasks.yaml` 的目标子任务字段。
- 运行状态、超时和日志尾在 `/runs`；调度台显示执行器空闲/运行中；任务详情页轮询 `/api/task/<编号>/dispatch-status` 显示当前子任务与 x/y 进度，审核阶段轮询 `/api/task/<编号>/review-status`，全自动推进轮询 `/api/task/<编号>/advance-status`，继续修正轮询 `/api/task/<编号>/revise-status`，完成后刷新为「已通过 / 须修正」。单次子任务默认超时 3600 秒，可用 `--run-timeout` 覆盖；总控登记默认 900 秒，可用 `--register-timeout` 覆盖。
- `POST /review/<编号>` 的预检：已交付（全部子任务 delivered/passed，或任务级 DONE.md / 无子任务时的工作区产物），且 `reviewer` 角色已绑定可执行 Agent；不要求一定有子任务。否则返回 409 与原因，不启动任何进程。审核失败写 `review_failed` 与日志路径，可在同一页面重试。
- `POST /advance/<编号>`：循环分发剩余 → 自动审核，直到通过 / 须修正 / 失败。每一步前先跑交接闸门（分发前 G1/G2，审核前 G2，审核后 G3；修正轮再查 G4）；缺件回叫责任角色，最多补交 2 次，仍缺则记为 `handoff_blocked` 并停止。每一步仍由执行器抢同一把单实例锁；状态可由 `GET /api/task/<编号>/advance-status` 轮询，闸门详情用 `GET /api/task/<编号>/handoff-status`。发布页「发布后自动推进」默认勾选，登记成功后自动启动。
- `POST /advance/<编号>/stop`：用户显式停止推进；终止当前 Agent 进程组、等待监视线程收尾并释放 `runner.lock`，状态写 `stopped`，之后不会自动重新拉起。详情页推进中显示「停止推进」按钮。
- 推进预算：默认最长运行 2 小时、最多 12 个子任务、最多 3 轮修订；超出即停止为 `stopped_with_budget`，页面显示原因与已用额度，同样释放锁。可用 `--advance-max-runtime` / `--advance-max-subtasks` / `--advance-max-revise-rounds` 或对应 `COLLAB_ADVANCE_MAX_*` 环境变量配置。
- `POST /revise/<编号>`：仅当阶段为「须修正」时可用。解析 `对接/审查.md` 问题清单中的 `Txx-n`，只把这些子任务重置为 pending（未命中时由页面二选一：全部重做 / 只重新审核）；旧审查复制到 `对接/审查历史/round-<n>.md`，`revise_round` 写回 `task.yaml`，随后直接复用全自动推进。默认最多 3 轮，达到上限后按钮禁用并提示人工介入。状态可由 `GET /api/task/<编号>/revise-status` 轮询，日志写 `工作区/日志/revise-<时间戳>.log`。

## 项目源码与任务档案

发布页的“这个任务怎样工作”提供三种方式：

| 工作方式 | 项目目录 | Agent 写入与交付 |
| --- | --- | --- |
| 修改项目源码（新任务默认） | 已存在的外部项目根目录 | 按子任务范围直接修改同一份源码；完成说明与验证清单放在任务档案内 |
| 维护工作台自身 | 自动使用当前工作台根目录 | 修改本系统源码；保留运行数据、本机 Agent 登记与任务档案边界，验收使用临时目录与独立端口 |
| 生成独立产物 | 所选工作目录 | 沿用旧版：每个子任务只写 `workdir/子任务/<id>/` |

- 工作方式记录在 `对接/task.yaml` 的 `work_mode`，分别为 `project`、`self`、`isolated`。未记录此字段的旧任务继续按 `isolated` 执行，原交付路径不迁移。
- 源码任务的执行 Agent 共用任务的 `workdir`；总控登记、审核与补交提示词也明确引用同一源码目录。总控登记仍在任务档案目录生成登记文件，审核按同一份源码核对结果。
- 源码任务的每个子任务把 `DONE.md` 与 `changes.json` 写到 `项目/<编号>_<短名>/工作区/交付/子任务/<子任务id>/`。`changes.json` 包含 `files`（项目相对路径列表）与 `checks`（实际验证命令和结果列表）。纯检查、提交或推送任务可不修改文件，但仍要记录验证/操作证据。
- G2 验证完成说明的新鲜度、清单格式与路径：拒绝越界、符号链接、运行数据和本机路由配置。审核读取清单涉及的实际源码，不把交付说明当成代码。
- 源码任务的修正继续修改原项目；归档的只是本任务旧交付记录，不会把项目源码移走。任务归档/恢复也只移动任务档案，外部项目与自维护源码留在原处。
- 已开始执行或已有交付/审查记录的任务不能切换工作方式，避免旧记录失效；尚未执行的任务可在详情页修改。
- 自维护任务可以修改 `console/`、`desktop/`、`scripts/`、文档与源码配置。`协作/调度台/` 中五个运行脚本属于源码；锁、状态、日志、快照和令牌属于运行数据。本机角色与 Agent 登记不作为源码交付。
- 这些是应用校验、提示词及交付验收的边界，并非操作系统沙箱；Agent 的实际文件访问能力由其 CLI 权限决定。自维护任务不能自行重启当前 8787 或菜单栏，重启需求写入完成说明。

新增 `.gitignore` 排除任务、归档、运行状态、令牌、日志、快照、审计备份、构建产物及本机角色/Agent YAML；源码与调度脚本保留。尚未初始化 Git 或设置远端，提交/推送仍需在任务中明确要求并指定仓库。新克隆后请在 Agent 应用中登记本机 CLI，再绑定角色。

## 全新克隆如何跑

公开仓库纳入 `desktop/runner.sh`、`协作/调度台/watch.sh`、`协作/调度台/ack.sh`
三个运行脚本；`watch.state`、`journal.log`、`guard.status`、`snapshots/` 等运行态不入库。
四个调度脚本共用 `协作/调度台/state.py`，修订上限统一来自 `协作/settings.json`（默认 3），可用 `COLLAB_MAX_REVISE_ROUNDS` 覆盖。守卫比较内容哈希与纳秒时间；新快照对未改变文件使用硬链接，原有快照原样保留。

全新克隆后按下面顺序执行：

```bash
git clone <仓库地址>
cd collab-duty-desk

# 可选：在临时目录完整复刻一次克隆，构建 .app 并校验脚本与导入
bash scripts/check-fresh-clone.sh

# 运行
bash desktop/build.sh
open desktop/build/CollabMenuBar.app
# 或不用菜单栏：python3 console/server.py
```

`scripts/check-fresh-clone.sh` 只使用系统 `bash`、`tar`、`swiftc`、`python3`：
临时目录复制仓库 → `bash desktop/build.sh` → 断言 `.app` 主程序可执行、
三个运行脚本存在且可执行、`python3 -c "import console.server"` 可导入；
不会修改源仓库，也不触碰 8787。

## 快速开始

在仓库根目录执行：

```bash
bash desktop/build.sh
open desktop/build/CollabMenuBar.app
```

菜单栏右上角会出现「协作」状态项：

- 日常入口：打开任务看板（⌘O）、发布新任务（⌘N）、查看运行记录（⌘R）；控制台未启动时会先启动，等待就绪再打开对应页面。
- 项目管理：角色配置、Agent 应用、归档库、打开项目文件夹。
- 运行与维护：查看调度台、按当前状态启动/停止控制台、重启控制台、暂停/恢复文件守卫；控制台日志与守卫日志分开打开。
- 控制台后台运行 `python3 console/server.py --port 8787`，退出应用时清理受控进程。应用内每 2 秒做一次 TCP 健康检查，不可达时按 1/2/4/8/16/30 秒退避重启，自动恢复次数与时间写入 `协作/调度台/console.log`。用户手动停止后不再自动拉起；外部启动的服务可访问，但不能由菜单停止/重启。
- 文件守卫在应用内常驻循环运行 `协作/调度台/watch.sh 7200`；检测到变化后写 `journal.log`、刷新基线并立即重挂。暂停文件守卫不会停止任务执行。
- 网页 `/dispatch` 显示守卫状态与执行器状态；守卫不能从网页启停。
- 分发实现：打开任务详情页 → 「开始分发实现」或「继续分发（余 n / 重试 m）」→ 二次确认。系统读取 pending/failed 子任务的 `agent_role`，解析角色绑定的 Agent 并串行执行；失败项先重置为 pending，已交付项不会重跑。页面不选 Agent，也不传整段提示词。预检不通过会返回 409 并列出未绑定的子任务/角色，一个都不启动。全部交付后，同一位置切换为「开始审核」，由 `reviewer` 绑定的 Agent 复核；页面会自动刷新结论。也可以点「全自动推进」，或在发布时保留默认勾选的「发布后自动推进」，让系统连续执行到审核终态；须修正或任一失败会停下等待人工处理。

仍可手动调试控制台：

```bash
python3 console/server.py
```

打开 <http://127.0.0.1:8787>，按 `Ctrl+C` 停止。手动进程不受菜单栏退出清理。

全新克隆/看护自检（都不触碰 8787；看护自检使用 8790 假实例）：

```bash
bash scripts/check-fresh-clone.sh   # 临时目录复制仓库 → 构建 → 三脚本 + import 检查
python3 scripts/check-agent-cleanup.py # 验证忽略 TERM 的 Agent 也会被清理
bash scripts/check-watchdog.sh      # 构建菜单栏应用 → 8790 两次 kill/退避恢复 → 手动停止不复活
```

执行器测试与临时验收使用独立端口，绝不复用或重启 8787：

```bash
python3 console/server.py --port 8789 --run-timeout 3600 --register-timeout 900
python3 -m unittest -v console/test_runner.py console/test_registration.py console/test_dispatch.py console/test_review.py console/test_advance.py console/test_revise.py console/test_handoff.py
```

总控登记会解析 `协作/roles/controller.yaml` 的 `agent_id`；未绑定、未登记或不可执行时依次回退 `openclaw-main`、`deepseek-harness`，发布页会标明本次使用的总控。


## 文档

- [`协作/协作说明.md`](协作/协作说明.md)：角色、目录、防护规则与叫醒流程
- [`console/README.md`](console/README.md)：页面、接口、状态判定与写入边界
- [`desktop/README.md`](desktop/README.md)：菜单栏应用、进程模型、构建与自动化接口
- [`协作/调度台/`](协作/调度台/)：`watch.sh` / `ack.sh` / `since.sh` / `rounds.sh`

## 已弃用目录

以下旧目录**已弃用**，仅按“旧版永不删除”保留作历史档案，不再被控制台或调度台扫描：

- `协作/任务单/`
- `协作/交付/`
- `协作/审查/`

新任务与新的交付/审查必须写入 `项目/<编号>_<短名>/对接/`。
