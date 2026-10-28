# 协作目录

- 规则： [`协作说明.md`](协作说明.md)
- 调度台： [`调度台/`](调度台/)
- 任务项目： [`../项目/`](../项目/)
- 角色配置： [`roles/`](roles/)（`<role-id>.yaml`）
- Agent 登记： [`agents/`](agents/)（`<agent-id>.yaml`）
- 本地控制台： [`../console/README.md`](../console/README.md)

## 目录约定

每个任务一个独立项目目录；交接文件统一放在项目内的 `对接/`：

```text
项目/<编号>_<短名>/
├── 对接/
│   ├── 任务单.md
│   ├── task.yaml            # id/title/workdir/created_at
│   ├── 交付/r1/DONE.md ...
│   └── 审查.md
└── 工作区/                  # 默认 workdir；可改到系统外
```

控制台只扫描 `项目/*/对接/任务单.md`；调度台只观察 `项目/*/对接/`（递归）。状态由本项目的 `对接/审查.md` 与 `对接/交付/**/DONE.md` 决定。

值班台（系统根）不是 Agent 的工作区；每个任务的绝对工作目录记录在 `对接/task.yaml` 的 `workdir`，默认 `工作区/`，可在系统外。角色与 Agent 是两层配置：角色保存名称、职责、写入范围与绑定的 `agent_id`；Agent 登记本机 CLI 路径、参数模板与版本。任务详情页可显式派发一次 Agent；`console/runner.py` 用固定提示词、**任务 workdir cwd**、空 stdin 与全局单实例锁运行，日志仍写值班台侧任务的 `工作区/日志/`，运行状态在 `协作/调度台/runner.*`，页面 `/runs` 可查看。

守卫由 macOS 菜单栏应用常驻管理；网页 `/dispatch` 读取 `guard.status` 并显示执行器状态，不提供守卫启停。执行器只从任务详情页派发。

## 启动

```bash
bash desktop/build.sh
open desktop/build/CollabMenuBar.app
```

在菜单栏中启动控制台，再打开 <http://127.0.0.1:8787>。仅调试控制台时也可以手动运行：

```bash
python3 console/server.py
```

## 已弃用目录

以下目录**已弃用**，仅为“旧版永不删除”之约定原样保留，不再参与扫描或状态判定：

- `任务单/`
- `交付/`
- `审查/`

不得再向其中写入新内容。
