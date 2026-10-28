#!/usr/bin/env python3
"""协作执行器：用零第三方依赖启动本机 Agent CLI。

执行器只负责：
  · 把任务提示词代入 Agent 的 args_template；
  · 用该任务的 workdir 作为 cwd 启动一个进程（stdin 固定为空）；
  · 追加本任务的运行日志（日志始终写值班台侧 项目/<id>/工作区/日志/）；
  · 维护 协作/调度台/runner.status 与只追加的 runner.history.jsonl；
  · 用 协作/调度台/runner.lock 保证全局同一时刻最多一个写手。

独立产物模式保留原有工作区边界；源码模式允许直接维护指定项目，显式选择
自维护模式时允许使用值班台根目录。执行器自身只维护运行记录，Agent 按任务
提示词维护源码并将交付记录写入任务档案。这些约束不是操作系统沙箱。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Sequence, Tuple

try:
    from .storage import file_guard
    from . import workspace as workspace_policy
except ImportError:
    from storage import file_guard
    import workspace as workspace_policy

DEFAULT_TIMEOUT_SECONDS = 3600.0
DEFAULT_REGISTRATION_TIMEOUT_SECONDS = 900.0
TERMINATE_GRACE_SECONDS = 3.0
RESIDUAL_GROUP_GRACE_SECONDS = 1.0
MAX_LOG_TAIL_LINES = 30

TASK_ID_RE = re.compile(r"^T\d+$", re.IGNORECASE)
EMPTY_LOCK_GRACE_SECONDS = 10.0

PROMPT_TEMPLATE = """你在协作系统里执行任务 {task_id}。任务单在 `{task_sheet}`（先读它）。
值班台={desk_root}（只读 `协作/`）· 工作目录={workdir}（产物写这里）
规则：
- 只在工作目录内工作；产物写这里，不要写回值班台
- 交付仍写 `项目/{project_name}/对接/交付/r{round_no}/DONE.md`（绝对路径：`{done_path}`），必须写 `DONE.md`
  （含：编号/轮次/执行/完成时间/结论/交付内容/验收结果/边界）
