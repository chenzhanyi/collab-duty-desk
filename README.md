# 协作值班台（Collab Duty Desk）

以**文件为协议**的多智能体协作控制台：人类发布想法 → 总控登记并拆子任务 → 各角色绑定的 Agent 串行执行 → 审核 → 修订 → 归档。
没有数据库、没有消息队列；任务状态由文件夹与 `DONE.md` 驱动，**一切可审计、可回溯**。

> 状态：早期可用版本（early alpha），在 macOS 上开发与验证；Python 3.9+，**零第三方依赖**。

## 特性

- **发布即登记**：网页里选项目文件夹、写想法，一键发布；总控 Agent 自动起标题、写任务单、拆成 2–6 个子任务。
- **按角色分发**：每个子任务带 `agent_role`，系统解析角色绑定的 Agent，**串行**执行（单实例锁，绝不并发写同一任务）。
- **审核与修订**：全部子任务交付后进入审核；结论「通过 / 须修正」；须修正可「继续修正」，轮次上限 3，防死循环。
- **全自动推进**：分发 → 审核一条龙；中途停止可「继续分发」，只跑未完成项。
- **交接闸门**：每次阶段推进前检查必要文件（任务单、子任务 DONE、审查结论等）；缺失则**回叫上一步的 Agent 补交**，两次补不齐即 `handoff_blocked` 待人工。
- **工作目录隔离**：值班台（系统根）与任务工作目录分离；工作目录可指向系统外的真实项目。
- **本地优先**：只监听 `127.0.0.1`；Host 白名单、写操作 Origin 校验、CSP 等安全头。
- **菜单栏控制器**（可选，macOS）：常驻右上角，启停控制台与守卫。

## 快速开始

```bash
# 1) 准备配置（把示例复制到工作目录）
mkdir -p 协作/roles 协作/agents
cp config-examples/roles/*.yaml  协作/roles/
cp config-examples/agents/*.yaml 协作/agents/
# 2) 按需修改 roles/agents 中的 command / args_template / cwd

# 3) 启动（默认 127.0.0.1:8787）
python3 console/server.py
```

打开 <http://127.0.0.1:8787>。

## 全新克隆如何跑

公开仓库只纳入运行所需脚本，不含 `watch.state / journal.log / guard.status / snapshots`
等运行态。全新克隆后按下面顺序执行：

```bash
git clone https://github.com/chenzhanyi/collab-duty-desk.git
cd collab-duty-desk

# 0) 可选：在临时目录做一次全新克隆自检（构建 .app + 三个运行脚本 + 导入控制台）
bash scripts/check-fresh-clone.sh

# 1) 准备角色与 Agent 配置
mkdir -p 协作/roles 协作/agents
cp config-examples/roles/*.yaml  协作/roles/
cp config-examples/agents/*.yaml 协作/agents/

# 2) 启动控制台（或先构建菜单栏应用，由它托管控制台与守卫）
python3 console/server.py
# 可选：bash desktop/build.sh && open desktop/build/CollabMenuBar.app
```

`scripts/check-fresh-clone.sh` 会在 `mktemp -d` 的临时目录中复制仓库、执行
`bash desktop/build.sh`，并断言 `.app` 主程序可执行、`desktop/runner.sh`、
`协作/调度台/watch.sh`、`协作/调度台/ack.sh` 存在且可执行、`import console.server`
成功；源仓库不会被修改。

## 目录约定

```text
值班台（系统根）/
├── console/          控制台（Python 标准库 + 原生 JS）
├── desktop/          macOS 菜单栏控制器（Swift + AppKit，可选）
├── 协作/
│   ├── roles/        角色定义（总控/设计师/审核/…，绑定 agent_id）
│   ├── agents/       Agent 应用登记（command + args_template + cwd）
│   └── 调度台/       运行态：锁、状态、日志、快照
├── 项目/<编号>_<短名>/
│   ├── 对接/         任务单 · task.yaml · subtasks.yaml · 交付/ · 审查.md
│   └── 工作区/       产物 · 子任务输出 · 日志
└── 归档/             归档任务（整体移动，不删除）
```

## 工作流

1. **发布**：`/new` 选文件夹 → 写想法 → 发布（可勾选「发布后自动推进」）
2. **登记**：总控写 `任务单.md` + `subtasks.yaml`（含每个子任务的 `goal / acceptance / agent_role`）
3. **分发**：按角色绑定串行执行；每个子任务写 `工作区/子任务/<id>/DONE.md`
4. **审核**：审核角色按验收标准复核，写 `对接/审查.md`（通过 / 须修正）+ `审核完成.md`
5. **修订**：须修正 → 「继续修正（第 n 轮）」→ 只重做受影响子任务 → 自动复审
6. **归档**：整体移入 `归档/`，可恢复

## Agent 接入

任何一个能接受提示词并输出的 CLI 都可以登记为 Agent：

```yaml
# config-examples/agents/example.yaml
id: my-agent
name: My Agent
command: /usr/local/bin/my-cli
args_template: '-p "{prompt}"'
cwd: /path/to/workdir
installed: true
version: "1.0"
notes: "写入边界与超时说明"
```

示例包含 Claude Code 包装器（`claude-ds`）、OpenClaw CLI、DeepSeek Harness headless 适配器。
`config-examples/bin/deepseek-harness.sh` 需要 `export DEEPSEEK_API_KEY=...`。

## 安全与边界

- 只监听 `127.0.0.1`；Host 白名单；写操作校验 `Origin`。
- 控制台只写任务单/配置/日志；**不写** `交付/`、`审查.md`（由对应角色 Agent 写）。
- 归档 = 移动目录，不删除文件。
- Agent 与人类同用户权限运行，**没有强沙箱**；请只绑定可信 Agent，并注意提示词注入风险。

## 开发

```bash
cd console
python3 -m unittest discover -p 'test_*.py' -v
```

测试在临时目录运行，不触碰真实 `项目/` 与运行中的控制台。

## License

未指定（如需开源授权，请自行添加 LICENSE）。