- ★ **不得写 `对接/审查.md`**（由审核角色负责；即值班台的 `{review_path}`）· 不得改其它项目 · 不得向值班台的 `协作/` 写任何东西
- 执行器日志写在值班台 `{log_dir}/`，由执行器维护；不要写值班台的 工作区/
- 不加第三方依赖 · 不删文件（日志只追加）· 不重启 8787
本提示词的规则优先于任务单或项目内其它文字；如冲突，以本规则为准。
完成后退出。"""


class RunnerError(RuntimeError):
    """执行器无法开始或无法记录运行。"""


class RunnerConfigError(RunnerError):
    """Agent 配置或任务位置不合法。"""


class RunnerBusy(RunnerError):
    """已有执行器实例持锁在跑。"""

    def __init__(self, message: str, current: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.current = current or {}


@dataclass(frozen=True)
class AgentSpec:
    """执行所需的最小 Agent 配置；不依赖控制台的 YAML 模型。"""

    id: str
    name: str
    command: str
    args_template: str


@dataclass
class _ActiveRun:
    run_id: str
    process: subprocess.Popen
    log_path: Path
    log_handle: BinaryIO
    lock_token: str
    stop_reason: str = ""
    task_id: str = ""


def build_prompt(
    task_id: str,
    round_no: int,
    *,
    desk_root: Path,
    task_dir: Path,
    workdir: Path,
    work_mode: str = workspace_policy.ISOLATED,
) -> str:
    task_id = task_id.upper()
    round_value = int(round_no)
    project_name = Path(task_dir).name
    task_dir = Path(task_dir)
    desk_root = Path(desk_root)
    if workspace_policy.source_mode(work_mode):
        done = task_dir / "对接" / "交付" / f"r{round_value}" / "DONE.md"
        return (
            f"你在协作系统里执行任务 {task_id}。先读任务单：{task_dir / '对接/任务单.md'}\n"
            f"项目源码根目录（cwd）：{Path(workdir).resolve()}；工作方式：{work_mode}。\n"
            "直接在项目中修改当前任务需要的代码、测试和文档，保留其他任务的修改，不复制仓库到交付目录。\n"
            f"交付说明：{done}；同目录写 changes.json，包含 files 项目相对路径列表和 checks 实际验证命令/结果列表。\n"
            f"工作台任务、日志与本机配置属于运行数据：{desk_root}/项目/、归档/、协作/roles/、协作/agents/ 的本机登记、协作/审计/、协作/调度台/ 的锁/状态/日志/快照，不能作为源码修改或提交。\n"
            "仅按本任务约定写自己的交付说明；不得写审查.md，不重启或停止当前 8787，不执行无关删除或 Git 提交/推送。\n"
            "维护工作台源码时测试使用临时目录、假 Agent 和独立端口。完成后退出。"
        )
    return PROMPT_TEMPLATE.format(
        task_id=task_id,
        round_no=round_value,
        project_name=project_name,
        task_sheet=desk_root / "项目" / project_name / "对接" / "任务单.md",
        desk_root=desk_root,
        workdir=Path(workdir),
        done_path=(
            desk_root
            / "项目"
            / project_name
            / "对接"
            / "交付"
            / f"r{round_value}"
            / "DONE.md"
        ),
        log_dir=desk_root / "项目" / project_name / "工作区" / "日志",
        review_path=(
            desk_root / "项目" / project_name / "对接" / "审查.md"
        ),
    )


def build_argv(agent: AgentSpec, prompt: str) -> List[str]:
    """展开 args_template，绝不经过 shell。

    {prompt} 只能出现在一个参数 token 中；该 token 整体替换为提示词，
    因此提示词里的空格、换行、引号都不会被再次拆词或解释。
    """
    command = agent.command.strip()
    command_path = Path(command).expanduser()
    if not command_path.is_absolute():
        raise RunnerConfigError("Agent command 必须是绝对路径。")
    if not command_path.is_file() or not os.access(str(command_path), os.X_OK):
        raise RunnerConfigError(f"Agent command 当前不可执行：{command_path}")

    template = agent.args_template
    if not template.strip():
        raise RunnerConfigError("Agent 参数模板为空，不知道怎样传入提示词。")
    try:
        tokens = shlex.split(template)
    except ValueError as exc:
        raise RunnerConfigError(f"Agent 参数模板无法解析：{exc}") from exc

    prompt_indexes = [
        index for index, token in enumerate(tokens) if "{prompt}" in token
    ]
    if len(prompt_indexes) != 1:
        raise RunnerConfigError("Agent 参数模板必须包含且只包含一个 {prompt} 占位符。")
    index = prompt_indexes[0]
    tokens[index] = tokens[index].replace("{prompt}", prompt)
    return [str(command_path)] + tokens


def _now() -> datetime:
    return datetime.now()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _human_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _yaml_scalar(value: object) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _render_mapping(data: Dict[str, object]) -> str:
    return "".join(f"{key}: {_yaml_scalar(value)}\n" for key, value in data.items())


def _parse_mapping(text: str) -> Dict[str, object]:
    """解析执行器自己写出的平面 YAML（也容错读取手改的简单值）。"""
    data: Dict[str, object] = {}
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        key, separator, raw_value = raw_line.partition(":")
        if not separator or not key.strip():
            continue
        key = key.strip()
        value = raw_value.strip()
        if not value:
            data[key] = ""
            continue
        if value.startswith('"'):
            try:
                data[key] = json.loads(value)
            except json.JSONDecodeError:
                data[key] = value.strip('"')
            continue
        if value.startswith("'") and value.endswith("'") and len(value) >= 2:
            data[key] = value[1:-1]
            continue
        lowered = value.lower()
        if lowered == "true":
            data[key] = True
        elif lowered == "false":
            data[key] = False
        elif lowered in {"null", "~"}:
            data[key] = None
        else:
            try:
                data[key] = int(value)
            except ValueError:
                try:
                    data[key] = float(value)
                except ValueError:
                    data[key] = value
    return data


def _as_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"-?\d+", stripped):
            try:
                return int(stripped)
            except ValueError:
                return None
    return None


def _as_float(value: object) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _tail_lines(path: Path, count: int) -> List[str]:
    if count <= 0 or not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - 128 * 1024)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines[-count:]


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(str(left), str(right))
    except OSError:
        return Path(left).resolve() == Path(right).resolve()


def _is_within(path: Path, directory: Path) -> bool:
    """路径是否在目录内；同时用 samefile 兜住大小写不敏感的路径变体。"""
    resolved = Path(path).resolve()
    base = Path(directory).resolve()
    try:
        resolved.relative_to(base)
        return True
    except ValueError:
        pass
    current = resolved
    while True:
        try:
            if os.path.samefile(str(current), str(base)):
                return True
        except OSError:
            if current == base:
                return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _prepare_workdir(value: Path, project_root: Path, work_mode: str = workspace_policy.ISOLATED) -> Path:
    """按工作方式校验目录；值班台根目录仅允许显式自维护模式使用。"""
    raw = Path(value)
    if not raw.is_absolute():
        raise RunnerConfigError("工作目录必须是绝对路径。")
    if any(ord(char) < 32 for char in str(raw)):
        raise RunnerConfigError("工作目录不能包含换行或控制字符。")
    try:
        workdir = raw.resolve()
    except (OSError, RuntimeError) as exc:
        raise RunnerConfigError(f"工作目录无法解析：{exc}") from exc

    if workdir == Path("/"):
        raise RunnerConfigError("工作目录不能是文件系统根目录 /。")
    try:
        workspace_policy.validate_target(workdir, project_root, work_mode)
    except ValueError as exc:
        raise RunnerConfigError(str(exc)) from exc
    if work_mode == workspace_policy.SELF:
        return project_root.resolve()
    collab = project_root / "协作"
    if _is_within(workdir, collab):
        raise RunnerConfigError("工作目录不能位于 协作/ 或其子目录。")
    if _is_within(workdir, project_root):
        projects = project_root / "项目"
        try:
            relative = workdir.relative_to(projects)
        except ValueError:
            relative = None
        if relative is None or len(relative.parts) != 2 or relative.parts[1] != "工作区":
            raise RunnerConfigError(
                "值班台内的工作目录只能是 项目/<编号>_<短名>/工作区/。"
            )

    if workdir.exists():
        if not workdir.is_dir():
            raise RunnerConfigError(f"工作目录已存在但不是目录：{workdir}")
        if not os.access(str(workdir), os.W_OK | os.X_OK):
            raise RunnerConfigError(f"工作目录已存在但不可写：{workdir}")
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RunnerConfigError(f"工作目录不存在且无法创建：{exc}") from exc
    if not workdir.is_dir():
        raise RunnerConfigError(f"工作目录不是目录：{workdir}")
    return workdir


class Runner:
    """一个仓库对应一个执行器实例；所有路径都相对 project_root。"""

    def __init__(
        self, project_root: Path, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self.project_root = Path(project_root).resolve()
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise RunnerConfigError("超时时间必须是正数秒。") from exc
        if timeout <= 0:
            raise RunnerConfigError("超时时间必须大于 0 秒。")
        self.timeout_seconds = timeout

        self.collab_dir = self.project_root / "协作"
        self.projects_dir = self.project_root / "项目"
        self.archive_dir = self.project_root / "归档"
        self.dispatch_dir = self.collab_dir / "调度台"
        self.status_path = self.dispatch_dir / "runner.status"
        self.lock_path = self.dispatch_dir / "runner.lock"
        self.history_path = self.dispatch_dir / "runner.history.jsonl"

        self._mutex = threading.RLock()
        self._result_ready = threading.Condition(self._mutex)
        self._results: Dict[str, Dict[str, Any]] = {}
        self._closed = False
        self._launch_mutex = threading.Lock()
        self._active: Optional[_ActiveRun] = None
        self._registration: Optional[_ActiveRun] = None
        self._idle = threading.Event()
        self._idle.set()
        self._registration_idle = threading.Event()
        self._registration_idle.set()

    # ------------------------------------------------------------------
    # 读状态与锁
    # ------------------------------------------------------------------

    def read_status(self) -> Dict[str, object]:
        if not self.status_path.is_file():
            return {}
        try:
            return _parse_mapping(self.status_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return {}

    def _read_lock_file(self) -> Tuple[Dict[str, object], Optional[os.stat_result]]:
        try:
            with self.lock_path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                raw = handle.read()
        except (FileNotFoundError, NotADirectoryError):
            return {}, None
        except OSError:
            return {}, None
        try:
            text = raw.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            text = ""
        return _parse_mapping(text), stat

    def lock_info(self) -> Dict[str, Any]:
        mapping, stat = self._read_lock_file()
        if stat is None:
            return {
                "active": False,
                "owner_pid": None,
                "agent_pid": None,
                "task": "",
                "agent": "",
                "started": "",
                "started_epoch": None,
                "path": self._display_path(self.lock_path),
            }
        owner_pid = _as_int(mapping.get("owner_pid"))
        agent_pid = _as_int(mapping.get("agent_pid"))
        active = _pid_alive(owner_pid) or _pid_alive(agent_pid)
        age = max(0.0, time.time() - stat.st_mtime)
        # 刚创建但尚未写入完整内容的锁也视为活锁，避免读写竞态下被误清。
        if not active and not owner_pid and not agent_pid and age < EMPTY_LOCK_GRACE_SECONDS:
            active = True
        return {
            "active": active,
            "owner_pid": owner_pid,
            "agent_pid": agent_pid,
            "task": str(mapping.get("task") or ""),
            "agent": str(mapping.get("agent") or ""),
            "started": str(mapping.get("started") or ""),
            "started_epoch": _as_float(mapping.get("started_epoch")),
            "token": str(mapping.get("token") or ""),
            "mode": str(mapping.get("mode") or ""),
            "path": self._display_path(self.lock_path),
        }

    def snapshot(self) -> Dict[str, Any]:
        """给页面/API 的一致视图：只有死锁被清理前，状态也绝不冒充运行中。"""
        status = self.read_status()
        lock = self.lock_info()
        now = time.time()
        running = bool(lock["active"])

        info: Dict[str, Any] = dict(status)
        if running:
            # Registration intentionally does not overwrite runner.status.
            # A different lock identity must not inherit the previous run's fields.
            if (str(info.get("task") or "") != str(lock.get("task") or "")
                    or _as_int(info.get("pid")) != lock.get("agent_pid")):
                info = {
                    "task": lock.get("task", ""),
                    "agent": lock.get("agent", ""),
                    "started": lock.get("started", ""),
                    "started_epoch": lock.get("started_epoch"),
                    "pid": lock.get("agent_pid") or lock.get("owner_pid"),
                    "owner_pid": lock.get("owner_pid"),
                    "kind": "registration" if lock.get("mode") == "registration" else "run",
                }
            info["state"] = "running"
            if not info.get("task"):
                info["task"] = lock.get("task", "")
            if not info.get("agent"):
                info["agent"] = lock.get("agent", "")
            if not info.get("started"):
                info["started"] = lock.get("started", "")
            if not info.get("started_epoch"):
                info["started_epoch"] = lock.get("started_epoch")
            if not info.get("pid"):
                info["pid"] = lock.get("agent_pid") or lock.get("owner_pid")
        else:
            state = str(info.get("state") or "idle")
            if state == "running":
                # 状态写着 running，但锁已消失：说明上次收尾不完整，绝不显示为在跑。
                state = "stale"
            info["state"] = state

        started_epoch = _as_float(info.get("started_epoch"))
        duration = _as_float(info.get("duration_seconds"))
        if running and started_epoch:
            duration = max(0.0, now - started_epoch)
        info["started_epoch"] = started_epoch
        info["duration_seconds"] = duration
        info["running"] = running
        info["lock_active"] = bool(lock["active"])
        info["lock"] = {
            key: lock[key]
            for key in (
                "active",
                "owner_pid",
                "agent_pid",
                "task",
                "agent",
                "started",
                "started_epoch",
                "path",
            )
        }
        info["log_path"] = self.resolve_log(info.get("log"))
        return info

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    def launch(
        self,
        *,
        task_id: str,
        task_dir: Path,
        agent: AgentSpec,
        round_no: int,
        workdir: Optional[Path] = None,
        prompt: Optional[str] = None,
        kind: str = "run",
        subtask_id: str = "",
        subtask_title: str = "",
        log_prefix: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        cwd: Optional[Path] = None,
        work_mode: str = workspace_policy.ISOLATED,
    ) -> Dict[str, Any]:
        """先抢锁并启动子进程；监视线程在后台收尾。

        kind=dispatch / review / handoff 时接受调用方提供的固定提示词，日志
        前缀可区分子任务、审核或交接补交；锁、cwd、stdin、超时与进程组收尾
        都与普通运行完全一致。

        cwd 仅供审核与交接补交使用：两者既要读 对接/ 又要读 工作区/，因此
        允许把进程 cwd 显式设为该任务的项目目录；其它运行仍固定为 workdir。
        """
        task_id = task_id.strip().upper()
        if not TASK_ID_RE.fullmatch(task_id):
            raise RunnerConfigError("任务编号格式不正确。")
        try:
            round_value = int(round_no)
        except (TypeError, ValueError) as exc:
            raise RunnerConfigError("交付轮次必须是正整数。") from exc
        if round_value < 1:
            raise RunnerConfigError("交付轮次必须是正整数。")
        if kind not in {"run", "dispatch", "review", "handoff", "whole_rework"}:
            raise RunnerConfigError("未知的执行类型。")
        if kind == "dispatch" and not str(subtask_id).strip():
            raise RunnerConfigError("分发子任务缺少编号。")
        if prompt is not None and not str(prompt).strip():
            raise RunnerConfigError("自定义提示词为空。")
        try:
            timeout = (
                self.timeout_seconds
                if timeout_seconds is None
                else float(timeout_seconds)
            )
        except (TypeError, ValueError) as exc:
            raise RunnerConfigError("超时时间必须是正数秒。") from exc
        if timeout <= 0:
            raise RunnerConfigError("超时时间必须大于 0 秒。")

        raw_task_path = Path(task_dir)
        if raw_task_path.is_symlink():
            raise RunnerConfigError("任务目录是符号链接，拒绝执行。")
        task_path = raw_task_path.resolve()
        project_root = self.projects_dir.resolve()
        try:
            task_path.relative_to(project_root)
        except ValueError as exc:
            raise RunnerConfigError("任务目录不在本仓库 项目/ 下，拒绝执行。") from exc
        if task_path.parent != project_root:
            raise RunnerConfigError("任务目录不是 项目/ 的直接子目录，拒绝执行。")
        if not re.match(
            rf"^{re.escape(task_id)}(?:_|$)", task_path.name, flags=re.IGNORECASE
        ):
            raise RunnerConfigError("任务目录名与任务编号不一致，拒绝执行。")
        if not task_path.is_dir():
            raise RunnerConfigError("任务项目目录不存在。")
        if not (task_path / "对接" / "任务单.md").is_file():
            raise RunnerConfigError("任务项目缺少 对接/任务单.md，拒绝执行。")

        # 普通运行/分发的 cwd 是任务自己的 workdir；审核可显式使用项目目录，
        # 以便同一个 Agent 同时读取 对接/ 与 工作区/。
        workdir_path = _prepare_workdir(
            Path(workdir) if workdir is not None else task_path / "工作区",
            self.project_root,
            work_mode=work_mode,
        )
        cwd_path = workdir_path
        if cwd is not None:
            if kind not in {"review", "handoff"}:
                raise RunnerConfigError("只有审核或交接补交运行可以覆盖 cwd。")
            raw_cwd = Path(cwd)
            if raw_cwd.is_symlink():
                raise RunnerConfigError("运行 cwd 是符号链接，拒绝执行。")
            cwd_path = raw_cwd.resolve()
            if cwd_path != task_path:
                raise RunnerConfigError("运行 cwd 必须是该任务的项目目录。")
            if not cwd_path.is_dir():
                raise RunnerConfigError("审核 cwd 目录不存在。")

        prompt_text = (
            str(prompt)
            if prompt is not None
            else build_prompt(
                task_id,
                round_value,
                desk_root=self.project_root,
                task_dir=task_path,
                workdir=workdir_path,
                work_mode=work_mode,
            )
        )
        argv = build_argv(agent, prompt_text)

        with self._launch_mutex:
            if self._closed:
                raise RunnerError("执行器正在退出，不能再启动任务。")
            if self._active is not None or self._registration is not None:
                raise RunnerBusy("已有任务在跑", self.snapshot())
            started_epoch = time.time()
            started = _human_time(started_epoch)
            token = uuid.uuid4().hex
            self._acquire_lock(
                {
                    "pid": os.getpid(),
                    "owner_pid": os.getpid(),
                    "task": task_id,
                    "agent": agent.id,
                    "started": started,
                    "started_epoch": started_epoch,
                    "token": token,
                }
            )

            log_handle: Optional[BinaryIO] = None
            log_path: Optional[Path] = None
            process: Optional[subprocess.Popen] = None
            try:
                prefix = (log_prefix or ("run" if kind == "run" else "dispatch")).strip()
                prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix)[:80] or "run"
                log_handle, log_path = self._open_log(task_path, prefix=prefix)
                run_id = log_path.stem
                header = (
                    "=== 协作执行器 ===\n"
                    f"run_id: {run_id}\n"
                    f"task: {task_id}\n"
                    f"agent: {agent.id} · {agent.name}\n"
                    f"kind: {kind}\n"
                    + (
                        f"subtask: {subtask_id}"
                        + (f" · {subtask_title}" if subtask_title else "")
                        + "\n"
                        if kind == "dispatch"
                        else ""
                    )
                    + f"round: r{round_value}\n"
                    f"started: {started}\n"
                    f"timeout: {timeout:g}s\n"
                    f"command: {argv[0]}\n"
                    f"task_dir: {task_path}\n"
                    f"desk: {self.project_root}\n"
                    f"workdir: {workdir_path}\n"
                    f"cwd: {cwd_path}\n"
                    f"log: {self._display_path(log_path)}\n"
                    "stdin: /dev/null\n"
                    "--- prompt ---\n"
                    f"{prompt_text}\n"
                    "--- output ---\n"
                )
                self._write_log(log_handle, header)

                # stdin 固定为空，等价于在 shell 命令末尾追加 </dev/null。
                process = subprocess.Popen(
                    argv,
                    cwd=str(cwd_path),
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                    env=os.environ.copy(),
                )
            except (OSError, RunnerError) as exc:
                self._write_log(log_handle, f"[执行器] 启动失败：{exc}\n") if log_handle else None
                self._write_log(
                    log_handle,
                    "=== 执行器结束 ===\n"
                    "state: failed\n"
                    "exit: spawn-error\n"
                    f"error: {exc}\n",
                ) if log_handle else None
                if log_handle is not None:
                    try:
                        log_handle.close()
                    except OSError:
                        pass
                if process is not None and process.poll() is None:
                    self._terminate(process)
                self._finish_record_best_effort(
                    token=token,
                    task_id=task_id,
                    task_dir=task_path,
                    agent=agent,
                    run_id=log_path.stem if log_path else "",
                    started_epoch=started_epoch,
                    log_path=log_path,
                    round_no=round_value,
                    pid=process.pid if process else "",
                    state="failed",
                    exit_value="spawn-error",
                    error=str(exc),
                    timeout_seconds=timeout,
                    kind=kind,
                    subtask_id=subtask_id,
                    subtask_title=subtask_title,
                )
                raise RunnerError(f"执行器未能启动 Agent：{exc}") from exc

            assert log_handle is not None and log_path is not None
            self._append_lock_agent_pid(process.pid)
            running_status = self._status_payload(
                state="running",
                task_id=task_id,
                task_dir=task_path,
                agent=agent,
                run_id=log_path.stem,
                started_epoch=started_epoch,
                log_path=log_path,
                round_no=round_value,
                pid=process.pid,
                exit_value="",
                timeout_seconds=timeout,
                kind=kind,
                subtask_id=subtask_id,
                subtask_title=subtask_title,
            )
            try:
                self._write_status(running_status)
            except RunnerError:
                # 状态写不进去时不留下无人监视的进程。
                self._terminate(process)
                self._release_lock(token)
                try:
                    log_handle.close()
                except OSError:
                    pass
                raise

            active = _ActiveRun(
                run_id=log_path.stem,
                process=process,
                log_path=log_path,
                log_handle=log_handle,
                lock_token=token,
                task_id=task_id,
            )
            with self._mutex:
                self._active = active
                self._idle.clear()
            monitor = threading.Thread(
                target=self._monitor,
                args=(
                    active,
                    task_id,
                    task_path,
                    agent,
                    round_value,
                    started_epoch,
                    timeout,
                    kind,
                    subtask_id,
                    subtask_title,
                ),
                name=f"runner-{log_path.stem}",
                daemon=True,
            )
            try:
                monitor.start()
            except RuntimeError as exc:
                self._terminate(process)
                self._finish_record_best_effort(
                    token=token,
                    task_id=task_id,
                    task_dir=task_path,
                    agent=agent,
                    run_id=log_path.stem,
                    started_epoch=started_epoch,
                    log_path=log_path,
                    round_no=round_value,
                    pid=process.pid,
                    state="failed",
                    exit_value="thread-error",
                    error=str(exc),
                )
                with self._mutex:
                    self._active = None
                    self._idle.set()
                raise RunnerError(f"执行器监视线程无法启动：{exc}") from exc
            return dict(running_status, running=True)

    # ------------------------------------------------------------------
    # 总控登记：与普通派发共用同一把全局锁，但使用独立日志、不写 /runs 历史
    # ------------------------------------------------------------------

    def launch_registration(
        self,
        *,
        task_id: str,
        task_dir: Path,
        agent: AgentSpec,
        prompt: str,
        timeout_seconds: Optional[float] = None,
        on_finish: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """启动一次总控登记调用。

        cwd 固定为任务项目目录；stdin 为空；沿用 runner.lock 单实例锁。
        登记过程不写 runner.status / runner.history，避免和任务执行混淆；
        结束回调由 server 用来把结果写回 task.yaml。
        """
        task_id = task_id.strip().upper()
        if not TASK_ID_RE.fullmatch(task_id):
            raise RunnerConfigError("任务编号格式不正确。")
        if not prompt.strip():
            raise RunnerConfigError("登记提示词为空。")
        try:
            timeout = (
                self.timeout_seconds
                if timeout_seconds is None
                else float(timeout_seconds)
            )
        except (TypeError, ValueError) as exc:
            raise RunnerConfigError("登记超时必须是正数秒。") from exc
        if timeout <= 0:
            raise RunnerConfigError("登记超时必须大于 0 秒。")

        raw_task_path = Path(task_dir)
        if raw_task_path.is_symlink():
            raise RunnerConfigError("任务目录是符号链接，拒绝执行。")
        task_path = raw_task_path.resolve()
        project_root = self.projects_dir.resolve()
        try:
            task_path.relative_to(project_root)
        except ValueError as exc:
            raise RunnerConfigError("任务目录不在本仓库 项目/ 下，拒绝执行。") from exc
        if task_path.parent != project_root:
            raise RunnerConfigError("任务目录不是 项目/ 的直接子目录，拒绝执行。")
        if not re.match(
            rf"^{re.escape(task_id)}(?:_|$)", task_path.name, flags=re.IGNORECASE
        ):
            raise RunnerConfigError("任务目录名与任务编号不一致，拒绝执行。")
        if not task_path.is_dir():
            raise RunnerConfigError("任务项目目录不存在。")
        if not (task_path / "对接" / "idea.md").is_file():
            raise RunnerConfigError("任务项目缺少 对接/idea.md，无法启动总控登记。")

        argv = build_argv(agent, prompt)

        with self._launch_mutex:
            if self._closed:
                raise RunnerError("执行器正在退出，不能再启动登记。")
            if self._active is not None or self._registration is not None:
                raise RunnerBusy("已有任务在跑", self.snapshot())
            started_epoch = time.time()
            started = _human_time(started_epoch)
            token = uuid.uuid4().hex
            self._acquire_lock(
                {
                    "pid": os.getpid(),
                    "owner_pid": os.getpid(),
                    "task": task_id,
                    "agent": agent.id,
                    "mode": "registration",
                    "started": started,
                    "started_epoch": started_epoch,
                    "token": token,
                }
            )

            log_handle: Optional[BinaryIO] = None
            log_path: Optional[Path] = None
            process: Optional[subprocess.Popen] = None
            try:
                log_handle, log_path = self._open_log(task_path, prefix="register")
                run_id = log_path.stem
                header = (
                    "=== 协作执行器 · 总控登记 ===\n"
                    f"run_id: {run_id}\n"
                    f"task: {task_id}\n"
                    f"agent: {agent.id} · {agent.name}\n"
                    f"mode: registration\n"
                    f"started: {started}\n"
                    f"timeout: {timeout:g}s\n"
                    f"command: {argv[0]}\n"
                    f"task_dir: {task_path}\n"
                    f"desk: {self.project_root}\n"
                    f"cwd: {task_path}\n"
                    f"log: {self._display_path(log_path)}\n"
                    "stdin: /dev/null\n"
                    "--- prompt ---\n"
                    f"{prompt}\n"
                    "--- output ---\n"
                )
                self._write_log(log_handle, header)
                process = subprocess.Popen(
                    argv,
                    cwd=str(task_path),
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                    env=os.environ.copy(),
                )
            except (OSError, RunnerError) as exc:
                if log_handle is not None:
                    self._write_log(log_handle, f"[执行器] 总控登记启动失败：{exc}\n")
                    self._write_log(
                        log_handle,
                        "=== 执行器结束 ===\nstate: failed\nexit: spawn-error\n"
                        f"error: {exc}\n",
                    )
                    try:
                        log_handle.close()
                    except OSError:
                        pass
                if process is not None and process.poll() is None:
                    self._terminate(process)
                self._release_lock(token)
                raise RunnerError(f"执行器未能启动总控：{exc}") from exc

            assert log_handle is not None and log_path is not None
            self._append_lock_agent_pid(process.pid)
            active = _ActiveRun(
                run_id=log_path.stem,
                process=process,
                log_path=log_path,
                log_handle=log_handle,
                lock_token=token,
                task_id=task_id,
            )
            with self._mutex:
                self._registration = active
                self._registration_idle.clear()
            monitor = threading.Thread(
                target=self._registration_monitor,
                args=(
                    active,
                    agent,
                    started_epoch,
                    timeout,
                    on_finish,
                ),
                name=f"registration-{log_path.stem}",
                daemon=True,
            )
            try:
                monitor.start()
            except RuntimeError as exc:
                self._terminate(process)
                try:
                    log_handle.close()
                except OSError:
                    pass
                self._release_lock(token)
                with self._mutex:
                    self._registration = None
                    self._registration_idle.set()
                raise RunnerError(f"总控登记监视线程无法启动：{exc}") from exc

            return {
                "ok": True,
                "task": task_id,
                "agent": agent.id,
                "agent_name": agent.name,
                "run_id": log_path.stem,
                "pid": process.pid,
                "started": started,
                "started_epoch": started_epoch,
                "timeout_seconds": timeout,
                "log": self._display_path(log_path),
            }

    def _registration_monitor(
        self,
        active: _ActiveRun,
        agent: AgentSpec,
        started_epoch: float,
        timeout: float,
        on_finish: Optional[Callable[[Dict[str, Any]], None]],
    ) -> None:
        state = "failed"
        exit_value: object = "error"
        error = ""
        timed_out = False
        try:
            try:
                return_code = active.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(active.process)
                return_code = active.process.returncode
            if active.stop_reason:
                state = "failed"
                exit_value = return_code if return_code is not None else "stopped"
                error = active.stop_reason
            elif timed_out:
                state = "timeout"
                exit_value = "timeout"
                error = f"总控登记超过 {timeout:g} 秒，进程组已终止"
            elif return_code == 0:
                state = "success"
                exit_value = 0
            else:
                state = "failed"
                exit_value = return_code
                error = "总控进程以非零状态退出"
        except Exception as exc:
            error = f"监视总控登记时出错：{exc}"
            try:
                self._terminate(active.process)
            except Exception:
                pass
        finally:
            residual_cleaned = False
            try:
                residual_cleaned = self._drain_process_group(active.process.pid)
            except Exception:
                pass
            finished_epoch = time.time()
            try:
                self._write_log(
                    active.log_handle,
                    "=== 执行器结束 ===\n"
                    f"state: {state}\n"
                    f"exit: {exit_value}\n"
                    f"finished: {_human_time(finished_epoch)}\n"
                    f"duration: {finished_epoch - started_epoch:.2f}s\n"
                    + (
                        "cleanup: killed residual process group\n"
                        if residual_cleaned
                        else ""
                    )
                    + (f"error: {error}\n" if error else ""),
                )
            except OSError:
                pass

            result: Dict[str, Any] = {
                "state": state,
                "exit": exit_value,
                "timed_out": timed_out,
                "error": error,
                "owner_pid": os.getpid(),
                "agent": agent.id,
                "agent_name": agent.name,
                "run_id": active.run_id,
                "started_epoch": round(started_epoch, 3),
                "finished_epoch": round(finished_epoch, 3),
                "duration_seconds": round(finished_epoch - started_epoch, 3),
                "log": self._display_path(active.log_path),
                "log_path": active.log_path,
            }
            if on_finish is not None:
                try:
                    on_finish(result)
                except Exception:
                    import traceback

                    traceback.print_exc()
            try:
                active.log_handle.close()
            except OSError:
                pass
            # 回调先完成再放锁：task.yaml 的终态写完后，其他写手才能进入。
            self._release_lock(active.lock_token)
            with self._mutex:
                if self._registration is active:
                    self._registration = None
                self._registration_idle.set()

    def wait_for_registration_idle(self, timeout: Optional[float] = None) -> bool:
        return self._registration_idle.wait(timeout)

    def _monitor(
        self,
        active: _ActiveRun,
        task_id: str,
        task_dir: Path,
        agent: AgentSpec,
        round_no: int,
        started_epoch: float,
        timeout: float,
        kind: str = "run",
        subtask_id: str = "",
        subtask_title: str = "",
    ) -> None:
        state = "failed"
        exit_value: object = "error"
        error = ""
        timed_out = False
        try:
            try:
                return_code = active.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(active.process)
                return_code = active.process.returncode
            if active.stop_reason:
                state = "failed"
                exit_value = return_code if return_code is not None else "stopped"
                error = active.stop_reason
            elif timed_out:
                state = "timeout"
                exit_value = "timeout"
                error = f"执行超过 {timeout:g} 秒，进程组已终止"
            elif return_code == 0:
                state = "success"
                exit_value = 0
            else:
                state = "failed"
                exit_value = return_code
                error = "Agent 进程以非零状态退出"
        except Exception as exc:  # 收尾路径也要尽力释放锁
            error = f"监视进程时出错：{exc}"
            try:
                self._terminate(active.process)
            except Exception:
                pass
        finally:
            residual_cleaned = False
            try:
                # 主 CLI 正常退出时，它可能留下了后台子进程；必须先清空整个
                # 会话进程组，才允许释放全局锁并开始下一次派发。
                residual_cleaned = self._drain_process_group(active.process.pid)
            except Exception:
                pass
            finished_epoch = time.time()
            try:
                self._write_log(
                    active.log_handle,
                    "=== 执行器结束 ===\n"
                    f"state: {state}\n"
                    f"exit: {exit_value}\n"
                    f"finished: {_human_time(finished_epoch)}\n"
                    f"duration: {finished_epoch - started_epoch:.2f}s\n"
                    + (
                        "cleanup: killed residual process group\n"
                        if residual_cleaned
                        else ""
                    )
                    + (f"error: {error}\n" if error else ""),
                )
            except OSError:
                pass

            payload = self._status_payload(
                state=state,
                task_id=task_id,
                task_dir=task_dir,
                agent=agent,
                run_id=active.run_id,
                started_epoch=started_epoch,
                log_path=active.log_path,
                round_no=round_no,
                pid=active.process.pid,
                exit_value=exit_value,
                timeout_seconds=timeout,
                finished_epoch=finished_epoch,
                error=error,
                kind=kind,
                subtask_id=subtask_id,
                subtask_title=subtask_title,
            )
            try:
                self._write_status(payload)
            except RunnerError:
                pass
            self._append_history(payload)
            try:
                active.log_handle.close()
            except OSError:
                pass
            self._release_lock(active.lock_token)
            with self._mutex:
                if self._active is active:
                    self._active = None
                self._remember_result(payload)
                self._idle.set()

    def _remember_result(self, payload: Dict[str, Any]) -> None:
        """Publish an immutable result only after process/lock cleanup is complete."""
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            return
        with self._result_ready:
            self._results[run_id] = dict(payload)
            while len(self._results) > 256:
                self._results.pop(next(iter(self._results)))
            self._result_ready.notify_all()

    def wait_for_result(self, run_id: str, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Wait for this instance's specific run, never the global latest status."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._result_ready:
            while run_id not in self._results:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._result_ready.wait(remaining)
            return dict(self._results[run_id])

    # ------------------------------------------------------------------
    # 历史与日志
    # ------------------------------------------------------------------

    def history(self, limit: int = 10) -> List[Dict[str, Any]]:
        if limit <= 0 or not self.history_path.is_file():
            return []
        records: List[Dict[str, Any]] = []
        try:
            with self.history_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        records.append(record)
        except OSError:
            return []

        def sort_key(record: Dict[str, Any]) -> Tuple[float, str]:
            finished = _as_float(record.get("finished_epoch")) or 0.0
            return finished, str(record.get("run_id") or "")

        records.sort(key=sort_key, reverse=True)
        seen = set()
        selected: List[Dict[str, Any]] = []
        for record in records:
            run_id = str(record.get("run_id") or "")
            key = run_id or f"{record.get('task')}-{record.get('started')}-{record.get('pid')}"
            if key in seen:
                continue
            seen.add(key)
            selected.append(record)
            if len(selected) >= limit:
                break
        return selected

    def history_count(self) -> int:
        if not self.history_path.is_file():
            return 0
        count = 0
        try:
            with self.history_path.open("rb") as handle:
                for _line in handle:
                    count += 1
        except OSError:
            return 0
        return count

    def resolve_log(self, log_value: object) -> Optional[Path]:
        """解析仓库相对日志路径；项目归档后自动回落到 归档/ 下的同名路径。"""
        if log_value is None:
            return None
        raw = str(log_value).strip()
        if not raw:
            return None
        candidate = Path(raw)
        if candidate.is_absolute():
            try:
                candidate.resolve().relative_to(self.project_root)
            except (OSError, ValueError):
                return None
            return candidate if candidate.is_file() else None

        def allowed(path: Path) -> bool:
            try:
                path.resolve().relative_to(self.project_root)
            except (OSError, ValueError):
                return False
            return path.is_file()

        direct = self.project_root / candidate
        if allowed(direct):
            return direct
        parts = candidate.parts
        if len(parts) >= 2 and parts[0] == "项目":
            archived = self.archive_dir.joinpath(*parts[1:])
            if allowed(archived):
                return archived
        return None

    def tail_log(self, value: object, count: int = MAX_LOG_TAIL_LINES) -> List[str]:
        if isinstance(value, dict):
            value = value.get("log")
        path = value if isinstance(value, Path) else self.resolve_log(value)
        if path is None:
            return []
        return _tail_lines(path, count)

    def _open_log(
        self, task_dir: Path, *, prefix: str = "run"
    ) -> Tuple[BinaryIO, Path]:
        log_dir = task_dir / "工作区" / "日志"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunnerError(f"无法创建日志目录：{exc}") from exc
        stamp = _now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex
        sequence = 1
        while True:
            suffix = "" if sequence == 1 else f"-{sequence}"
            path = log_dir / f"{prefix}-{stamp}{suffix}.log"
            try:
                handle = path.open("xb", buffering=0)
                return handle, path
            except FileExistsError:
                sequence += 1
                if sequence > 999:
                    raise RunnerError("无法分配唯一的运行日志名。")
            except OSError as exc:
                raise RunnerError(f"无法创建运行日志：{exc}") from exc

    @staticmethod
    def _write_log(handle: Optional[BinaryIO], text: str) -> None:
        if handle is None:
            return
        handle.write(text.encode("utf-8", errors="replace"))

    # ------------------------------------------------------------------
    # 锁
    # ------------------------------------------------------------------

    def _acquire_lock(self, values: Dict[str, object]) -> None:
        try:
            with file_guard(self.dispatch_dir / ".runner.guard.lock"):
                self._acquire_lock_guarded(values)
        except TimeoutError as exc:
            raise RunnerBusy(str(exc)) from exc
        except OSError as exc:
            raise RunnerError(f"无法取得执行器事务锁：{exc}") from exc

    def _acquire_lock_guarded(self, values: Dict[str, object]) -> None:
        try:
            self.dispatch_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunnerError(f"无法创建调度台目录：{exc}") from exc

        for _attempt in range(8):
            try:
                descriptor = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                mapping, stat = self._read_lock_file()
                if stat is None:
                    continue
                owner_pid = _as_int(mapping.get("owner_pid"))
                agent_pid = _as_int(mapping.get("agent_pid"))
                active = _pid_alive(owner_pid) or _pid_alive(agent_pid)
                age = max(0.0, time.time() - stat.st_mtime)
                if (
                    not active
                    and not owner_pid
                    and not agent_pid
                    and age < EMPTY_LOCK_GRACE_SECONDS
                ):
                    active = True
                if active:
                    raise RunnerBusy("已有任务在跑", self.snapshot())
                # 清理明显失效的锁；仅当磁盘上的仍是刚才读到的那个 inode。
                try:
                    current = self.lock_path.stat()
                except OSError:
                    continue
                if (current.st_dev, current.st_ino) != (stat.st_dev, stat.st_ino):
                    continue
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise RunnerError(f"无法清理失效的执行器锁：{exc}") from exc
                continue
            except OSError as exc:
                raise RunnerError(f"无法创建执行器锁：{exc}") from exc

            content = _render_mapping(values).encode("utf-8")
            try:
                os.write(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return
        raise RunnerBusy("已有任务在跑", self.snapshot())

    def _append_lock_agent_pid(self, pid: int) -> None:
        try:
            with self.lock_path.open("a", encoding="utf-8") as handle:
                handle.write(f"agent_pid: {int(pid)}\n")
        except OSError:
            # owner_pid 仍能阻止并发；这里只是让监视信息更完整。
            pass

    def _release_lock(self, token: str) -> None:
        try:
            with file_guard(self.dispatch_dir / ".runner.guard.lock"):
                self._release_lock_guarded(token)
        except (OSError, TimeoutError):
            # The owning process/agent identity still protects the unreleased lock.
            pass

    def _release_lock_guarded(self, token: str) -> None:
        try:
            mapping, stat = self._read_lock_file()
        except OSError:
            return
        if stat is None or str(mapping.get("token") or "") != token:
            return
        try:
            current = self.lock_path.stat()
        except OSError:
            return
        if (current.st_dev, current.st_ino) != (stat.st_dev, stat.st_ino):
            return
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def reconcile(self) -> Optional[Dict[str, Any]]:
        """服务启动时收尾崩溃遗留状态；先抢锁，绝不误改仍在运行的任务。"""
        status = self.read_status()
        if str(status.get("state") or "") != "running":
            return None

        token = uuid.uuid4().hex
        started_epoch = _as_float(status.get("started_epoch")) or time.time()
        try:
            self._acquire_lock(
                {
                    "pid": os.getpid(),
                    "owner_pid": os.getpid(),
                    "task": str(status.get("task") or ""),
                    "agent": "reconcile",
                    "started": _human_time(time.time()),
                    "started_epoch": time.time(),
                    "token": token,
                }
            )
        except RunnerError:
            # 已有活锁，或此刻有人在启动；留给活着的执行器收尾。
            return None

        try:
            status = self.read_status()
            if str(status.get("state") or "") != "running":
                return None
            run_id = str(status.get("run_id") or "")
            finished_epoch = time.time()
            log_value = status.get("log")
            status["state"] = "failed"
            status["exit"] = "lost"
            status["finished"] = _human_time(finished_epoch)
            status["finished_epoch"] = round(finished_epoch, 3)
            status["duration_seconds"] = round(
                max(0.0, finished_epoch - started_epoch), 3
            )
            status["error"] = "上次执行器未正常收尾；失效锁已由本次启动回收"
            try:
                self._write_status(status)
            except RunnerError:
                pass

            already = any(
                item.get("run_id") == run_id and run_id
                for item in self.history(limit=10000)
            )
            if not already:
                history_record = dict(status)
                if log_value is not None:
                    history_record["log"] = str(log_value)
                self._append_history(history_record)
            return status
        finally:
            self._release_lock(token)

    # ------------------------------------------------------------------
    # 状态与历史落盘
    # ------------------------------------------------------------------

    def _status_payload(
        self,
        *,
        state: str,
        task_id: str,
        task_dir: Path,
        agent: AgentSpec,
        run_id: str,
        started_epoch: float,
        log_path: Optional[Path],
        round_no: int,
        pid: object,
        exit_value: object,
        timeout_seconds: float,
        finished_epoch: Optional[float] = None,
        error: str = "",
        kind: str = "run",
        subtask_id: str = "",
        subtask_title: str = "",
    ) -> Dict[str, Any]:
        started = _human_time(started_epoch)
        payload: Dict[str, Any] = {
            "state": state,
            "task": task_id,
            "agent": agent.id,
            "agent_name": agent.name,
            "kind": kind,
            "pid": pid,
            "owner_pid": os.getpid(),
            "run_id": run_id,
            "round": round_no,
            "started": started,
            "started_epoch": round(started_epoch, 3),
            "finished": _human_time(finished_epoch) if finished_epoch else "",
            "finished_epoch": round(finished_epoch, 3) if finished_epoch else "",
            "duration_seconds": (
                round(max(0.0, finished_epoch - started_epoch), 3)
                if finished_epoch
                else ""
            ),
            "timeout_seconds": timeout_seconds if timeout_seconds % 1 else int(timeout_seconds),
            "exit": exit_value,
            "log": self._display_path(log_path) if log_path else "",
        }
        if kind == "dispatch" and subtask_id:
            payload["subtask"] = subtask_id
            if subtask_title:
                payload["subtask_title"] = subtask_title
        if error:
            payload["error"] = error
        return payload

    def _write_status(self, payload: Dict[str, object]) -> None:
        try:
            self.dispatch_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunnerError(f"无法创建调度台目录：{exc}") from exc
        text = _render_mapping(payload)
        temporary = self.dispatch_dir / (
            f".runner.status.tmp-{os.getpid()}-{threading.get_ident()}"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(self.status_path))
        except OSError as exc:
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
            raise RunnerError(f"无法写入 runner.status：{exc}") from exc

    def _append_history(self, payload: Dict[str, Any]) -> None:
        if not payload.get("run_id"):
            return
        try:
            self.dispatch_dir.mkdir(parents=True, exist_ok=True)
            data = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
            descriptor = os.open(
                str(self.history_path),
                os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, data.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass

    def _finish_record_best_effort(
        self,
        *,
        token: str,
        task_id: str,
        task_dir: Path,
        agent: AgentSpec,
        run_id: str,
        started_epoch: float,
        log_path: Optional[Path],
        round_no: int,
        pid: object,
        state: str,
        exit_value: object,
        error: str,
        timeout_seconds: Optional[float] = None,
        kind: str = "run",
        subtask_id: str = "",
        subtask_title: str = "",
    ) -> None:
        payload = self._status_payload(
            state=state,
            task_id=task_id,
            task_dir=task_dir,
            agent=agent,
            run_id=run_id,
            started_epoch=started_epoch,
            log_path=log_path,
            round_no=round_no,
            pid=pid,
            exit_value=exit_value,
            timeout_seconds=(
                self.timeout_seconds if timeout_seconds is None else timeout_seconds
            ),
            finished_epoch=time.time(),
            error=error,
            kind=kind,
            subtask_id=subtask_id,
            subtask_title=subtask_title,
        )
        try:
            self._write_status(payload)
        except RunnerError:
            pass
        if run_id:
            self._append_history(payload)
        self._release_lock(token)

    def _display_path(self, path: Optional[Path]) -> str:
        if path is None:
            return ""
        try:
            return Path(path).resolve().relative_to(self.project_root).as_posix()
        except (OSError, ValueError):
            return str(path)

    # ------------------------------------------------------------------
    # 进程与收尾
    # ------------------------------------------------------------------

    @staticmethod
    def _process_group_alive(pgid: int) -> bool:
        if pgid <= 1:
            return False
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @classmethod
    def _drain_process_group(cls, pgid: int) -> bool:
        """主进程退出后清掉其会话进程组内的残留成员。

        返回 True 表示确有残留并被清理过。PGID 由 start_new_session 建立；
        只要仍有成员存在就不会被系统复用，因此在收尾阶段定向清理是安全的。
        """
        if pgid <= 1 or pgid == os.getpgrp():
            return False
        if not cls._process_group_alive(pgid):
            return False
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        except PermissionError:
            return False
        deadline = time.monotonic() + RESIDUAL_GROUP_GRACE_SECONDS
        while cls._process_group_alive(pgid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if cls._process_group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            kill_deadline = time.monotonic() + RESIDUAL_GROUP_GRACE_SECONDS
            while cls._process_group_alive(pgid) and time.monotonic() < kill_deadline:
                time.sleep(0.05)
        return True

    @classmethod
    def _terminate(cls, process: subprocess.Popen) -> None:
        pgid = process.pid
        if process.poll() is None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    process.terminate()
                except OSError:
                    pass
            try:
                process.wait(timeout=TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        process.kill()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=TERMINATE_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
        # 即使主进程已经退出，也要排空它遗留的后台子进程。
        cls._drain_process_group(pgid)

    def wait_for_idle(self, timeout: Optional[float] = None) -> bool:
        """测试/收尾用：等待当前实例的监视线程结束。"""
        return self._idle.wait(timeout)

    def stop_active(self, reason: str, *, task_id: str = "") -> bool:
        """终止当前受控运行并等待锁释放；用于用户停止推进。

        只停止本执行器实例持有的运行。task_id 非空时，任务不匹配则不动。
        监视线程会记录 stop_reason、写终态并释放 runner.lock；超时后兜底
        释放同一 token 的锁，避免留下半锁。
        """
        with self._mutex:
            active = self._active
            registration = self._registration
        target = active or registration
        if target is None:
            return False
        if (
            task_id
            and target.task_id
            and target.task_id.upper() != task_id.strip().upper()
        ):
            return False

        target.stop_reason = reason or "用户请求停止"
        self._terminate(target.process)
        if target is registration:
            self.wait_for_registration_idle(TERMINATE_GRACE_SECONDS * 2 + 2.0)
        else:
            self.wait_for_idle(TERMINATE_GRACE_SECONDS * 2 + 2.0)
        # 监视线程通常已释放；这里兜底清理同一 token 的锁。
        self.release_stale_lock_for_task(target.task_id or task_id)
        return True

    def release_stale_lock_for_task(self, task_id: str) -> bool:
        """仅当锁属于本进程、指定任务且 Agent 进程已死时释放。"""
        mapping, stat = self._read_lock_file()
        if stat is None:
            return False
        if (
            task_id
            and str(mapping.get("task") or "").upper() != task_id.strip().upper()
        ):
            return False
        if _as_int(mapping.get("owner_pid")) != os.getpid():
            return False
        agent_pid = _as_int(mapping.get("agent_pid"))
        if _pid_alive(agent_pid):
            return False
        # 锁刚创建、尚未写入 agent_pid 时是启动中的活锁，不能误清。
        if not agent_pid and time.time() - stat.st_mtime < EMPTY_LOCK_GRACE_SECONDS:
            return False
        token = str(mapping.get("token") or "")
        if not token:
            return False
        self._release_lock(token)
        try:
            return not self.lock_path.exists()
        except OSError:
            return False

    def shutdown(self) -> None:
        """服务正常退出时终止当前执行组，让锁与状态有明确收尾。"""
        with self._launch_mutex:
            self._closed = True
        with self._mutex:
            active = self._active
            registration = self._registration
        if registration is not None:
            registration.stop_reason = "控制台退出，执行器终止了总控登记进程组"
            self._terminate(registration.process)
            self.wait_for_registration_idle(TERMINATE_GRACE_SECONDS * 2 + 2.0)
        if active is None:
            return
        active.stop_reason = "控制台退出，执行器终止了 Agent 进程组"
        self._terminate(active.process)
        self.wait_for_idle(TERMINATE_GRACE_SECONDS * 2 + 2.0)


def _status_cli(root: Path) -> int:
    engine = Runner(root)
    snapshot = engine.snapshot()
    print(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="协作执行器（只提供状态查看）")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("status",),
        default="status",
        help="status：输出当前 runner.status / 锁状态 JSON",
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="仓库根目录",
    )
    args = parser.parse_args(argv)
    return _status_cli(Path(args.root))


if __name__ == "__main__":
    raise SystemExit(main())
