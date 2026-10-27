#!/usr/bin/env python3
"""协作值班台：一个只依赖 Python 3 标准库的本地控制台。

默认只监听 127.0.0.1:8787；测试可用 --port 8789 起临时实例，仍只绑定回环地址。
任务以独立项目目录组织，主要读取：
  · 项目/{TNN}_{slug}/对接/**（任务单、交付、审查及全部对接文件）
  · 项目/{TNN}_{slug}/工作区/**（产物文件列表）
  · 归档/{TNN}_{slug}/**（已归档项目的只读详情）
  · 角色与 Agent 配置（协作/roles/*.yaml、协作/agents/*.yaml）
  · 协作/调度台/guard.status（菜单栏应用写入的守卫状态）
  · 协作/调度台/runner.status 与 runner.history.jsonl（执行器状态）
写入范围仅限：
  · 项目/{TNN}_{slug}/对接/任务单.md（新建任务；自动建项目与对接目录）
  · 项目/ ↔ 归档/ 的项目目录整体移动（归档/恢复；不改写文件）
  · 协作/roles/**（默认角色、角色编辑与保留文件的角色移出）
  · 协作/agents/**（手动登记 Agent；仅探测 --version）
  · 用户从任务详情发起执行时，由 console/runner.py 在任务的工作区追加日志，
    并维护 协作/调度台/runner.status、runner.history.jsonl、runner.lock
执行器本身绝不写入 对接/交付/ 或 对接/审查.md；它把固定提示词交给 Agent，
由 Agent 在项目边界内写交付与工作区。守卫仍只由 macOS 菜单栏应用管理。
控制台不删除用户文件；内部状态文件采用写后替换，锁在收尾时释放。

启动：
  python3 console/server.py
  python3 console/server.py --port 8789
  python3 console/server.py --run-timeout 3600
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass

try:  # 仅用于跨进程的任务生命周期锁；不可用时退化为进程内锁。
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows 无 fcntl
    fcntl = None  # type: ignore
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlsplit

try:  # 包导入（python3 -c "import console.server"）与脚本运行都可用
    from . import runner as runner_engine
except ImportError:  # pragma: no cover - 直接运行 console/server.py 时走这里
    import runner as runner_engine

# ---------------------------------------------------------------------------
# 路径固定；命令行只覆盖测试端口与执行器超时，服务始终只绑定 127.0.0.1
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
COLLAB_DIR = PROJECT_DIR / "协作"
PROJECTS_DIR = PROJECT_DIR / "项目"
ARCHIVE_DIR = PROJECT_DIR / "归档"

DOCKING_DIRNAME = "对接"
DELIVERY_DIRNAME = "交付"
WORKSPACE_DIRNAME = "工作区"
TASK_SHEET_NAME = "任务单.md"
TASK_META_NAME = "task.yaml"
REVIEW_NAME = "审查.md"
REVIEW_DONE_NAME = "审核完成.md"
REVIEW_HISTORY_DIRNAME = "审查历史"
DONE_NAME = "DONE.md"
IDEA_NAME = "idea.md"
SUBTASKS_NAME = "subtasks.yaml"
REGISTER_DONE_NAME = "登记完成.md"
HISTORY_DIRNAME = "历史"
SUBTASK_HISTORY_DIRNAME = "子任务"

DISPATCH_DIR = COLLAB_DIR / "调度台"
ROLES_DIR = COLLAB_DIR / "roles"
AGENTS_DIR = COLLAB_DIR / "agents"
ROLES_ARCHIVE_DIR = ROLES_DIR / ".archive"

WATCH_STATE = DISPATCH_DIR / "watch.state"
ROUNDS_TSV = DISPATCH_DIR / "rounds.tsv"
JOURNAL_LOG = DISPATCH_DIR / "journal.log"
GUARD_STATUS = DISPATCH_DIR / "guard.status"

STYLE_FILE = APP_DIR / "static" / "style.css"
CONFIRM_SCRIPT_FILE = APP_DIR / "static" / "confirm.js"
NEW_TASK_SCRIPT_FILE = APP_DIR / "static" / "new-task.js"
REGISTERING_SCRIPT_FILE = APP_DIR / "static" / "registering.js"
DISPATCH_SCRIPT_FILE = APP_DIR / "static" / "dispatch.js"
REVIEW_SCRIPT_FILE = APP_DIR / "static" / "review.js"
ADVANCE_SCRIPT_FILE = APP_DIR / "static" / "advance.js"
REVISE_SCRIPT_FILE = APP_DIR / "static" / "revise.js"

HOST = "127.0.0.1"
PORT = 8787

MAX_FORM_BYTES = 512 * 1024
MAX_CONFIG_FORM_BYTES = 128 * 1024
MAX_CONFIG_FILE_BYTES = 128 * 1024
MAX_TITLE_CHARS = 120
MAX_FIELD_CHARS = 20_000
MAX_IDEA_CHARS = 120_000
MAX_SUBTASKS_FILE_BYTES = 256 * 1024
MAX_LOG_VIEW_BYTES = 512 * 1024
MAX_WORKDIR_CHARS = 1024
MAX_CONFIG_NAME_CHARS = 80
MAX_CONFIG_TEXT_CHARS = 4_000
MAX_STORED_VERSION_CHARS = 80
MAX_SCOPE_ITEMS = 64
MAX_SCOPE_CHARS = 200
VERSION_TIMEOUT_SECONDS = 3
MAX_FILE_PREVIEW_BYTES = 256 * 1024
JOURNAL_TAIL_LINES = 30
RUN_LOG_TAIL_LINES = runner_engine.MAX_LOG_TAIL_LINES
RUN_HISTORY_LIMIT = 10
DEFAULT_RUN_TIMEOUT_SECONDS = int(runner_engine.DEFAULT_TIMEOUT_SECONDS)
MAX_RUN_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
DEFAULT_REGISTRATION_TIMEOUT_SECONDS = int(
    runner_engine.DEFAULT_REGISTRATION_TIMEOUT_SECONDS
)
MAX_REGISTRATION_TIMEOUT_SECONDS = 24 * 60 * 60
REGISTER_POLL_INTERVAL_MS = 2000
PICK_FOLDER_TIMEOUT_SECONDS = 120
PICK_FOLDER_SCRIPT = (
    'POSIX path of (choose folder with prompt "选择任务的项目文件夹")'
)

REGISTERING = "registering"
REGISTERED = "registered"
REGISTER_FAILED = "register_failed"
REGISTER_STATUS_LABELS = {
    REGISTERING: "登记中",
    REGISTERED: "登记完成",
    REGISTER_FAILED: "登记失败",
}
SUBTASK_STATUSES = ("pending", "running", "delivered", "passed", "failed")
SUBTASK_STATUS_LABELS = {
    "pending": "待开始",
    "running": "进行中",
    "delivered": "已交付",
    "passed": "已通过",
    "failed": "失败",
}
SUBTASK_STATUS_TONES = {
    "pending": "pending",
    "running": "running",
    "delivered": "delivered",
    "passed": "passed",
    "failed": "failed",
}
CONTROLLER_ROLE_ID = "controller"
CONTROLLER_FALLBACK_IDS = ("openclaw-main", "deepseek-harness")
REVIEW_ROLE_ID = "reviewer"

# 工作流阶段：分发 → 审核。顺序判定见 _task_phase。
PHASE_UNDISPATCHED = "undispatched"
PHASE_DISPATCHING = "dispatching"
PHASE_REVIEW_READY = "review_ready"
PHASE_REVIEWING = "reviewing"
PHASE_PASSED = "passed"
PHASE_REVISE = "revise"
PHASE_HANDOFF_BLOCKED = "handoff_blocked"
PHASE_META = {
    PHASE_UNDISPATCHED: {"label": "未分发", "tone": "pending"},
    PHASE_DISPATCHING: {"label": "分发中", "tone": "running"},
    PHASE_REVIEW_READY: {"label": "待审核", "tone": "delivered"},
    PHASE_REVIEWING: {"label": "审核中", "tone": "running"},
    PHASE_PASSED: {"label": "已通过", "tone": "passed"},
    PHASE_REVISE: {"label": "须修正", "tone": "revise"},
    PHASE_HANDOFF_BLOCKED: {"label": "交接阻塞", "tone": "failed"},
}

# 交接闸门：阶段之间只认文件，不认 Agent 的口头“已完成”。
HANDOFF_LOG_PREFIX = "handoff"
HANDOFF_MAX_SUPPLEMENTS = 2
HANDOFF_GATE_LABELS = {
    "G1": "登记完成",
    "G2": "子任务交付",
    "G3": "审核完成",
    "G4": "修正完成",
}
HANDOFF_STAGE_LABELS = {
    "dispatch": "分发前交接体检",
    "review": "审核前交接体检",
    "review_complete": "审核完成交接体检",
    "revise_before": "继续修正前交接体检",
    "revise_complete": "修正完成交接体检",
}
HANDOFF_STATE_NAMES = {
    "idle": "未开始",
    "checking": "体检中",
    "supplementing": "自动补交中",
    "passed": "体检通过",
    "blocked": "交接阻塞",
}
HANDOFF_STATE_TONES = {
    "idle": "pending",
    "checking": "running",
    "supplementing": "running",
    "passed": "passed",
    "blocked": "failed",
}
HANDOFF_REQUIRED_DONE_MARKERS = ("做了什么", "怎么验", "结果")
REVIEW_STATE_NAMES = {
    "idle": "未开始",
    "running": "审核中",
    "completed": "审核完成",
    "review_failed": "审核失败",
}
REVIEW_STATE_TONES = {
    "idle": "idle",
    "running": "running",
    "completed": "passed",
    "review_failed": "failed",
}
ADVANCE_STATE_NAMES = {
    "idle": "未开始",
    "running": "推进中",
    "completed": "推进完成",
    "stopped": "已停止",
    "stopped_with_budget": "预算用尽已停止",
    "failed": "推进失败",
    "handoff_blocked": "交接阻塞",
}
ADVANCE_STATE_TONES = {
    "idle": "idle",
    "running": "running",
    "completed": "passed",
    "stopped": "revise",
    "stopped_with_budget": "revise",
    "failed": "failed",
    "handoff_blocked": "failed",
}
ADVANCE_STEP_LABELS = {
    "starting": "准备",
    "handoff": "交接体检",
    "dispatch": "分发剩余",
    "review": "自动审核",
    "done": "结束",
}
ADVANCE_TERMINAL_STATES = {
    "completed",
    "stopped",
    "stopped_with_budget",
    "failed",
    "handoff_blocked",
}

# 全自动推进预算：默认 2 小时 / 12 个子任务 / 3 轮修订；
# 可用环境变量或 server.py 命令行参数覆盖。测试直接改这些模块常量。
ADVANCE_MAX_RUNTIME_SECONDS = float(
    os.environ.get("COLLAB_ADVANCE_MAX_RUNTIME_SECONDS", "7200")
)
ADVANCE_MAX_SUBTASKS = int(
    os.environ.get("COLLAB_ADVANCE_MAX_SUBTASKS", "12")
)
ADVANCE_MAX_REVISE_ROUNDS = int(
    os.environ.get("COLLAB_ADVANCE_MAX_REVISE_ROUNDS", "3")
)

# 继续修正（重修）：须修正 → 重置受影响子任务 → 自动分发 → 自动审核。
MAX_REVISE_ROUNDS = 3
REVISE_MODE_AFFECTED = "affected"
REVISE_MODE_ALL = "all"
REVISE_MODE_REVIEW = "review"
REVISE_MODE_WHOLE = "whole"
REVISE_MODES = (
    REVISE_MODE_AFFECTED,
    REVISE_MODE_ALL,
    REVISE_MODE_REVIEW,
    REVISE_MODE_WHOLE,
)
REVISE_MODE_LABELS = {
    REVISE_MODE_AFFECTED: "只重做问题清单涉及的子任务",
    REVISE_MODE_ALL: "全部子任务重做",
    REVISE_MODE_REVIEW: "只重新审核（不改产物）",
    REVISE_MODE_WHOLE: "整单重修（回叫 owner_role 重做整单）",
}
DEFAULT_OWNER_ROLE = "designer"
TASK_LIFECYCLE_LOCK_TIMEOUT_SECONDS = 10.0

# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

STATUS_PASSED = "已通过/结案"
STATUS_REVISE = "须修正"
STATUS_DELIVERED = "已交付"
STATUS_PENDING = "待处理"
STATUS_NEEDS_ACTION = "需处理"

STATUS_TONES = {
    STATUS_PASSED: "passed",
    STATUS_REVISE: "revise",
    STATUS_DELIVERED: "delivered",
    STATUS_PENDING: "pending",
    STATUS_NEEDS_ACTION: "revise",
}
STATUS_DISPLAY_ORDER = (
    (STATUS_PENDING, "pending"),
    (STATUS_NEEDS_ACTION, "revise"),
    (STATUS_DELIVERED, "delivered"),
    (STATUS_REVISE, "revise"),
    (STATUS_PASSED, "passed"),
)


_REVIEW_CONCLUSION_LABEL_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*)?(?:审查)?结论(?:\*\*)?\s*[：:]\s*(.*)$"
)
# 兼容历史报告把“**结论**：”写在“**审查时间**：”同一行的格式；
# 要求标签前是空白，避免误命中行内代码里被引用的 `**结论**：`。
_REVIEW_INLINE_CONCLUSION_RE = re.compile(
    r"(?<=\s)\*\*(?:审查)?结论\*\*\s*[：:]\s*(.*)$"
)
_REVIEW_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# 围栏代码块（``` / ~~~）。围栏内的示例、模板或旧报告引用不参与结论判定。
_REVIEW_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
# 标题序号（“三、”“3.”“（一）”“第 3 节”等）；规范化后判断标题是否为结论标题。
_REVIEW_HEADING_ORDINAL_RE = re.compile(
    r"^(?:"
    r"[（(][一二三四五六七八九十百零〇\d]+[）)]"
    r"|[一二三四五六七八九十百零〇]+[、.．]"
    r"|\d+[、.．)）]"
    r"|第\s*[一二三四五六七八九十百零〇\d]+\s*[章节部分]?[、.．：:]?"
    r")\s*"
)
# 结论里的否定短语（“无须修正”“全部通过，无不通过项”）不是修正结论；
# 取词前先剔除，避免“须修正/不通过”的子串误伤。只匹配带否定前缀的短语，
# 裸“不通过/未通过”仍按修正处理。
_REVIEW_NEGATED_REVISE_RE = re.compile(
    r"(?:没有|并无|不存在|未发现|未见|无|不)\s*"
    r"(?:任何|其它|其他)?\s*"
    r"(?:须修正|需修正|不通过|未通过)"
    r"(?:项|的情况|问题|之处)?"
)
# 在明确结论来源内按语义优先级取词，而非字符位置：否定/修正词优先，
# 其次待定类，最后才看通过。这样“通过与否待定”不会被较早的“通过”
# 覆盖，“通过，但须修正”也不会误判为 passed。
_REVIEW_VERDICT_TERMS = (
    (("须修正", "不通过", "未通过", "not passed", "revise"), "revise"),
    (("待定", "不确定", "待议"), ""),
    (("通过", "passed"), "passed"),
)

# 审核署名：新审查必须在头部声明审核角色与 Agent；出现 AI-A 字样视为冒名。
_REVIEW_ROLE_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?审核角色[ \t]*[：:][ \t]*(.+?)[ \t]*$",
    re.MULTILINE,
)
_REVIEW_TIME_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*+][ \t]+)?审查时间[ \t]*[：:][ \t]*\S",
    re.MULTILINE,
)
_REVIEW_AGENT_PAREN_RE = re.compile(r"[（(]\s*([^（）()]+?)\s*[）)]\s*$")
_REVIEW_IMPERSONATION_RE = re.compile(r"AI\s*[-–—－]\s*A", re.IGNORECASE)
REVIEW_ATTRIBUTION_HISTORY_PREFIX = "invalid-attribution"
# T24 起的新审查必须署名；早于此刻的既有 审查.md 按旧规则判定。
# 可用环境变量覆盖，便于隔离测试/迁移；默认值取 T24 任务创建时间。
REVIEW_ATTRIBUTION_SINCE = os.environ.get(
    "COLLAB_REVIEW_ATTRIBUTION_SINCE", "2026-10-27T02:51:29-04:00"
)
try:
    REVIEW_ATTRIBUTION_SINCE_EPOCH = datetime.fromisoformat(
        REVIEW_ATTRIBUTION_SINCE
    ).timestamp()
except ValueError:
    REVIEW_ATTRIBUTION_SINCE_EPOCH = 0.0


def _review_attribution_header(
    role_name: str, agent_id: str, *, when: str = ""
) -> str:
    """返回可原样照抄的署名两行；结论行由审核 Agent 按实际结论另写。"""
    return (
        f"- 审核角色：{role_name}（{agent_id}）\n"
        f"- 审查时间：{when or _now_iso()}\n"
    )


def _review_attribution_required(
    task: Task, state: Dict[str, object]
) -> bool:
    """新审查需要署名：本次 review 状态标记，或文件晚于 T24 起算点。"""
    if state.get("attribution_required"):
        return True
    if task.review_path is None:
        return False
    try:
        return task.review_path.stat().st_mtime >= REVIEW_ATTRIBUTION_SINCE_EPOCH
    except OSError:
        return False


def _review_attribution_issue(
    text: str, state: Dict[str, object]
) -> str:
    """校验新审查的头部署名；返回空串表示通过。

    仅对带 `attribution_required` 的新审查调用；历史审查不套用新规则。
    AI-A 字样在全篇任意位置出现都拒收（防止执行方自称 AI-A 通过）。
    """
    if not text.strip():
        return "missing"
    if _REVIEW_IMPERSONATION_RE.search(text):
        return "impersonation"
    header = "\n".join(text.splitlines()[:40])
    role_match = _REVIEW_ROLE_LINE_RE.search(header)
    if role_match is None:
        return "missing_role"
    if _REVIEW_TIME_LINE_RE.search(header) is None:
        return "missing_time"
    role_value = role_match.group(1).strip()
    paren = _REVIEW_AGENT_PAREN_RE.search(role_value)
    if paren is None:
        return "missing_agent"
    header_role = role_value[: paren.start()].strip().rstrip("：: -—").strip()
    header_agent = paren.group(1).strip()
    expected_agent = str(
        state.get("attribution_agent") or state.get("agent") or ""
    ).strip()
    expected_role = str(
        state.get("attribution_role_name") or state.get("role_name") or ""
    ).strip()
    if expected_agent and header_agent != expected_agent:
        return "wrong_agent"
    if expected_role and header_role != expected_role:
        return "wrong_role"
    return ""


def _review_attribution_message(issue: str) -> str:
    return {
        "impersonation": "审查中出现 AI-A 冒名字样，已拒收并隔离",
        "missing": "审查为空，未署名",
        "missing_role": "审查头部缺少「审核角色：」",
        "missing_time": "审查头部缺少「审查时间：」",
        "missing_agent": "审核角色未按「角色名（Agent ID）」格式署名",
        "wrong_agent": "审核角色署名的 Agent 与本次绑定不一致",
        "wrong_role": "审核角色署名与本次绑定角色不一致",
    }.get(issue, "审查署名无效")


def _review_strip_code_fences(text: str) -> str:
    """去掉 Markdown 围栏代码块；支持 ``` 与 ~~~，未闭合围栏视为到文末。"""
    kept: List[str] = []
    fence: Optional[Tuple[str, int]] = None
    for raw_line in text.splitlines():
        match = _REVIEW_FENCE_RE.match(raw_line)
        if fence is not None:
            if (
                match is not None
                and match.group(1)[0] == fence[0]
                and len(match.group(1)) >= fence[1]
                and not match.group(2).strip()
            ):
                fence = None
            continue
        if match is not None:
            fence = (match.group(1)[0], len(match.group(1)))
            continue
        kept.append(raw_line)
    return "\n".join(kept)


def _review_is_conclusion_heading(title: str) -> bool:
    """判断标题是否为结论标题：规范化后等于「结论」或以「结论」结尾。"""
    normalized = title.strip()
    bold = re.fullmatch(r"\*\*(.+?)\*\*", normalized)
    if bold:
        normalized = bold.group(1).strip()
    normalized = _REVIEW_HEADING_ORDINAL_RE.sub("", normalized).strip()
    return normalized == "结论" or normalized.endswith("结论")


def _review_conclusion_verdict(source: str) -> str:
    """只在明确结论来源内按语义优先级映射枚举词；待定/不可识别返回空。"""
    haystack = _REVIEW_NEGATED_REVISE_RE.sub("", source.lower())
    for terms, verdict in _REVIEW_VERDICT_TERMS:
        if any(term in haystack for term in terms):
            return verdict
    return ""


def _review_verdict(text: str) -> str:
    """从 审查.md 的明确结论来源提取结论。

    来源优先级：先全篇扫描显式 `结论：` / `**结论**：` 行（含
    `- **结论**：` 与同行 `**审查时间**：… **结论**：…`），取第一个非空
    来源；只有不存在非空显式行时，才回退到标题段落。标题段落仅接受
    level ≥ 2（排除 H1 文档标题），且标题去掉序号（`三、`、`3.`、
    `（一）` 等）后必须等于「结论」或以「结论」结尾；`结论解析`、
    `结论与建议` 等叙述性标题不作为来源。

    来源内按「修正 > 待定 > 通过」的语义优先级映射枚举值（取词前剔除
    “无须修正/无不通过项”等否定短语）。待定、空或没有明确结论来源时
    返回 ""，不得回退到全文关键词，避免“结论待定但正文提到通过”被判为
    passed。围栏代码块中的示例先被剔除；空的结论标签会跳过并继续扫描
    后续来源。
    """
    if not text:
        return ""
    lines = _review_strip_code_fences(text).splitlines()
    # A) 显式结论行优先：全篇取第一个非空来源，标题段落不得覆盖。
    for raw_line in lines:
        line = raw_line.strip()
        label = _REVIEW_CONCLUSION_LABEL_RE.match(line)
        if label:
            source = label.group(1).strip()
        else:
            inline_label = _REVIEW_INLINE_CONCLUSION_RE.search(line)
            source = inline_label.group(1).strip() if inline_label else ""
        if not source:
            continue
        return _review_conclusion_verdict(source)
    # B) 标题来源收窄：仅 level ≥ 2 且标题本身是“结论”。
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        heading = _REVIEW_HEADING_RE.match(line)
        if heading is None:
            continue
        level = len(heading.group(1))
        if level < 2 or not _review_is_conclusion_heading(heading.group(2)):
            continue
        body: List[str] = []
        for following in lines[index + 1:]:
            stripped = following.strip()
            next_heading = _REVIEW_HEADING_RE.match(stripped)
            if next_heading and len(next_heading.group(1)) <= level:
                break
            body.append(stripped)
        source = "\n".join(body)
        if not source.strip():
            continue
        return _review_conclusion_verdict(source)
    return ""


def _review_issue_section(text: str) -> str:
    """返回“问题清单（待修正）”标题下的原文，供摘要与子任务解析共用。

    支持标题下继续出现更深的子标题；遇到同级或更高级标题即结束。
    不做完整 Markdown 解析，表格行会原样保留。
    """
    if not text:
        return ""
    collected: List[str] = []
    in_issues = False
    level = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = re.match(r"^(#{1,6})\s*(.+?)\s*$", line)
        bold_heading = (
            re.match(r"^\*\*(.+?)\*\*\s*$", line) if line.startswith("**") else None
        )
        title = heading or bold_heading
        if title is not None:
            new_level = len(heading.group(1)) if heading else 7
            label = (
                heading.group(2) if heading else bold_heading.group(1)
            ).strip()
            is_issue_heading = "问题" in label or "待修正" in label
            if in_issues:
                if new_level <= level:
                    if is_issue_heading:
                        level = new_level
                    else:
                        break
                continue
            if is_issue_heading:
                in_issues = True
                level = new_level
            continue
        if in_issues:
            collected.append(raw_line)
    return "\n".join(collected)


def _review_table_cells(line: str) -> List[str]:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _is_table_separator(cells: List[str]) -> bool:
    if not cells:
        return False
    return all(
        re.fullmatch(r":?-{2,}:?", cell.replace(" ", "")) is not None
        for cell in cells
        if cell
    ) and any(cell for cell in cells)


def _review_issue_items(text: str, limit: int = 8) -> List[str]:
    """提取“问题清单”下的条目（列表或表格），供详情页做摘要。"""
    section = _review_issue_section(text)
    if not section:
        return []
    lines = section.splitlines()
    items: List[str] = []

    def add(item: str) -> bool:
        item = item.strip()
        if not item:
            return False
        items.append(item if len(item) <= 240 else item[:240] + "…")
        return len(items) >= limit

    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        bullet = re.match(r"^(?:[-*+]|\d+[.)])\s+(.+)$", line)
        if bullet is not None:
            if add(bullet.group(1)):
                break
            continue
        cells = _review_table_cells(line)
        if not cells:
            continue
        if _is_table_separator(cells):
            continue
        next_cells: List[str] = []
        for following in lines[index + 1:]:
            if not following.strip():
                continue
            next_cells = _review_table_cells(following)
            break
        if _is_table_separator(next_cells):
            # 表头（下一行是分隔行）不入摘要。
            continue
        if add(" · ".join(cell for cell in cells if cell)):
            break
    return items


def _review_target_ids(task: Task) -> List[str]:
    """从问题清单中解析受影响子任务编号（Txx-n），按 subtasks 顺序返回。"""
    section = _review_issue_section(task.review_text)
    if not section:
        return []
    found = {
        match.group(0).upper()
        for match in re.finditer(
            r"(?<![A-Za-z0-9])T\d+-\d+(?![0-9])", section, re.IGNORECASE
        )
    }
    return [item.id for item in task.subtasks if item.id.upper() in found]


def _file_signature(path: Path) -> Optional[List[object]]:
    """文件的 (mtime, sha256) 快照；用于判断审核是否真的重写了本轮文件。"""
    try:
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return [stat.st_mtime, digest]


def _artifact_updated(path: Path, baseline: object) -> bool:
    """本轮审核要求重新写入；基线不存在时任何新文件都算更新。"""
    current = _file_signature(path)
    if current is None:
        return False
    if not isinstance(baseline, list) or len(baseline) != 2:
        return True
    return current != baseline

FORM_FIELDS = (
    "title",
    "goal",
    "input",
    "output",
    "acceptance",
    "prohibitions",
    "workdir",
    "owner_role",
    "idea",
    "auto_advance",
    "mode",
)
PUBLISH_FIELDS = ("title", "idea", "workdir", "auto_advance")
AUTO_ADVANCE_FIELD = "auto_advance"
TASK_META_KEYS = (
    "id",
    "title",
    "workdir",
    "created_at",
    "status",
    "controller",
    "register_run",
    "register_started_at",
    "register_log",
    "register_error",
    "registered_at",
    "idea_sha256",
    "subtask_count",
    "auto_advance",
    "revise_round",
    "owner_role",
)
PROJECT_NAME_RE = re.compile(r"^(T\d+)(?:_([^/]*))?$", re.IGNORECASE)
TASK_ID_RE = re.compile(r"^T\d+$", re.IGNORECASE)
SUBTASK_ID_RE = re.compile(r"^T\d+-\d+$", re.IGNORECASE)
CONFIG_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
TEXT_FILE_SUFFIXES = {".md", ".markdown", ".txt", ".log", ".json", ".csv", ".tsv", ".py", ".sh"}

KNOWN_AGENT_COMMANDS = ("claude", "claude-ds", "claude-doubao", "codex", "openclaw")
DEFAULT_ROLE_SPECS = (
    (
        "controller",
        "总控",
        "统筹任务分派、协作节奏与角色路由；实际执行由任务详情页的派发触发。",
    ),
    (
        "designer",
        "设计师",
        "负责方案与界面设计，按绑定的 Agent 应用产出设计交付。",
    ),
    (
        "reviewer",
        "审核",
        "独立审查交付质量、边界与验收证据，给出通过或须修正意见。",
    ),
)

TASK_WRITE_LOCK = threading.Lock()
PROJECT_MOVE_LOCK = threading.Lock()
CONFIG_WRITE_LOCK = threading.Lock()
TASK_LIFECYCLE_LOCKS: Dict[str, threading.RLock] = {}
TASK_LIFECYCLE_LOCKS_GUARD = threading.Lock()
TASK_LIFECYCLE_STATE = threading.local()
REGISTRATION_START_LOCK = threading.Lock()
ACTIVE_REGISTRATIONS: set[str] = set()
DISPATCH_START_LOCK = threading.Lock()
DISPATCH_STATE_LOCK = threading.RLock()
ACTIVE_DISPATCH: Optional[Dict[str, object]] = None
DISPATCH_POLL_INTERVAL_MS = 1500
REVIEW_START_LOCK = threading.Lock()
REVIEW_STATE_LOCK = threading.RLock()
ACTIVE_REVIEW: Optional[Dict[str, object]] = None
REVIEW_POLL_INTERVAL_MS = 1500
ADVANCE_START_LOCK = threading.Lock()
ADVANCE_STATE_LOCK = threading.RLock()
ACTIVE_ADVANCE: Optional[Dict[str, object]] = None
ADVANCE_POLL_INTERVAL_MS = 1500
AUTO_ADVANCE_WAIT_SECONDS = 30 * 60
REVISE_START_LOCK = threading.Lock()
REVISE_POLL_INTERVAL_MS = 1500
HANDOFF_RUN_LOCK = threading.Lock()
HANDOFF_STATE_LOCK = threading.RLock()
ACTIVE_HANDOFF: Optional[Dict[str, object]] = None
HANDOFF_POLL_INTERVAL_MS = 1500

RUNNER_TIMEOUT_SECONDS = float(DEFAULT_RUN_TIMEOUT_SECONDS)
REGISTRATION_TIMEOUT_SECONDS = float(DEFAULT_REGISTRATION_TIMEOUT_SECONDS)
RUNNER: Optional[runner_engine.Runner] = None


def _runner() -> runner_engine.Runner:
    global RUNNER
    if RUNNER is None:
        RUNNER = runner_engine.Runner(PROJECT_DIR, timeout_seconds=RUNNER_TIMEOUT_SECONDS)
    return RUNNER


class FormError(ValueError):
    """表单校验失败；消息用于页面提示。"""


class ActionError(RuntimeError):
    """归档/恢复/分发失败；携带可直接渲染的状态码与说明。"""

    def __init__(
        self,
        status: int,
        title: str,
        message: str,
        hint: str = "",
        violations: Optional[List[str]] = None,
        extra: Optional[Dict[str, object]] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.title = title
        self.message = message
        self.hint = hint
        self.violations = list(violations or [])
        self.extra = dict(extra or {})


class ConfigError(ValueError):
    """角色/Agent YAML 配置无法读取或字段不合法。"""


def _task_lifecycle_lock_path(task_id: str) -> Path:
    return DISPATCH_DIR / ".locks" / f"task-{task_id.upper()}.lock"


def _acquire_task_lock_file(task_id: str):
    """跨进程文件锁；不可用或目录不可写时退化为进程内锁。"""
    if fcntl is None:
        return None
    path = _task_lifecycle_lock_path(task_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+")
    except OSError:
        return None
    deadline = time.monotonic() + TASK_LIFECYCLE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise ActionError(
                    409,
                    "任务正在切换状态",
                    f"{task_id} 正被另一个控制台进程移动或启动，本次没有执行任何操作。",
                    "等待当前归档/恢复/分发启动结束后重试。",
                )
            time.sleep(0.05)
        except OSError:
            handle.close()
            return None


@contextmanager
def _task_lifecycle_lock(task_id: str):
    """按任务串行化「校验 + 预占 + move/launch」。

    只覆盖归档/恢复、改 workdir、启动分发/审核/修订/推进的临界区，
    不覆盖 Agent 随后的运行期。同一线程嵌套获取时复用最外层文件锁。
    """
    tid = str(task_id or "").strip().upper()
    if not TASK_ID_RE.fullmatch(tid):
        raise ActionError(400, "任务编号不合法", f"{task_id!r} 不是有效的任务编号。")
    with TASK_LIFECYCLE_LOCKS_GUARD:
        lock = TASK_LIFECYCLE_LOCKS.setdefault(tid, threading.RLock())
    depth = int(getattr(TASK_LIFECYCLE_STATE, "depth", 0))
    acquired = lock.acquire(timeout=TASK_LIFECYCLE_LOCK_TIMEOUT_SECONDS)
    if not acquired:
        raise ActionError(
            409,
            "任务正在切换状态",
            f"{tid} 正在归档/恢复或启动另一个流程，本次没有执行任何操作。",
            "等待当前操作结束后重试。",
        )
    handle = None
    try:
        if depth == 0:
            handle = _acquire_task_lock_file(tid)
        TASK_LIFECYCLE_STATE.depth = depth + 1
        yield
    finally:
        TASK_LIFECYCLE_STATE.depth = depth
        if handle is not None:
            try:
                assert fcntl is not None
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                handle.close()
        lock.release()


@dataclass(frozen=True)
class Delivery:
    folder: str
    done_path: Path
    mtime: float


@dataclass(frozen=True)
class FileEntry:
    path: Path
    relative: str
    size: int
    mtime: float


@dataclass(frozen=True)
class Subtask:
    id: str
    title: str
    goal: str
    acceptance: str
    status: str
    agent_role: str
    log: str = ""
    run_id: str = ""
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    attempt: int = 0


@dataclass(frozen=True)
class ControllerResolution:
    agent: Optional[AgentApp]
    source: str
    note: str


@dataclass
class Task:
    task_id: str
    title: str
    path: Path
    mtime: float
    rounds: int
    status: str
    project_dir: Path
    project_name: str
    review_path: Optional[Path]
    review_text: str
    review_mtime: Optional[float]
    delivery: Optional[Delivery]
    workdir: Path
    meta_path: Path
    meta_created_at: str
    registration_status: str
    controller_agent: str
    register_log: str
    register_error: str
    registered_at: str
    subtasks: Tuple[Subtask, ...]
    subtasks_error: str
    archived: bool = False
    revise_round: int = 0
    owner_role: str = DEFAULT_OWNER_ROLE

    @property
    def docking_dir(self) -> Path:
        return self.project_dir / DOCKING_DIRNAME

    @property
    def workspace_dir(self) -> Path:
        return self.project_dir / WORKSPACE_DIRNAME

    @property
    def idea_path(self) -> Path:
        return self.docking_dir / IDEA_NAME

    @property
    def subtasks_path(self) -> Path:
        return self.docking_dir / SUBTASKS_NAME

    @property
    def uses_custom_workdir(self) -> bool:
        return self.workdir != self.workspace_dir

    @property
    def subtasks_done(self) -> int:
        return sum(
            1 for item in self.subtasks if item.status in {"delivered", "passed"}
        )


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    description: str
    agent_id: str
    write_scope: Tuple[str, ...]
    enabled: bool
    notes: str
    path: Path
    mtime: float


@dataclass(frozen=True)
class AgentApp:
    id: str
    name: str
    command: str
    args_template: str
    cwd: str
    installed: bool
    version: str
    notes: str
    path: Path
    mtime: float

    @property
    def currently_executable(self) -> bool:
        candidate = Path(self.command).expanduser()
        return candidate.is_absolute() and candidate.is_file() and os.access(str(candidate), os.X_OK)


@dataclass(frozen=True)
class DetectedAgent:
    path: str
    executable: bool
    version: str
    source: str


@dataclass(frozen=True)
class ConfigIssue:
    path: Path
    message: str


# ---------------------------------------------------------------------------
# 读盘
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _fmt_time(timestamp: Optional[float]) -> str:
    if timestamp is None:
        return "—"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _iso_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_DIR))
    except ValueError:
        return path.name


# ---------------------------------------------------------------------------
# 任务元数据：对接/task.yaml（workdir = Agent 的工作目录，可在值班台外）
# ---------------------------------------------------------------------------


def _task_meta_path(project_dir: Path) -> Path:
    return project_dir / DOCKING_DIRNAME / TASK_META_NAME


def _meta_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _meta_int(value: object) -> int:
    try:
        return max(0, int(str(value or "0").strip() or "0"))
    except (TypeError, ValueError):
        return 0


def _default_workdir(project_dir: Path) -> Path:
    return (project_dir / WORKSPACE_DIRNAME).resolve()


def _yaml_emit_meta_extra(key: str, value: object) -> str:
    """序列化 task.yaml 的未知扩展字段，避免归档/改 workdir 时丢字段。"""
    if isinstance(value, list):
        return f"{key}:\n" + "".join(
            f"  - {_yaml_emit_scalar(str(item))}\n" for item in value
        )
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}\n"
    if isinstance(value, (int, float)):
        return f"{key}: {value}\n"
    return _yaml_emit_field(key, str(value or ""))


def _task_meta_text(meta: Dict[str, object]) -> str:
    """序列化 task.yaml；核心字段总写，扩展字段仅在非空时写，未知字段原样保留。"""
    core = {"id", "title", "workdir", "created_at"}
    lines: List[str] = []
    for key in TASK_META_KEYS:
        value = meta.get(key, "")
        if key in core or value:
            lines.append(_yaml_emit_field(key, str(value)))
    extras = [
        key
        for key in meta
        if key not in TASK_META_KEYS
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", str(key))
    ]
    for key in sorted(extras):
        lines.append(_yaml_emit_meta_extra(str(key), meta[key]))
    return "".join(lines)


def _write_task_meta(
    path: Path, meta: Dict[str, object], *, create_only: bool = False
) -> bool:
    """独占创建或写后替换 task.yaml；不触碰其它文件。"""
    text = _task_meta_text(meta)
    if create_only:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            return True
        except FileExistsError:
            return False
        except OSError as exc:
            raise ConfigError(f"无法写入 {TASK_META_NAME}：{exc.strerror or exc}") from exc

    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    except OSError as exc:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        raise ConfigError(f"无法写入 {TASK_META_NAME}：{exc.strerror or exc}") from exc
    return True


def _normalize_task_meta(
    data: Dict[str, object],
    *,
    project_dir: Path,
    task_id: str,
    title: str,
    title_mtime: float,
) -> Dict[str, object]:
    def text(key: str, fallback: str) -> str:
        value = data.get(key)
        if value is None or isinstance(value, (dict, list, bool)):
            return fallback
        return str(value).strip() or fallback

    default_workdir = str(_default_workdir(project_dir))
    workdir = text("workdir", default_workdir)
    if not Path(workdir).expanduser().is_absolute():
        workdir = default_workdir
    meta: Dict[str, object] = {
        "id": task_id.upper(),
        "title": text("title", title),
        "workdir": workdir,
        "created_at": text("created_at", _meta_iso(title_mtime)),
    }
    for key in TASK_META_KEYS:
        if key in meta:
            continue
        meta[key] = text(
            key, DEFAULT_OWNER_ROLE if key == "owner_role" else ""
        )
    # 保留未知扩展字段，避免后续任何一次写回把外部集成字段清掉。
    for key, value in data.items():
        if key in meta or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", str(key)):
            continue
        meta[str(key)] = value
    return meta


def _ensure_task_meta(
    project_dir: Path, task_id: str, title: str, *, title_mtime: float
) -> Dict[str, object]:
    """读取任务元数据；task.yaml 缺失时按默认工作区自动生成。"""
    path = _task_meta_path(project_dir)
    data: Dict[str, object] = {}
    if not path.is_file():
        generated = _normalize_task_meta(
            {},
            project_dir=project_dir,
            task_id=task_id,
            title=title,
            title_mtime=title_mtime,
        )
        try:
            _write_task_meta(path, generated, create_only=True)
        except ConfigError:
            pass
    if path.is_file():
        try:
            data = _parse_simple_yaml(_read_text(path))
        except (OSError, ConfigError):
            data = {}
    return _normalize_task_meta(
        data,
        project_dir=project_dir,
        task_id=task_id,
        title=title,
        title_mtime=title_mtime,
    )


def _read_task_meta(
    project_dir: Path, task_id: str, *, title: str = "", title_mtime: float = 0.0
) -> Dict[str, object]:
    """读取 task.yaml（不自动补文件），返回带默认值的已知字段。"""
    path = _task_meta_path(project_dir)
    data: Dict[str, object] = {}
    if path.is_file():
        try:
            data = _parse_simple_yaml(_read_text(path))
        except (OSError, ConfigError):
            data = {}
    if title_mtime <= 0:
        try:
            title_mtime = path.stat().st_mtime
        except OSError:
            title_mtime = time.time()
    return _normalize_task_meta(
        data,
        project_dir=project_dir,
        task_id=task_id,
        title=title,
        title_mtime=title_mtime,
    )


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


def _validate_workdir(raw: str, *, default: Path) -> Path:
    """校验并规范化工作目录；不创建目录。默认值始终允许。"""
    value = raw.replace("\x00", "").strip()
    if not value:
        return default.resolve()
    if len(value) > MAX_WORKDIR_CHARS:
        raise FormError(f"工作目录超过 {MAX_WORKDIR_CHARS} 字符，没有写入。")
    if not value.startswith("/"):
        raise FormError("工作目录必须是绝对路径（以 / 开头）；相对路径会被拒绝。")
    if any(ord(char) < 32 for char in value):
        raise FormError("工作目录不能包含换行或控制字符。")

    try:
        resolved = Path(value).resolve()
    except (OSError, RuntimeError) as exc:
        raise FormError(f"工作目录无法解析：{exc}") from exc

    root = PROJECT_DIR.resolve()
    collab = COLLAB_DIR.resolve()
    projects = PROJECTS_DIR.resolve()

    if resolved == Path("/"):
        raise FormError("工作目录不能是文件系统根目录 /。")
    if _same_path(resolved, root):
        raise FormError("工作目录不能是值班台系统根本身；值班台不是 Agent 的工作区。")
    if _is_within(resolved, collab):
        raise FormError("工作目录不能位于 `协作/` 或其子目录；值班台的协作配置只读。")
    if _is_within(resolved, root):
        if not _is_within(resolved, projects):
            raise FormError(
                "值班台内只允许 `项目/<编号>_<短名>/工作区/` 作为工作目录；"
                "其它值班台目录不可写入。"
            )
        relative = resolved.relative_to(projects)
        if len(relative.parts) != 2 or relative.parts[1] != WORKSPACE_DIRNAME:
            raise FormError("值班台内的工作目录必须是 `项目/<编号>_<短名>/工作区/`。")

    if resolved.exists():
        if not resolved.is_dir():
            raise FormError(f"工作目录已存在但不是目录：{resolved}")
        if not os.access(str(resolved), os.W_OK | os.X_OK):
            raise FormError(f"工作目录已存在但不可写：{resolved}")
        return resolved

    ancestor = resolved
    while True:
        parent = ancestor.parent
        if parent == ancestor:
            break
        if parent.exists():
            ancestor = parent
            break
        ancestor = parent
    if not ancestor.is_dir():
        raise FormError(f"工作目录不存在且无法创建：{ancestor} 不是目录。")
    if not os.access(str(ancestor), os.W_OK | os.X_OK):
        raise FormError(f"工作目录不存在且无法创建：{ancestor} 不可写。")
    return resolved


def _ensure_workdir(raw: str, *, default: Path) -> Path:
    workdir = _validate_workdir(raw, default=default)
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FormError(f"工作目录不存在且无法创建：{exc.strerror or exc}") from exc
    if not workdir.is_dir():
        raise FormError(f"工作目录不可用：{workdir}")
    return workdir


def _validate_project_folder(raw: str) -> Path:
    """发布任务时选择的项目文件夹：必须是已存在的可用绝对目录。"""
    value = str(raw or "").replace("\x00", "").strip()
    if not value:
        raise FormError("请选择项目文件夹，或填写一个已存在的绝对路径。")
    if not value.startswith("/"):
        raise FormError("项目文件夹必须是绝对路径（以 / 开头）；相对路径会被拒绝。")
    folder = _validate_workdir(value, default=PROJECTS_DIR)
    if not folder.exists():
        raise FormError(f"项目文件夹不存在：{folder}。请先创建，或改选已有文件夹。")
    if not folder.is_dir():
        raise FormError(f"项目文件夹不是目录：{folder}")
    return folder


def _update_task_workdir(task: Task, raw: str) -> Path:
    with _task_lifecycle_lock(task.task_id):
        latest = _find_task(task.task_id)
        if latest is None:
            raise ActionError(
                409,
                "任务正在移动",
                f"{task.task_id} 已不在 项目/ 中，本次没有修改工作目录。",
                "刷新看板后再试。",
            )
        task = latest
        if task.archived:
            raise ActionError(
                409,
                "归档任务只读",
                f"{task.task_id} 位于 归档/，不能修改工作目录。",
                "先从归档库恢复项目，再修改工作目录。",
            )
        if _active_dispatch_task() == task.task_id:
            raise ActionError(
                409,
                "任务正在分发",
                f"{task.task_id} 正在按子任务串行分发，本次不修改工作目录。",
                "等待分发结束后再修改。",
            )
        if _review_active_for(task.task_id) or _active_review_task() == task.task_id:
            raise ActionError(
                409,
                "任务正在审核",
                f"{task.task_id} 正在由审核角色复核，本次不修改工作目录。",
                "等待审核结束后再修改。",
            )
        if _active_handoff_task() == task.task_id:
            raise ActionError(
                409,
                "任务正在交接体检",
                f"{task.task_id} 正在检查/补交交接文件，本次不修改工作目录。",
                "等待交接体检结束后再修改。",
            )
        if _active_advance_task() == task.task_id:
            raise ActionError(
                409,
                "任务正在全自动推进",
                f"{task.task_id} 正在自动分发/审核，本次不修改工作目录。",
                "等待推进结束后再修改。",
            )
        workdir = _ensure_workdir(raw, default=task.workspace_dir)
        meta = _ensure_task_meta(
            task.project_dir, task.task_id, task.title, title_mtime=task.mtime
        )
        meta["workdir"] = str(workdir)
        try:
            _write_task_meta(task.meta_path, meta)
        except ConfigError as exc:
            raise FormError(f"无法写入 对接/{TASK_META_NAME}：{exc}") from exc
        return workdir


def _update_task_owner_role(task: Task, raw: str) -> str:
    """编辑 task.yaml 的 owner_role（无子任务整单修重的责任角色）。"""
    with _task_lifecycle_lock(task.task_id):
        latest = _find_task(task.task_id)
        if latest is None:
            raise ActionError(
                409,
                "任务正在移动",
                f"{task.task_id} 已不在 项目/ 中，本次没有修改 owner_role。",
                "刷新看板后再试。",
            )
        task = latest
        if task.archived:
            raise ActionError(
                409,
                "归档任务只读",
                f"{task.task_id} 位于 归档/，不能修改 owner_role。",
                "先从归档库恢复项目。",
            )
        if (
            _active_dispatch_task() == task.task_id
            or _review_active_for(task.task_id)
            or _active_advance_task() == task.task_id
            or _active_handoff_task() == task.task_id
        ):
            raise ActionError(
                409,
                "任务正在运行",
                f"{task.task_id} 正在分发/审核/推进/体检，本次没有修改 owner_role。",
                "等待当前运行结束后再修改。",
            )
        role_id = str(raw or "").replace("\x00", "").strip()
        if not role_id:
            raise FormError("owner_role 不能为空；请填写已存在角色（默认 designer）。")
        if not CONFIG_ID_RE.fullmatch(role_id):
            raise FormError("owner_role 只能使用小写字母、数字、下划线或短横线。")
        role = _find_role(role_id)
        if role is None:
            raise FormError(f"owner_role={role_id} 不是已存在角色。")
        meta = _ensure_task_meta(
            task.project_dir, task.task_id, task.title, title_mtime=task.mtime
        )
        meta["owner_role"] = role_id
        try:
            _write_task_meta(task.meta_path, meta)
        except ConfigError as exc:
            raise FormError(f"无法写入 对接/{TASK_META_NAME}：{exc}") from exc
        return role_id


def _retarget_task_meta(old_project: Path, new_project: Path) -> None:
    """归档/恢复后，只改仍指向旧项目 工作区/ 的默认 workdir。

    逐行替换 `workdir:`，其它字段（含 status/controller/revise_round/
    auto_advance/idea_sha256/register_log 及未知扩展字段）、顺序与注释
    原样保留；外部 workdir 不移动，文件不重写。
    """
    meta_path = _task_meta_path(new_project)
    if not meta_path.is_file():
        return
    try:
        text = _read_text(meta_path)
    except OSError:
        return
    workdir_pattern = re.compile(r"^workdir\s*:\s*(.*)$")
    lines = text.splitlines(keepends=True)
    workdir_line = -1
    raw = ""
    for index, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if body.startswith((" ", "\t")):
            continue
        match = workdir_pattern.match(body)
        if match is None:
            continue
        try:
            parsed = _yaml_scalar(_strip_yaml_comment(match.group(1)))
        except ConfigError:
            return
        if isinstance(parsed, str):
            raw = parsed
            workdir_line = index
        break
    if workdir_line < 0 or not raw.strip() or not Path(raw).is_absolute():
        return
    try:
        old_workspace = (old_project / WORKSPACE_DIRNAME).resolve()
        current = Path(raw).resolve()
        relative = current.relative_to(old_workspace)
    except (OSError, RuntimeError, ValueError):
        return
    new_workdir = str((new_project / WORKSPACE_DIRNAME / relative).resolve())
    original = lines[workdir_line]
    newline = "\r\n" if original.endswith("\r\n") else "\n" if original.endswith("\n") else ""
    lines[workdir_line] = (
        f"workdir: {_yaml_emit_scalar(new_workdir)}{newline}"
    )
    updated = "".join(lines)
    if updated == text:
        return
    try:
        _atomic_write_text(meta_path, updated)
    except ConfigError:
        pass


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _collect_files(root: Path) -> List[FileEntry]:
    """按相对路径列出目录内全部普通文件；缺失目录返回空列表。"""
    if not root.is_dir():
        return []
    entries: List[FileEntry] = []
    try:
        candidates = list(root.rglob("*"))
    except OSError:
        return []
    for path in candidates:
        try:
            if not path.is_file():
                continue
            stat = path.stat()
            entries.append(
                FileEntry(
                    path=path,
                    relative=path.relative_to(root).as_posix(),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )
        except (OSError, ValueError):
            continue
    entries.sort(key=lambda entry: entry.relative.casefold())
    return entries


def _load_rounds() -> Dict[str, int]:
    rounds: Dict[str, int] = {}
    if not ROUNDS_TSV.is_file():
        return rounds
    try:
        text = _read_text(ROUNDS_TSV)
    except OSError:
        return rounds
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        task_id = parts[0].strip().upper()
        if not TASK_ID_RE.fullmatch(task_id):
            continue
        try:
            rounds[task_id] = int(parts[1].strip())
        except ValueError:
            continue
    return rounds


def _latest_delivery(docking_dir: Path) -> Optional[Delivery]:
    """在 对接/交付/**/DONE.md 中按 mtime 取最新一轮交付。"""
    delivery_root = docking_dir / DELIVERY_DIRNAME
    if not delivery_root.is_dir():
        return None

    candidates: List[Delivery] = []
    try:
        done_files = list(delivery_root.rglob(DONE_NAME))
    except OSError:
        return None

    for done in done_files:
        try:
            if not done.is_file():
                continue
            folder = done.parent.relative_to(docking_dir).as_posix()
            candidates.append(Delivery(folder, done, done.stat().st_mtime))
        except (OSError, ValueError):
            continue

    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.mtime, item.folder))


def _extract_title(path: Path, task_id: str, project_name: str) -> str:
    text = _read_text(path)
    heading = ""
    for line in text.splitlines()[:120]:
        if line.startswith("# "):
            heading = line[2:].strip()
            break
        titled = re.match(r"^\s*(?:任务)?标题\s*[:：]\s*(.+?)\s*$", line)
        if titled:
            heading = titled.group(1)
            break

    if heading:
        title = re.sub(
            r"^" + re.escape(task_id) + r"\s*(?:[—–·|:：-]\s*)?",
            "",
            heading,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", title)
        title = title.replace("**", "").replace("__", "").replace("`", "").strip()
        title = re.sub(r"\s*[—–-]\s*任务单\s*$", "", title).strip()
        if title:
            return title

    fallback = re.sub(r"^T\d+_?", "", project_name, count=1, flags=re.IGNORECASE)
    fallback = fallback.replace("_", " ").strip(" -—")
    return fallback or "未命名任务"


def _build_task(
    project_dir: Path,
    project_name: str,
    task_id: str,
    path: Path,
    mtime: float,
    rounds: Dict[str, int],
    *,
    archived: bool = False,
) -> Task:
    try:
        title = _extract_title(path, task_id, project_name)
    except OSError:
        title = "未命名任务"

    docking_dir = project_dir / DOCKING_DIRNAME
    review_path = docking_dir / REVIEW_NAME
    review_text = ""
    review_mtime: Optional[float] = None
    if review_path.is_file():
        review_text = _read_text(review_path)
        review_mtime = review_path.stat().st_mtime

    delivery = _latest_delivery(docking_dir)
    meta = _ensure_task_meta(project_dir, task_id, title, title_mtime=mtime)
    subtasks, subtasks_error = _load_subtasks(
        docking_dir / SUBTASKS_NAME, task_id
    )

    task = Task(
        task_id=task_id,
        title=title,
        path=path,
        mtime=mtime,
        rounds=rounds.get(task_id, 0),
        status=STATUS_PENDING,
        project_dir=project_dir,
        project_name=project_name,
        review_path=review_path if review_mtime is not None else None,
        review_text=review_text,
        review_mtime=review_mtime,
        delivery=delivery,
        workdir=Path(meta["workdir"]),
        meta_path=_task_meta_path(project_dir),
        meta_created_at=meta["created_at"],
        registration_status=str(meta.get("status") or ""),
        controller_agent=str(meta.get("controller") or ""),
        register_log=str(meta.get("register_log") or ""),
        register_error=str(meta.get("register_error") or ""),
        registered_at=str(meta.get("registered_at") or ""),
        subtasks=subtasks,
        subtasks_error=subtasks_error,
        archived=archived,
        revise_round=_meta_int(meta.get("revise_round")),
        owner_role=str(meta.get("owner_role") or DEFAULT_OWNER_ROLE),
    )
    task.status = _task_status_from_phase(task, _task_phase(task))
    return task


def _scan_project_tasks(root: Path, *, archived: bool) -> List[Task]:
    """扫描 root/*/对接/任务单.md，每个项目目录返回一张记录。"""
    if not root.is_dir():
        return []

    rounds = _load_rounds()
    tasks: List[Task] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []

    for project_dir in entries:
        try:
            if not project_dir.is_dir():
                continue
            match = PROJECT_NAME_RE.fullmatch(project_dir.name)
            if not match:
                continue
            task_path = project_dir / DOCKING_DIRNAME / TASK_SHEET_NAME
            if not task_path.is_file():
                continue
            task_id = match.group(1).upper()
            mtime = task_path.stat().st_mtime
            tasks.append(
                _build_task(
                    project_dir,
                    project_dir.name,
                    task_id,
                    task_path,
                    mtime,
                    rounds,
                    archived=archived,
                )
            )
        except OSError:
            continue

    tasks.sort(
        key=lambda task: (int(task.task_id[1:]), task.mtime, task.project_name),
        reverse=True,
    )
    return tasks


def _load_tasks() -> List[Task]:
    """只扫描 项目/*/对接/任务单.md；同编号多份时以最新任务单为准。"""
    candidates: Dict[str, Task] = {}
    for task in _scan_project_tasks(PROJECTS_DIR, archived=False):
        previous = candidates.get(task.task_id)
        if previous is None or task.mtime > previous.mtime:
            candidates[task.task_id] = task

    tasks = list(candidates.values())
    tasks.sort(key=lambda task: int(task.task_id[1:]), reverse=True)
    return tasks


def _load_archived_tasks() -> List[Task]:
    """单独扫描 归档/*/对接/任务单.md；保留同编号的全部归档项目。"""
    return _scan_project_tasks(ARCHIVE_DIR, archived=True)


def _find_task(task_id: str) -> Optional[Task]:
    """详情页先找活动项目，再回落到 归档/ 中的只读档案。"""
    for task in _load_tasks():
        if task.task_id == task_id:
            return task
    for task in _load_archived_tasks():
        if task.task_id == task_id:
            return task
    return None


def _latest_task(tasks: List[Task], task_id: str, project_name: str = "") -> Optional[Task]:
    matches = [task for task in tasks if task.task_id == task_id]
    if project_name:
        matches = [task for task in matches if task.project_name == project_name]
    if not matches:
        return None
    return max(matches, key=lambda task: (task.mtime, task.project_name))


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _unique_project_target(parent: Path, project_name: str) -> Path:
    """同名项目已存在时加时间戳后缀，绝不覆盖。"""
    target = parent / project_name
    if not _path_present(target):
        return target

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{project_name}-{stamp}"
    target = parent / base
    sequence = 2
    while _path_present(target):
        target = parent / f"{base}-{sequence}"
        sequence += 1
    return target


def _archive_project(task_id: str, project_name: str = "") -> Path:
    """把 项目/ 中的整个项目目录移动到 归档/；只移动，不删除。"""
    with _task_lifecycle_lock(task_id):
        task = _latest_task(_load_tasks(), task_id, project_name)
        if task is None:
            raise ActionError(
                404,
                "没有找到可归档的项目",
                f"{task_id} 不在 项目/*/对接/任务单.md 中。",
                "刷新看板后再试；已归档项目请到归档库恢复。",
            )
        snapshot = _runner().snapshot()
        lock = _runner().lock_info()
        # 总控登记不写 runner.status；活动任务必须以锁上的 task 为准，
        # 否则旧 status 里的任务号会让正在登记的其它项目被误移走。
        active_task = (
            str(lock.get("task") or "")
            if lock.get("active")
            else str(snapshot.get("task") or "")
        )
        if (snapshot.get("running") or lock.get("active")) and active_task == task.task_id:
            raise ActionError(
                409,
                "任务正在执行",
                f"{task.task_id} 正在由执行器运行，项目目录不能移动。",
                "等待运行结束并在 /runs 确认状态后再归档。",
            )
        if _active_dispatch_task() == task.task_id:
            raise ActionError(
                409,
                "任务正在分发",
                f"{task.task_id} 正在按子任务串行分发，项目目录不能移动。",
                "等待分发结束或失败停止后再归档。",
            )
        if _review_active_for(task.task_id):
            raise ActionError(
                409,
                "任务正在审核",
                f"{task.task_id} 正在由审核角色复核，项目目录不能移动。",
                "等待审核结束后再归档。",
            )
        if _active_handoff_task() == task.task_id:
            raise ActionError(
                409,
                "任务正在交接体检",
                f"{task.task_id} 正在检查/补交交接文件，项目目录不能移动。",
                "等待交接体检结束后再归档。",
            )
        if _active_advance_task() == task.task_id:
            raise ActionError(
                409,
                "任务正在全自动推进",
                f"{task.task_id} 正在自动分发/审核，项目目录不能移动。",
                "等待推进结束后再归档。",
            )

        try:
            with PROJECT_MOVE_LOCK:
                ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
                target = _unique_project_target(ARCHIVE_DIR, task.project_name)
                shutil.move(str(task.project_dir), str(target))
            try:
                _retarget_task_meta(task.project_dir, target)
            except Exception:
                traceback.print_exc()
        except OSError as exc:
            raise ActionError(
                500,
                "归档没有完成",
                f"移动项目目录时出错：{exc}",
                "请查看启动终端的错误信息，并核对 项目/ 与 归档/ 当前状态。",
            ) from exc
        return target


def _restore_project(task_id: str, project_name: str = "") -> Path:
    """把 归档/ 中的整个项目目录移回 项目/；只移动，不覆盖同名目录。"""
    with _task_lifecycle_lock(task_id):
        task = _latest_task(_load_archived_tasks(), task_id, project_name)
        if task is None:
            raise ActionError(
                404,
                "没有找到已归档的项目",
                f"{task_id} 不在 归档/*/对接/任务单.md 中。",
                "回到归档库确认项目编号后再试。",
            )

        try:
            with PROJECT_MOVE_LOCK:
                PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
                target = _unique_project_target(PROJECTS_DIR, task.project_name)
                shutil.move(str(task.project_dir), str(target))
            try:
                _retarget_task_meta(task.project_dir, target)
            except Exception:
                traceback.print_exc()
        except OSError as exc:
            raise ActionError(
                500,
                "恢复没有完成",
                f"移动项目目录时出错：{exc}",
                "请查看启动终端的错误信息，并核对 归档/ 与 项目/ 当前状态。",
            ) from exc
        return target


def _next_task_id() -> str:
    """归档项目仍占用编号，避免未来任务复用旧编号后覆盖历史档案。"""
    used = set()
    for root in (PROJECTS_DIR, ARCHIVE_DIR):
        if not root.is_dir():
            continue
        for project_dir in root.iterdir():
            match = PROJECT_NAME_RE.fullmatch(project_dir.name)
            if match:
                used.add(int(match.group(1)[1:]))
    number = 1
    while number in used:
        number += 1
    return f"T{number:02d}"


def _slugify(title: str) -> str:
    chars: List[str] = []
    for char in title:
        if char.isalnum() or char in "-_":
            chars.append(char)
        else:
            chars.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(chars)).strip("-_")
    slug = slug[:48].strip("-_")
    return slug or "任务"


def _normalize_body(value: str) -> str:
    return (
        value.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\x00", "")
        .strip()
    )


def _blank_form() -> Dict[str, str]:
    return {name: "" for name in FORM_FIELDS}


def _create_task(fields: Dict[str, str]) -> str:
    title = " ".join(fields.get("title", "").replace("\x00", "").split())
    if not title:
        raise FormError("标题不能为空，任务单没有写入。")
    if len(title) > MAX_TITLE_CHARS:
        raise FormError(f"标题超过 {MAX_TITLE_CHARS} 字，任务单没有写入。")

    cleaned: Dict[str, str] = {"title": title}
    labels = {
        "goal": "目标",
        "input": "输入",
        "output": "输出",
        "acceptance": "验收标准",
        "prohibitions": "禁止事项",
    }
    for name in ("goal", "input", "output", "acceptance", "prohibitions"):
        value = _normalize_body(fields.get(name, ""))
        if len(value) > MAX_FIELD_CHARS:
            raise FormError(f"字段「{labels[name]}」超过 {MAX_FIELD_CHARS} 字，任务单没有写入。")
        cleaned[name] = value

    def section(name: str) -> str:
        return cleaned[name] if cleaned[name] else "（未填写）"

    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    slug = _slugify(title)

    with TASK_WRITE_LOCK:
        task_id = _next_task_id()
        project_name = f"{task_id}_{slug}"
        project_dir = PROJECTS_DIR / project_name
        docking_dir = project_dir / DOCKING_DIRNAME
        path = docking_dir / TASK_SHEET_NAME
        workdir = _ensure_workdir(
            fields.get("workdir", ""), default=project_dir / WORKSPACE_DIRNAME
        )
        body = (
            f"# {task_id} — {title}\n\n"
            f"- 编号：{task_id}\n"
            f"- 创建时间：{created}\n"
            f"- 项目目录：项目/{project_name}/\n"
            "- 来源：本地控制台\n\n"
            "## 目标\n\n"
            f"{section('goal')}\n\n"
            "## 输入\n\n"
            f"{section('input')}\n\n"
            "## 输出\n\n"
            f"{section('output')}\n\n"
            "## 验收标准\n\n"
            f"{section('acceptance')}\n\n"
            "## 禁止事项\n\n"
            f"{section('prohibitions')}\n"
        )
        try:
            # 只建项目、对接目录与选定的工作目录；交付/、审查.md 留给协作方创建。
            docking_dir.mkdir(parents=True, exist_ok=False)
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
        except FileExistsError as exc:
            raise FormError("编号刚刚被占用，任务单没有写入；请刷新后重试。") from exc
        except OSError as exc:
            raise FormError(f"任务单写入失败：{exc.strerror or '磁盘错误'}。") from exc

        try:
            _write_task_meta(
                _task_meta_path(project_dir),
                {
                    "id": task_id,
                    "title": title,
                    "workdir": str(workdir),
                    "created_at": _now_iso(),
                },
                create_only=True,
            )
        except ConfigError as exc:
            # 任务单已落盘；下次读取会按默认工作区补生成，不阻断任务创建。
            print(f"[warn] {task_id} 的 {TASK_META_NAME} 未写入：{exc}")

    return task_id


# ---------------------------------------------------------------------------
# 发布 → 总控登记（T08）
# ---------------------------------------------------------------------------


def _slug_from_idea(idea: str) -> str:
    for line in idea.splitlines():
        candidate = re.sub(r"^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)]\s+)?", "", line)
        candidate = candidate.replace("`", "").replace("*", "").strip()
        if candidate:
            return _slugify(candidate[:48])
    return "待登记"


def _create_published_task(fields: Dict[str, str]) -> Dict[str, object]:
    idea = str(fields.get("idea") or "")
    if "\x00" in idea:
        raise FormError("想法正文包含 NUL 字符，未写入任何文件。")
    if not idea.strip():
        raise FormError("请写下想法或要实现的任务，再发布。")
    if len(idea) > MAX_IDEA_CHARS:
        raise FormError(f"想法正文超过 {MAX_IDEA_CHARS} 字，未写入任何文件。")

    title = " ".join(
        str(fields.get("title") or "").replace("\x00", "").split()
    )
    if len(title) > MAX_TITLE_CHARS:
        raise FormError(f"任务标题超过 {MAX_TITLE_CHARS} 字，未写入任何文件。")

    workdir = _validate_project_folder(str(fields.get("workdir") or ""))
    auto_advance = str(fields.get(AUTO_ADVANCE_FIELD) or "").strip().lower() in {
        "1",
        "on",
        "true",
        "yes",
    }
    resolution = _resolve_controller()
    if resolution.agent is None:
        raise FormError(resolution.note)
    controller = resolution.agent

    active_advance = _active_advance_task()
    if active_advance:
        raise FormError(
            f"{active_advance} 的全自动推进正在运行，本次没有创建项目；"
            "请等待推进结束后再发布。"
        )
    lock = _runner().lock_info()
    if ACTIVE_REGISTRATIONS or lock.get("active"):
        current = str(lock.get("task") or "").strip()
        raise FormError(
            "执行器正忙"
            + (f"（{current}）" if current else "")
            + "，本次没有创建项目；请等待当前运行/登记结束后再发布。"
        )

    idea_bytes = idea.encode("utf-8")
    idea_hash = hashlib.sha256(idea_bytes).hexdigest()
    created = _now_iso()

    with TASK_WRITE_LOCK:
        task_id = _next_task_id()
        slug = _slugify(title) if title else _slug_from_idea(idea)
        project_name = f"{task_id}_{slug}"
        project_dir = PROJECTS_DIR / project_name
        docking_dir = project_dir / DOCKING_DIRNAME
        if _path_present(project_dir):
            raise FormError("编号刚刚被占用，没有写入任何文件；请重试发布。")
        if _is_within(workdir, project_dir):
            raise FormError("项目文件夹不能位于新任务自己的项目目录内。")
        try:
            docking_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise FormError("编号刚刚被占用，没有写入任何文件；请重试发布。") from exc
        except OSError as exc:
            raise FormError(f"无法创建任务项目目录：{exc.strerror or exc}") from exc

        idea_path = docking_dir / IDEA_NAME
        try:
            with idea_path.open("xb") as handle:
                handle.write(idea_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise FormError(f"写入 {IDEA_NAME} 失败：{exc.strerror or exc}") from exc

        meta = {
            "id": task_id,
            "title": title,
            "workdir": str(workdir),
            "created_at": created,
            "status": REGISTERING,
            "controller": controller.id,
            "idea_sha256": idea_hash,
            "auto_advance": "1" if auto_advance else "",
        }
        try:
            _write_task_meta(
                _task_meta_path(project_dir), meta, create_only=True
            )
        except ConfigError as exc:
            raise FormError(f"写入 {TASK_META_NAME} 失败：{exc}") from exc

    return {
        "task_id": task_id,
        "project_dir": project_dir,
        "project_name": project_name,
        "title": title,
        "workdir": workdir,
        "idea_sha256": idea_hash,
        "controller": controller,
        "resolution": resolution,
        "auto_advance": auto_advance,
    }


def _registration_prompt(
    *,
    task_id: str,
    project_name: str,
    project_dir: Path,
    workdir: Path,
    title_hint: str,
) -> str:
    docking = project_dir / DOCKING_DIRNAME
    title_instruction = (
        f"人类指定标题为「{title_hint}」，任务单与 task.yaml 必须使用这个标题，不要另起。"
        if title_hint
        else "人类没有指定标题：请根据 idea.md 起一个简洁、具体的中文标题。"
    )
    roles, _issues = _load_roles()
    available_roles = [
        role for role in roles if role.enabled and role.id != CONTROLLER_ROLE_ID
    ]
    role_ids = "、".join(role.id for role in available_roles) or "designer、reviewer"
    owner_suggestion = (
        DEFAULT_OWNER_ROLE
        if any(role.id == DEFAULT_OWNER_ROLE for role in roles)
        else (available_roles[0].id if available_roles else DEFAULT_OWNER_ROLE)
    )
    return f"""你是协作值班台的总控。你只负责「任务登记 + 拆子任务」，不要执行任何子任务。

任务编号：{task_id}
值班台根：{PROJECT_DIR}
任务项目：{project_dir}
人类项目文件夹（未来执行子任务的工作目录）：{workdir}

先读（只读，严禁改写/覆盖/移动）：
- 人类原文：{docking / IDEA_NAME}

{title_instruction}

必须完成并真实写盘这四件事：
1. 写 {docking / TASK_SHEET_NAME}
   内容必须包含这些二级标题：## 目标、## 范围、## 验收标准、## 禁止事项、## 子任务清单。
   子任务清单要与 subtasks.yaml 一致；任务单是给人看的总览。
2. 写 {docking / SUBTASKS_NAME}
   拆成 2–6 个可独立运行的小任务，必须严格使用下面的 YAML 结构：
   subtasks:
     - id: {task_id}-1
       title: 简短标题
       goal: 这个子任务要达成什么
       acceptance: 可检查的验收标准
       status: pending
       agent_role: designer
     - id: {task_id}-2
       title: 简短标题
       goal: 这个子任务要达成什么
       acceptance: 可检查的验收标准
       status: pending
   规则：id 必须严格写成 {task_id}-<数字>（大小写不敏感，唯一，禁止 /、\\、..、绝对路径）；
   status 只能是 pending/running/delivered/passed/failed，初始一律写 pending；
   每个子任务必须写 agent_role，且必须是已存在角色（可用：{role_ids}）；
   缺角色或未知角色会让整次登记失败（register_failed）。
3. 更新 {docking / TASK_META_NAME}
   只把 status 改为 registered；保留 id、title、workdir、created_at 等既有字段。
   同时写入 owner_role: {owner_suggestion}（必须是已存在角色；仅无子任务整单重修时回叫它，
   人类之后可在任务详情编辑）。不要改 workdir。
   若 human 未指定标题，可同时把 title 补成你起的标题。
4. 最后写完成信号 {docking / REGISTER_DONE_NAME}
   写标题、子任务数量和时间即可。此文件必须最后写，表示登记完成。

禁止：
- 不改写 idea.md，不删文件，不修改其它项目；
- 不写 {docking / REVIEW_NAME}（审查由 AI-A 负责）；
- 不向 {PROJECT_DIR / "协作"} 写任何东西；
- 不运行子任务、不安装依赖、不重启 8787；
- 不把产物写到值班台外的项目文件夹；本次只写上面的 对接/ 文件。
所有路径都使用绝对路径。必须实际创建/更新文件后再退出。"""


def _registration_failure_log(
    project_dir: Path, task_id: str, error: str
) -> str:
    """总控尚未生成日志就失败时，仍留下可指向的登记日志。"""
    log_dir = project_dir / WORKSPACE_DIRNAME / "日志"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        sequence = 1
        while True:
            suffix = "" if sequence == 1 else f"-{sequence}"
            path = log_dir / f"register-{stamp}{suffix}.log"
            try:
                with path.open("xb") as handle:
                    handle.write(
                        (
                            "=== 协作执行器 · 总控登记失败 ===\n"
                            f"task: {task_id}\n"
                            f"time: {_now_iso()}\n"
                            f"error: {error}\n"
                        ).encode("utf-8")
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                return _display_path(path)
            except FileExistsError:
                sequence += 1
                if sequence > 999:
                    return ""
            except OSError:
                return ""
    except OSError:
        return ""


def _mark_registration_failed(
    project_dir: Path,
    task_id: str,
    error: str,
    log: str,
    *,
    controller: str = "",
) -> None:
    with TASK_WRITE_LOCK:
        meta = _read_task_meta(project_dir, task_id)
        meta["status"] = REGISTER_FAILED
        meta["register_error"] = error.strip()[:4_000]
        if log:
            meta["register_log"] = log
        elif not meta.get("register_log"):
            fallback_log = _registration_failure_log(project_dir, task_id, error)
            if fallback_log:
                meta["register_log"] = fallback_log
        if controller:
            meta["controller"] = controller
        try:
            _write_task_meta(_task_meta_path(project_dir), meta)
        except ConfigError as exc:
            print(f"[登记] {task_id} 写入失败状态失败：{exc}")


def _file_written_since(
    path: Path, start_epoch: float, tolerance: float = 0.5
) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    if not path.is_file() or stat.st_size <= 0:
        return False
    return stat.st_mtime >= (start_epoch - tolerance)


def _registration_result_error(
    result: Dict[str, object],
    *,
    project_dir: Path,
    idea_hash: str,
    start_epoch: float,
) -> Tuple[str, Optional[str], Tuple[Subtask, ...]]:
    state = str(result.get("state") or "failed")
    if state != "success":
        labels = {"failed": "失败", "timeout": "超时"}
        detail = str(result.get("error") or "").strip()
        return (
            f"总控调用{labels.get(state, state)}"
            + (f"：{detail}" if detail else "。")
        ), None, ()

    problems: List[str] = []
    docking = project_dir / DOCKING_DIRNAME
    task_sheet = docking / TASK_SHEET_NAME
    subtasks_path = docking / SUBTASKS_NAME
    marker = docking / REGISTER_DONE_NAME
    idea_path = docking / IDEA_NAME

    try:
        current_hash = hashlib.sha256(idea_path.read_bytes()).hexdigest()
    except OSError:
        current_hash = ""
    if current_hash != idea_hash:
        problems.append("idea.md 被改写或无法读取")

    if not _file_written_since(task_sheet, start_epoch):
        problems.append("任务单.md 未在本次登记中写入")
    if not _file_written_since(subtasks_path, start_epoch):
        problems.append("subtasks.yaml 未在本次登记中写入")
    if not _file_written_since(marker, start_epoch):
        problems.append("登记完成.md 未在最后写入")
    try:
        marker_mtime = marker.stat().st_mtime
        other_mtime = max(
            task_sheet.stat().st_mtime, subtasks_path.stat().st_mtime
        )
        if marker_mtime + 1.0 < other_mtime:
            problems.append("登记完成.md 早于任务单或子任务文件，不是最后写入的完成信号")
    except OSError:
        pass

    project_match = PROJECT_NAME_RE.fullmatch(project_dir.name)
    expected_task_id = project_match.group(1) if project_match else ""
    subtasks, subtasks_error = _load_subtasks(
        subtasks_path, expected_task_id
    )
    if subtasks_error:
        problems.append(subtasks_error)
    else:
        roles, _role_issues = _load_roles()
        known_roles = sorted({role.id for role in roles})
        available_text = "、".join(known_roles) or "（无已登记角色）"
        for item in subtasks:
            if not item.agent_role:
                problems.append(
                    f"子任务 {item.id} 缺少 agent_role"
                    f"（每个子任务必须填写已存在角色：{available_text}）"
                )
            elif item.agent_role not in known_roles:
                problems.append(
                    f"子任务 {item.id} 的 agent_role={item.agent_role} 不存在"
                    f"（可用：{available_text}）"
                )
        if not (2 <= len(subtasks) <= 6):
            problems.append(f"子任务数量为 {len(subtasks)}，要求 2–6 个")
    if problems:
        return "；".join(problems), None, subtasks

    title: Optional[str] = None
    if task_sheet.is_file():
        project_name = project_dir.name
        try:
            title = _extract_title(
                task_sheet, project_dir.name.split("_", 1)[0].upper(), project_name
            )
        except OSError:
            title = None
    return "", title, subtasks


def _finalize_registration(
    *,
    task_id: str,
    project_dir: Path,
    workdir: Path,
    idea_hash: str,
    title_hint: str,
    controller: str,
    result: Dict[str, object],
) -> None:
    error, extracted_title, subtasks = _registration_result_error(
        result,
        project_dir=project_dir,
        idea_hash=idea_hash,
        start_epoch=float(result.get("started_epoch") or time.time()),
    )
    log = str(result.get("log") or "")
    run_id = str(result.get("run_id") or "")

    with TASK_WRITE_LOCK:
        meta = _read_task_meta(project_dir, task_id, title=title_hint)
        current_run = str(meta.get("register_run") or "")
        if current_run and run_id and current_run != run_id:
            print(
                f"[登记] 忽略 {task_id} 的旧回调 {run_id}（当前 {current_run}）"
            )
            return
        meta["id"] = task_id
        meta["workdir"] = str(workdir)
        meta["controller"] = controller
        meta["register_run"] = run_id
        meta["idea_sha256"] = idea_hash
        if log:
            meta["register_log"] = log
        if error:
            meta["status"] = REGISTER_FAILED
            meta["register_error"] = error[:4_000]
            if not meta.get("title") and extracted_title:
                meta["title"] = extracted_title
        else:
            meta["status"] = REGISTERED
            meta["registered_at"] = _now_iso()
            meta["register_error"] = ""
            meta["subtask_count"] = str(len(subtasks))
            meta["title"] = (
                title_hint
                or str(meta.get("title") or "").strip()
                or extracted_title
                or ""
            )
        try:
            _write_task_meta(_task_meta_path(project_dir), meta)
        except ConfigError as exc:
            print(f"[登记] {task_id} 写入最终状态失败：{exc}")


def _start_registration(
    *,
    task_id: str,
    project_dir: Path,
    title_hint: str,
    controller: Optional[AgentApp] = None,
) -> Dict[str, object]:
    """在同一把执行器锁下启动总控；立即返回，完成状态由回调写入 task.yaml。"""
    with REGISTRATION_START_LOCK:
        if ACTIVE_REGISTRATIONS:
            raise FormError("已有登记正在运行，请等待完成后再重试。")
        if _active_dispatch_task():
            raise FormError("正在分发实现，请等待分发结束后再登记。")
        if _active_review_task():
            raise FormError("正在审核，请等待审核结束后再登记。")
        active_advance = _active_advance_task()
        if active_advance:
            raise FormError(
                f"{active_advance} 的全自动推进正在运行，"
                "请等待推进结束后再登记。"
            )
        lock = _runner().lock_info()
        if lock.get("active"):
            raise FormError(
                f"执行器正忙（{lock.get('task') or '未知任务'}），"
                "请等待运行结束后再发布或重试登记。"
            )

        meta = _read_task_meta(project_dir, task_id, title=title_hint)
        if str(meta.get("status") or "") == REGISTERED:
            raise FormError(f"{task_id} 已经登记完成，不会重复登记。")
        idea_path = project_dir / DOCKING_DIRNAME / IDEA_NAME
        if not idea_path.is_file():
            raise FormError("任务缺少 对接/idea.md，无法启动总控登记。")
        try:
            idea_hash = hashlib.sha256(idea_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise FormError(f"无法读取 idea.md：{exc.strerror or exc}") from exc
        recorded_hash = str(meta.get("idea_sha256") or "")
        if recorded_hash and recorded_hash != idea_hash:
            raise FormError(
                "idea.md 与发布时的原文不一致（可能已被改写），"
                "为避免丢失人类原文，本次没有启动登记。"
            )
        if not recorded_hash:
            meta["idea_sha256"] = idea_hash
        auto_advance = str(meta.get("auto_advance") or "").strip().lower() in {
            "1",
            "on",
            "true",
            "yes",
        }

        resolution = (
            ControllerResolution(
                agent=controller,
                source=f"指定总控 → {controller.name}（{controller.id}）",
                note="本次使用已解析的总控 Agent。",
            )
            if controller is not None
            else _resolve_controller()
        )
        if resolution.agent is None or not _agent_dispatch_ready(resolution.agent):
            raise FormError(resolution.note or "没有可用总控。")
        agent = resolution.agent
        workdir = Path(str(meta.get("workdir") or _default_workdir(project_dir)))
        prompt = _registration_prompt(
            task_id=task_id,
            project_name=project_dir.name,
            project_dir=project_dir,
            workdir=workdir,
            title_hint=title_hint,
        )

        meta["status"] = REGISTERING
        meta["controller"] = agent.id
        meta["register_started_at"] = _now_iso()
        meta["register_error"] = ""
        meta["register_run"] = ""
        meta["register_log"] = ""
        try:
            _write_task_meta(_task_meta_path(project_dir), meta)
        except ConfigError as exc:
            raise FormError(f"写入 {TASK_META_NAME} 失败：{exc}") from exc

        ACTIVE_REGISTRATIONS.add(task_id)
        try:
            spec = runner_engine.AgentSpec(
                id=agent.id,
                name=agent.name,
                command=agent.command,
                args_template=agent.args_template,
            )

            def on_finish(result: Dict[str, object]) -> None:
                should_auto_advance = False
                try:
                    try:
                        _finalize_registration(
                            task_id=task_id,
                            project_dir=project_dir,
                            workdir=workdir,
                            idea_hash=idea_hash,
                            title_hint=title_hint,
                            controller=agent.id,
                            result=result,
                        )
                        if auto_advance:
                            current = _read_task_meta(
                                project_dir, task_id, title=title_hint
                            )
                            should_auto_advance = (
                                str(current.get("status") or "") == REGISTERED
                            )
                    except Exception as exc:
                        traceback.print_exc()
                        _mark_registration_failed(
                            project_dir,
                            task_id,
                            f"登记收尾失败：{exc}",
                            str(result.get("log") or ""),
                            controller=agent.id,
                        )
                finally:
                    with REGISTRATION_START_LOCK:
                        ACTIVE_REGISTRATIONS.discard(task_id)
                    if should_auto_advance:
                        _schedule_auto_advance(task_id)

            snapshot = _runner().launch_registration(
                task_id=task_id,
                task_dir=project_dir,
                agent=spec,
                prompt=prompt,
                timeout_seconds=REGISTRATION_TIMEOUT_SECONDS,
                on_finish=on_finish,
            )
        except FormError as exc:
            with REGISTRATION_START_LOCK:
                ACTIVE_REGISTRATIONS.discard(task_id)
            _mark_registration_failed(
                project_dir,
                task_id,
                f"总控登记启动失败：{exc}",
                "",
                controller=agent.id,
            )
            raise
        except Exception as exc:
            with REGISTRATION_START_LOCK:
                ACTIVE_REGISTRATIONS.discard(task_id)
            _mark_registration_failed(
                project_dir,
                task_id,
                f"总控未能启动：{exc}",
                "",
                controller=agent.id,
            )
            raise FormError(f"总控未能启动：{exc}") from exc

        with TASK_WRITE_LOCK:
            current = _read_task_meta(project_dir, task_id, title=title_hint)
            if (
                str(current.get("status") or "") == REGISTERING
                and not str(current.get("register_run") or "")
            ):
                current["register_run"] = str(snapshot.get("run_id") or "")
                current["register_log"] = str(snapshot.get("log") or "")
                try:
                    _write_task_meta(_task_meta_path(project_dir), current)
                except ConfigError:
                    pass
        return snapshot


def _find_registration_project(task_id: str) -> Optional[Path]:
    """按编号找活动项目；登记阶段可能还没有任务单，故不依赖看板扫描。"""
    if not TASK_ID_RE.fullmatch(task_id):
        return None
    best: Optional[Tuple[float, Path]] = None
    if not PROJECTS_DIR.is_dir():
        return None
    try:
        entries = list(PROJECTS_DIR.iterdir())
    except OSError:
        return None
    for entry in entries:
        match = PROJECT_NAME_RE.fullmatch(entry.name)
        if not match or match.group(1).upper() != task_id.upper():
            continue
        if not entry.is_dir() or entry.is_symlink():
            continue
        stamp = 0.0
        for candidate in (
            entry / DOCKING_DIRNAME / TASK_META_NAME,
            entry / DOCKING_DIRNAME / IDEA_NAME,
            entry / DOCKING_DIRNAME / TASK_SHEET_NAME,
        ):
            try:
                stamp = max(stamp, candidate.stat().st_mtime)
            except OSError:
                continue
        if best is None or stamp > best[0]:
            best = (stamp, entry)
    return best[1] if best else None


def _reconcile_stale_registrations() -> int:
    """服务启动时收尾上次进程遗留的 registering；不碰活锁对应的登记。"""
    try:
        if _runner().lock_info().get("active"):
            return 0
    except Exception:
        return 0
    if not PROJECTS_DIR.is_dir():
        return 0
    repaired = 0
    try:
        entries = list(PROJECTS_DIR.iterdir())
    except OSError:
        return 0
    for project_dir in entries:
        match = PROJECT_NAME_RE.fullmatch(project_dir.name)
        if not match or not project_dir.is_dir() or project_dir.is_symlink():
            continue
        task_id = match.group(1).upper()
        meta_path = _task_meta_path(project_dir)
        if not meta_path.is_file():
            continue
        meta = _read_task_meta(project_dir, task_id)
        if str(meta.get("status") or "") != REGISTERING:
            continue
        _mark_registration_failed(
            project_dir,
            task_id,
            "控制台上次退出前没有完成登记收尾；请重试登记。",
            str(meta.get("register_log") or ""),
            controller=str(meta.get("controller") or ""),
        )
        repaired += 1
    return repaired


def _reconcile_interrupted_subtasks() -> int:
    """启动时收尾崩溃遗留：无活锁/无对应进程的 running 子任务置 failed。

    · 有活锁（runner.lock 指向仍存活进程）的任务不动；
    · 内存里仍有分发/审核/推进/体检线程的任务不动；
    · 只改 status/error/finished_at，log/run_id 原样保留（缺失时从
      runner.status 的本轮记录补齐），恢复后「继续分发」会选中 failed 项。
    """
    if not PROJECTS_DIR.is_dir():
        return 0
    try:
        lock = _runner().lock_info()
    except Exception:
        lock = {}
    lock_active = bool(lock.get("active"))
    lock_task = str(lock.get("task") or "").upper() if lock_active else ""
    if lock_active and not lock_task:
        # 锁刚创建但内容未写全：无法判断归属，宁可不动。
        return 0
    live_tasks = {
        value.upper()
        for value in (
            _active_dispatch_task(),
            _active_review_task(),
            _active_advance_task(),
            _active_handoff_task(),
        )
        if value
    }
    if lock_task:
        live_tasks.add(lock_task)
    try:
        status = _runner().read_status()
    except Exception:
        status = {}
    status_task = str(status.get("task") or "").upper()
    status_subtask = str(status.get("subtask") or "")
    status_log = str(status.get("log") or "")
    status_run_id = str(status.get("run_id") or "")

    repaired = 0
    try:
        entries = list(PROJECTS_DIR.iterdir())
    except OSError:
        return 0
    for project_dir in entries:
        match = PROJECT_NAME_RE.fullmatch(project_dir.name)
        if not match or not project_dir.is_dir() or project_dir.is_symlink():
            continue
        task_id = match.group(1).upper()
        if task_id in live_tasks:
            continue
        subtasks_path = project_dir / DOCKING_DIRNAME / SUBTASKS_NAME
        subtasks, error = _load_subtasks(subtasks_path, task_id)
        if error or not subtasks:
            continue
        task_repaired = 0
        for item in subtasks:
            if item.status != "running":
                continue
            updates = {
                "status": "failed",
                "error": "interrupted",
                "finished_at": _now_iso(),
            }
            if not item.log and status_task == task_id and (
                not status_subtask or status_subtask == item.id
            ):
                if status_log:
                    updates["log"] = status_log
            if not item.run_id and status_task == task_id and (
                not status_subtask or status_subtask == item.id
            ):
                if status_run_id:
                    updates["run_id"] = status_run_id
            try:
                _update_subtask_record(subtasks_path, item.id, updates)
                repaired += 1
                task_repaired += 1
            except Exception:
                traceback.print_exc()
        if task_repaired:
            state = _read_dispatch_state_file(task_id)
            if str(state.get("state") or "") == "running":
                state["state"] = "failed"
                state["finished_at"] = _now_iso()
                state["message"] = (
                    "上次分发没有正常收尾；running 子任务已标记为 failed"
                    "（interrupted），可继续分发重试。"
                )
                try:
                    _write_dispatch_state_file(state)
                except Exception:
                    traceback.print_exc()
    return repaired


def _registration_payload(task_id: str) -> Dict[str, object]:
    project_dir = _find_registration_project(task_id)
    if project_dir is None:
        raise ActionError(
            404,
            "没有找到这次发布",
            f"{task_id} 不在活动项目目录中。",
            "回到看板确认任务编号，或重新发布。",
        )
    meta = _read_task_meta(project_dir, task_id)
    status = str(meta.get("status") or "")
    if status not in {REGISTERING, REGISTERED, REGISTER_FAILED}:
        status = REGISTER_FAILED
    subtasks, _error = _load_subtasks(
        project_dir / DOCKING_DIRNAME / SUBTASKS_NAME, task_id
    )
    task_sheet = project_dir / DOCKING_DIRNAME / TASK_SHEET_NAME
    title = str(meta.get("title") or "")
    if task_sheet.is_file() and not title:
        try:
            title = _extract_title(
                task_sheet, task_id, project_dir.name
            )
        except OSError:
            pass
    return {
        "ok": True,
        "id": task_id,
        "project": project_dir.name,
        "status": status,
        "status_label": REGISTER_STATUS_LABELS.get(status, status),
        "title": title,
        "controller": str(meta.get("controller") or ""),
        "error": str(meta.get("register_error") or ""),
        "log": str(meta.get("register_log") or ""),
        "started_at": str(meta.get("register_started_at") or ""),
        "registered_at": str(meta.get("registered_at") or ""),
        "subtask_count": len(subtasks) if subtasks else int(meta.get("subtask_count") or 0),
        "auto_advance": str(meta.get("auto_advance") or "").strip().lower()
        in {"1", "on", "true", "yes"},
        "advance_status_url": f"/api/task/{task_id}/advance-status",
        "task_url": f"/task/{task_id}" if task_sheet.is_file() else "",
        "retry_url": f"/register/{task_id}/retry",
        "can_retry": status == REGISTER_FAILED,
        "poll_after_ms": REGISTER_POLL_INTERVAL_MS if status == REGISTERING else 0,
    }


def _state_line_count() -> int:
    try:
        with WATCH_STATE.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def _tail_lines(path: Path, count: int) -> List[str]:
    if not path.is_file():
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

    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # 丢掉可能被截断的首行
    return lines[-count:]


def _load_guard_status() -> Dict[str, str]:
    """只读 guard.status；网页不启动、不停止守卫进程。

    文件由菜单栏应用写入，格式为 key=value：
      state=running|stopped
      updated=<本地时间>
      updated_epoch=<epoch 秒>
      app_pid=<菜单栏应用 PID>
      watch_pid=<当前 watch.sh PID 或 ->
      note=<说明>
    """
    values: Dict[str, str] = {
        "state": "unknown",
        "updated": "",
        "updated_epoch": "",
        "watch_pid": "",
        "note": "",
    }
    if not GUARD_STATUS.is_file():
        values["note"] = "等待菜单栏应用写入 guard.status"
        return values
    try:
        text = _read_text(GUARD_STATUS)
    except OSError:
        values["note"] = "无法读取 guard.status"
        return values

    raw: Dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            raw[key.strip()] = value.strip()

    state = raw.get("state", "")
    values["state"] = state if state in {"running", "stopped"} else "unknown"
    values["updated"] = raw.get("updated", "")
    values["updated_epoch"] = raw.get("updated_epoch", "")
    values["watch_pid"] = raw.get("watch_pid", "")
    values["note"] = raw.get("note", "")
    if not values["updated"] and values["updated_epoch"].isdigit():
        values["updated"] = _fmt_time(float(values["updated_epoch"]))
    return values


# ---------------------------------------------------------------------------
# 执行器视图（读取 runner.status / lock / history；只由 POST /run 触发）
# ---------------------------------------------------------------------------

RUN_STATE_LABELS = {
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
    "timeout": "超时",
    "stale": "中断",
    "idle": "空闲",
}

RUN_STATE_TONES = {
    "running": "running",
    "success": "passed",
    "failed": "failed",
    "timeout": "timeout",
    "stale": "failed",
    "idle": "idle",
}


def _run_state_label(state: object) -> str:
    return RUN_STATE_LABELS.get(str(state or "idle"), str(state or "idle"))


def _run_state_tone(state: object) -> str:
    return RUN_STATE_TONES.get(str(state or "idle"), "idle")


def _format_duration(value: object) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "—"
    if seconds < 0:
        return "—"
    if seconds < 10:
        return f"{seconds:.1f} 秒"
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    whole = int(seconds)
    minutes, secs = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes} 分 {secs:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分"


def _format_exit(value: object) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


# ---------------------------------------------------------------------------
# 子任务分发：按 agent_role 绑定串行执行
# ---------------------------------------------------------------------------

DISPATCH_STATE_NAMES = {
    "idle": "未开始",
    "running": "分发中",
    "completed": "已完成",
    "failed": "已停止",
}
DISPATCH_STATE_TONES = {
    "idle": "idle",
    "running": "running",
    "completed": "passed",
    "failed": "failed",
}


def _dispatch_state_path(task_id: str) -> Path:
    return DISPATCH_DIR / f"dispatch-{task_id.upper()}.status.json"


def _write_dispatch_state_file(state: Dict[str, object]) -> None:
    """尽力持久化分发状态；页面轮询失败不应打断执行。"""
    task_id = str(state.get("task") or "")
    if not task_id:
        return
    path = _dispatch_state_path(task_id)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    except OSError:
        traceback.print_exc()
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _read_dispatch_state_file(task_id: str) -> Dict[str, object]:
    path = _dispatch_state_path(task_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if str(data.get("task") or "").upper() != task_id.upper():
        return {}
    return data


def _active_dispatch_task() -> str:
    with DISPATCH_STATE_LOCK:
        if ACTIVE_DISPATCH is None:
            return ""
        return str(ACTIVE_DISPATCH.get("task") or "")


def _dispatch_state_snapshot(task_id: str) -> Dict[str, object]:
    task_id = task_id.upper()
    with DISPATCH_STATE_LOCK:
        if ACTIVE_DISPATCH is not None and str(
            ACTIVE_DISPATCH.get("task") or ""
        ).upper() == task_id:
            state = copy.deepcopy(ACTIVE_DISPATCH.get("state") or {})
            assert isinstance(state, dict)
            return state
    state = _read_dispatch_state_file(task_id)
    if not state:
        return {}
    if str(state.get("state") or "") == "running":
        try:
            lock = _runner().lock_info()
        except Exception:
            lock = {}
        if not lock.get("active") or str(lock.get("task") or "").upper() != task_id:
            state = dict(state)
            state["state"] = "failed"
            state["message"] = (
                "上次分发没有正常收尾（控制台重启或线程中断）；"
                "待开始子任务仍保留 pending，可重新分发。"
            )
    return state


def _dispatch_preflight(
    pending: Tuple[Subtask, ...],
    roles: List[Role],
    agents: List[AgentApp],
) -> Tuple[Dict[str, Dict[str, object]], List[str]]:
    """检查所有 pending 子任务的角色绑定；返回绑定表和完整问题清单。"""
    role_map = {role.id: role for role in roles}
    agent_map = {agent.id: agent for agent in agents}
    bindings: Dict[str, Dict[str, object]] = {}
    violations: List[str] = []
    for item in pending:
        role_id = item.agent_role.strip()
        if not role_id:
            violations.append(f"{item.id}：未填写 agent_role")
            continue
        role = role_map.get(role_id)
        if role is None:
            violations.append(f"{item.id}：角色 {role_id} 不存在")
            continue
        label = f"角色 {role.name}（{role.id}）"
        if not role.enabled:
            violations.append(f"{item.id}：{label}已停用")
            continue
        if not role.agent_id:
            violations.append(f"{item.id}：{label}未绑定 Agent")
            continue
        agent = agent_map.get(role.agent_id)
        if agent is None:
            violations.append(
                f"{item.id}：{label}绑定的 Agent {role.agent_id} 未登记"
            )
            continue
        if not agent.installed:
            violations.append(
                f"{item.id}：{label}绑定的 Agent {agent.id} 未安装（installed=false）"
            )
            continue
        if not agent.currently_executable:
            violations.append(
                f"{item.id}：{label}绑定的 Agent {agent.id} 命令当前不可执行"
            )
            continue
        bindings[item.id] = {"role": role, "agent": agent}
    return bindings, violations


def _markdown_section_text(text: str, keywords: Tuple[str, ...]) -> str:
    """取标题含关键词的 Markdown 章节原文；遇到同级/更高级标题结束。"""
    if not text:
        return ""
    collected: List[str] = []
    in_section = False
    level = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        heading = re.match(r"^(#{1,6})\s*(.+?)\s*$", line)
        bold_heading = (
            re.match(r"^\*\*(.+?)\*\*\s*$", line) if line.startswith("**") else None
        )
        title = heading or bold_heading
        if title is not None:
            new_level = len(heading.group(1)) if heading else 7
            label = (
                heading.group(2) if heading else bold_heading.group(1)
            ).strip()
            matched = any(keyword in label for keyword in keywords)
            if in_section and new_level <= level:
                if matched:
                    level = new_level
                    continue
                break
            if not in_section and matched:
                in_section = True
                level = new_level
            continue
        if in_section:
            collected.append(raw_line)
    return "\n".join(collected).strip()


def _task_sheet_prohibitions(task: Task) -> str:
    """父任务单「禁止事项」全文；供修订提示词原样传递。"""
    try:
        text = _read_text(task.path)
    except OSError:
        return "（任务单不可读）"
    section = _markdown_section_text(text, ("禁止",))
    return section or "（任务单未写禁止事项）"


def _dispatch_revision_block(
    context: Dict[str, object], subtask: Subtask
) -> str:
    """修订上下文：父任务单/禁止事项、问题全文、审查快照与轮次、回归验收。"""
    round_no = str(context.get("round") or "?")
    sheet = str(context.get("parent_task_sheet") or "")
    prohibitions = str(context.get("prohibitions") or "（未提供）").strip()
    issues = str(context.get("issues_full") or "").strip()
    snapshot = str(context.get("review_snapshot") or "")
    related = [
        line
        for line in issues.splitlines()
        if subtask.id.lower() in line.lower()
    ]
    related_text = "\n".join(related).strip() or (
        "（问题清单没有点名本子任务；仍须按下方全文逐条自查）"
    )
    lines = [
        "",
        f"=== 修订上下文 · 第 {round_no} 轮（必须先读，优先级高于普通建议） ===",
        f"- 父任务单（绝对路径）：{sheet}",
        "- 父任务单「禁止事项」全文：",
        prohibitions,
        f"- 本轮审查中与本子任务（{subtask.id}）直接相关的问题（原文）：",
        related_text,
        "- 本轮审查问题清单全文（原文）：",
        issues or "（审查未提供问题清单）",
        f"- 上一轮审查快照（绝对路径）：{snapshot}",
        f"- 审查轮次：第 {round_no} 轮",
        "- 回归验收要求：",
        f"  · 重做后必须重新满足本子任务验收标准：{subtask.acceptance or '（未填写）'}",
        "  · 在 DONE.md 的「怎么验」写清本轮复跑命令与结果，并逐条对应上面的问题",
        "  · 不得回退上一轮已通过的其它子任务产物，不得缩小原有验收范围",
        "  · 只写本子任务输出目录；发现问题无法在本子任务内解决时在 DONE.md 注明",
    ]
    return "\n".join(lines)


def _dispatch_prompt(
    role: Role,
    subtask: Subtask,
    workdir: Path,
    *,
    output_dir: Optional[Path] = None,
    revise_context: Optional[Dict[str, object]] = None,
    attempt: int = 0,
) -> str:
    output = (
        Path(output_dir).resolve()
        if output_dir is not None
        else (Path(workdir) / "子任务" / subtask.id).resolve()
    )
    done_path = output / DONE_NAME
    prompt = (
        f"你是 {role.name}。执行子任务 {subtask.id}：{subtask.title}\n"
        f"目标：{subtask.goal or '（未填写）'} · 验收：{subtask.acceptance or '（未填写）'}\n"
        f"工作目录（cwd）：{workdir}\n"
        f"输出目录（绝对路径）：{output}\n"
        f"完成后写 DONE.md（绝对路径）：{done_path}"
        f"（含：做了什么/怎么验/结果）\n"
        "★ 所有产物与 DONE.md 只能写在上述输出目录内；不要再套一层 工作区/。\n"
        "★ 不得写 对接/审查.md；不得改其它子任务；不删文件"
    )
    if attempt > 0:
        prompt += (
            f"\n★ 本次分发记录：attempt: {attempt}。DONE.md 必须原样包含一行 "
            f"`attempt: {attempt}`（与本次分发一致）；若另写 `run_id:`，"
            "也必须与本次运行一致。缺少或不匹配会被 G2 判为缺件。"
        )
    if revise_context:
        prompt += _dispatch_revision_block(revise_context, subtask)
    return prompt


def _dispatch_targets(task: Task) -> Tuple[Subtask, ...]:
    """本次分发目标：pending 与 failed；delivered/passed 永不重跑。"""
    return tuple(
        item for item in task.subtasks if item.status in {"pending", "failed"}
    )


def _dispatch_action_label(task: Task) -> str:
    pending = sum(1 for item in task.subtasks if item.status == "pending")
    failed = sum(1 for item in task.subtasks if item.status == "failed")
    delivered = sum(
        1 for item in task.subtasks if item.status in {"delivered", "passed"}
    )
    if failed and pending:
        return f"继续分发（余 {pending} · 重试 {failed}）"
    if failed:
        return f"继续分发（重试 {failed}）"
    if pending and delivered:
        return f"继续分发（余 {pending}）"
    return "开始分发实现"


def _dispatch_availability(task: Task) -> Tuple[bool, str]:
    if task.archived:
        return False, "归档任务只读，不能分发。"
    if _handoff_blocked_for(task.task_id):
        return False, "交接体检未通过；请先点「重试体检」或人工补齐缺件。"
    if _active_handoff_task():
        return False, "交接体检或自动补交正在进行，请等待结束。"
    if not task.subtasks_path.is_file():
        return False, "没有 对接/subtasks.yaml，请先登记拆分任务。"
    if task.subtasks_error:
        return False, f"subtasks.yaml 无法解析：{task.subtasks_error}"
    if not task.subtasks:
        return False, "subtasks.yaml 没有列出子任务。"
    if _review_active_for(task.task_id):
        return False, "审核正在运行，请等待审核结束后再分发。"
    active_advance = _active_advance_task()
    if active_advance:
        return False, f"{active_advance} 的全自动推进正在进行，请等待推进结束。"
    targets = _dispatch_targets(task)
    if not targets:
        delivered = _subtasks_all_delivered(task)
        if delivered:
            return False, "没有待开始或失败的子任务；下一步是开始审核。"
        return False, "没有待开始或失败的子任务，无法分发。"
    active = _active_dispatch_task()
    if active == task.task_id:
        return False, "正在分发实现，请等待当前子任务完成。"
    if active:
        return False, f"执行器正在为 {active} 分发实现，同一时刻只允许一个分发。"
    if ACTIVE_REGISTRATIONS:
        return False, "总控登记正在运行，请等待登记结束后再分发。"
    try:
        lock = _runner().lock_info()
    except Exception:
        lock = {}
    if lock.get("active"):
        return False, (
            f"执行器正忙（{lock.get('task') or '未知任务'}），"
            "请等待当前运行结束。"
        )
    pending = sum(1 for item in targets if item.status == "pending")
    failed = sum(1 for item in targets if item.status == "failed")
    if failed and pending:
        return True, (
            f"{pending} 个待开始 + {failed} 个失败子任务；"
            "失败项会重置为 pending 后按角色绑定继续分发。"
        )
    if failed:
        return True, f"{failed} 个失败子任务会重置为 pending 后按角色绑定重试。"
    delivered = sum(
        1 for item in task.subtasks if item.status in {"delivered", "passed"}
    )
    prefix = "继续分发" if delivered else "首次分发"
    return True, f"{prefix}：{pending} 个待开始子任务将按角色绑定串行分发。"


def _dispatch_commit(dispatch_state: Dict[str, object], **updates: object) -> None:
    with DISPATCH_STATE_LOCK:
        dispatch_state.update(updates)
        _write_dispatch_state_file(dispatch_state)


def _dispatch_fail(
    state: Dict[str, object],
    subtask: Subtask,
    *,
    error: str,
    log: str = "",
    started_at: str = "",
    stopped_index: int = 0,
) -> None:
    """记录失败并停止后续；调用方负责已写入的 YAML 状态。"""
    finished_at = _now_iso()
    result = {
        "id": subtask.id,
        "title": subtask.title,
        "role": subtask.agent_role,
        "state": "failed",
        "log": log,
        "error": error[:2_000],
        "started_at": started_at,
        "finished_at": finished_at,
    }
    with DISPATCH_STATE_LOCK:
        results = state.get("results")
        if not isinstance(results, list):
            results = []
            state["results"] = results
        results.append(result)
        state["failed"] = int(state.get("failed") or 0) + 1
        state["done"] = int(state.get("done") or 0) + 1
        if stopped_index:
            state["current_index"] = stopped_index
        state["current_subtask"] = subtask.id
        state["current_title"] = subtask.title
        state["state"] = "failed"
        state["finished_at"] = finished_at
        state["message"] = (
            f"子任务 {subtask.id} 失败，已停止后续分发：{error[:220]}"
        )
        _write_dispatch_state_file(state)


def _dispatch_worker(
    task: Task,
    runs: Tuple[Subtask, ...],
    bindings: Dict[str, Dict[str, object]],
    state: Dict[str, object],
    revise_context: Optional[Dict[str, object]] = None,
) -> None:
    global ACTIVE_DISPATCH
    try:
        for index, subtask in enumerate(runs, start=1):
            binding = bindings.get(subtask.id) or {}
            role = binding.get("role")
            agent = binding.get("agent")
            if not isinstance(role, Role) or not isinstance(agent, AgentApp):
                _dispatch_fail(
                    state,
                    subtask,
                    error="角色绑定在分发开始后失效，未启动进程。",
                    stopped_index=index,
                )
                return
            _dispatch_commit(
                state,
                current_index=index,
                current_subtask=subtask.id,
                current_title=subtask.title,
                message=(
                    f"分发中… 当前子任务 {subtask.id} {subtask.title}"
                ),
            )
            started_at = _now_iso()
            attempt = int(getattr(subtask, "attempt", 0) or 0) + 1
            try:
                _update_subtask_record(
                    task.subtasks_path,
                    subtask.id,
                    {
                        "status": "running",
                        "started_at": started_at,
                        "finished_at": "",
                        "error": "",
                        "log": "",
                        "run_id": "",
                        "attempt": str(attempt),
                    },
                )
            except Exception as exc:
                _dispatch_fail(
                    state,
                    subtask,
                    error=f"写入 subtasks.yaml 失败：{exc}",
                    started_at=started_at,
                    stopped_index=index,
                )
                return

            spec = runner_engine.AgentSpec(
                id=agent.id,
                name=agent.name,
                command=agent.command,
                args_template=agent.args_template,
            )
            log_prefix = "dispatch-" + (
                re.sub(r"[^A-Za-z0-9_-]+", "_", subtask.id)[:40] or "subtask"
            )
            output_dir = _subtask_output_dir(task, subtask)
            try:
                runner = _runner()
                launched = runner.launch(
                    task_id=task.task_id,
                    task_dir=task.project_dir,
                    agent=spec,
                    round_no=1,
                    workdir=task.workdir,
                    prompt=_dispatch_prompt(
                        role,
                        subtask,
                        task.workdir,
                        output_dir=output_dir,
                        revise_context=revise_context,
                        attempt=attempt,
                    ),
                    kind="dispatch",
                    subtask_id=subtask.id,
                    subtask_title=subtask.title,
                    log_prefix=log_prefix,
                )
                # 先落 run_id/log：控制台崩溃后恢复时据此保留现场。
                try:
                    _update_subtask_record(
                        task.subtasks_path,
                        subtask.id,
                        {
                            "log": str(launched.get("log") or ""),
                            "run_id": str(launched.get("run_id") or ""),
                        },
                    )
                except Exception:
                    traceback.print_exc()
            except Exception as exc:
                error = f"未能启动 Agent：{exc}"
                try:
                    _update_subtask_record(
                        task.subtasks_path,
                        subtask.id,
                        {
                            "status": "failed",
                            "error": error[:2_000],
                            "finished_at": _now_iso(),
                            "log": "",
                            "run_id": "",
                        },
                    )
                except Exception:
                    traceback.print_exc()
                _dispatch_fail(
                    state,
                    subtask,
                    error=error,
                    started_at=started_at,
                    stopped_index=index,
                )
                return

            finished = runner.wait_for_idle(runner.timeout_seconds + 120.0)
            final = runner.snapshot()
            if not finished:
                result_state = "failed"
                error = "等待子任务收尾超时；请检查执行器状态。"
            else:
                result_state = str(final.get("state") or "failed")
                error = str(final.get("error") or "")
                if result_state != "success" and not error:
                    error = f"Agent 以 {result_state} 状态结束。"
            log = str(final.get("log") or "")
            run_id = str(final.get("run_id") or "")
            delivered = result_state == "success"
            finished_at = _now_iso()
            try:
                updates = {
                    "status": "delivered" if delivered else "failed",
                    "log": log,
                    "finished_at": finished_at,
                    "error": "" if delivered else error[:2_000],
                }
                if run_id:
                    updates["run_id"] = run_id
                _update_subtask_record(
                    task.subtasks_path,
                    subtask.id,
                    updates,
                )
            except Exception as exc:
                _dispatch_fail(
                    state,
                    subtask,
                    error=f"子任务已结束，但写入 subtasks.yaml 失败：{exc}",
                    log=log,
                    started_at=started_at,
                    stopped_index=index,
                )
                return

            if not delivered:
                _dispatch_fail(
                    state,
                    subtask,
                    error=error or "Agent 执行失败。",
                    log=log,
                    started_at=started_at,
                    stopped_index=index,
                )
                return

            result = {
                "id": subtask.id,
                "title": subtask.title,
                "role": role.id,
                "state": "delivered",
                "log": log,
                "error": "",
                "started_at": started_at,
                "finished_at": finished_at,
            }
            with DISPATCH_STATE_LOCK:
                results = state.get("results")
                if not isinstance(results, list):
                    results = []
                    state["results"] = results
                results.append(result)
                state["success"] = int(state.get("success") or 0) + 1
                state["done"] = int(state.get("done") or 0) + 1
                _write_dispatch_state_file(state)

        finished_at = _now_iso()
        success = int(state.get("success") or 0)
        failed = int(state.get("failed") or 0)
        _dispatch_commit(
            state,
            state="completed",
            finished_at=finished_at,
            message=f"分发完成：成功 {success} / 失败 {failed}",
            current_subtask="",
            current_title="",
        )
    except Exception as exc:
        traceback.print_exc()
        try:
            with DISPATCH_STATE_LOCK:
                state["state"] = "failed"
                state["finished_at"] = _now_iso()
                state["message"] = f"分发线程异常：{exc}"
                _write_dispatch_state_file(state)
        except Exception:
            traceback.print_exc()
    finally:
        with DISPATCH_STATE_LOCK:
            if ACTIVE_DISPATCH is not None and (
                ACTIVE_DISPATCH.get("state") is state
            ):
                ACTIVE_DISPATCH = None


def _start_dispatch(
    task: Task,
    *,
    _advance_owned: bool = False,
    revise_context: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """预检 pending/failed 子任务后启动分发线程；失败时不启动任何进程。"""
    global ACTIVE_DISPATCH
    with DISPATCH_START_LOCK:
        if _advance_owned and _advance_stop_requested():
            raise ActionError(
                409,
                "推进已请求停止",
                "用户已请求停止全自动推进；本次没有启动任何子任务。",
                "等待停止完成后刷新页面。",
            )
        if not _advance_owned:
            active_advance = _active_advance_task()
            if active_advance:
                raise ActionError(
                    409,
                    "全自动推进正在运行",
                    f"{active_advance} 的全自动推进占用执行器；本次没有启动任何进程。",
                    "等待推进结束后再手动分发。",
                )
        with _task_lifecycle_lock(task.task_id):
            current = _find_task(task.task_id)
            if current is None:
                raise ActionError(
                    409,
                    "任务正在移动或已归档",
                    f"{task.task_id} 已不在 项目/ 中，本次没有启动任何进程。",
                    "刷新看板；归档任务请先恢复。",
                )
            if current.archived:
                raise ActionError(
                    409,
                    "归档任务只读",
                    f"{task.task_id} 位于 归档/，不能分发实现。",
                    "先从归档库恢复项目。",
                )
            task = current
        if ACTIVE_DISPATCH is not None:
            active_task = str(ACTIVE_DISPATCH.get("task") or "")
            raise ActionError(
                409,
                "已有分发在跑",
                f"{active_task or '另一个任务'} 正在分发实现；"
                "同一时刻只允许一个分发。",
                "等待当前分发结束，或到 /runs 查看运行状态。",
            )
        try:
            lock = _runner().lock_info()
        except Exception:
            lock = {}
        if ACTIVE_REVIEW is not None:
            active_review_task = str(ACTIVE_REVIEW.get("task") or "")
            raise ActionError(
                409,
                "审核正在运行",
                f"{active_review_task or '另一个任务'} 正在审核；"
                "同一时刻只允许一个执行器写手，本次没有启动任何进程。",
                "等待审核结束后再分发。",
            )
        if ACTIVE_REGISTRATIONS:
            raise ActionError(
                409,
                "总控登记正在运行",
                "已有总控登记占用执行器；本次没有启动任何进程。",
                "等待登记结束后再分发。",
            )
        if lock.get("active"):
            raise ActionError(
                409,
                "执行器正忙",
                f"执行器正在运行 {lock.get('task') or '未知任务'}；"
                "本次没有启动任何进程。",
                "等待当前运行结束后再分发。",
            )
        if task.archived:
            raise ActionError(
                409,
                "归档任务只读",
                f"{task.task_id} 位于 归档/，不能分发实现。",
                "先从归档库恢复项目。",
            )
        _ensure_handoff_not_blocked(task)
        if _registration_aware(task) and _registration_structure_incomplete(task):
            handoff = _run_handoff_gates(
                task,
                "dispatch",
                source="advance" if _advance_owned else "manual",
            )
            if not bool(handoff.get("passed")):
                raise _handoff_action_error(handoff)
            refreshed = _find_task(task.task_id)
            if refreshed is not None:
                task = refreshed
        if not task.subtasks_path.is_file():
            raise ActionError(
                400,
                "没有子任务清单",
                f"{task.task_id} 缺少 对接/subtasks.yaml，未启动任何进程。",
                "先完成总控登记，生成子任务清单后再分发。",
            )
        subtasks, error = _load_subtasks(task.subtasks_path, task.task_id)
        if error:
            raise ActionError(
                400,
                "子任务清单无法解析",
                f"{error} 未启动任何进程。",
                "修正 对接/subtasks.yaml 后重试。",
            )
        if not subtasks:
            raise ActionError(
                400,
                "没有子任务",
                "对接/subtasks.yaml 没有列出子任务，未启动任何进程。",
                "先完成总控登记，生成子任务清单后再分发。",
            )
        pending = tuple(
            item for item in subtasks if item.status in {"pending", "failed"}
        )
        if not pending:
            raise ActionError(
                400,
                "没有待开始或失败的子任务",
                "所有子任务都是 delivered/passed，未启动任何进程。",
                "查看子任务表格；下一步是开始审核。",
            )

        roles, _role_issues = _load_roles()
        agents, _agent_issues = _load_agents()
        bindings, violations = _dispatch_preflight(pending, roles, agents)
        if violations:
            raise ActionError(
                409,
                "分发预检未通过",
                "以下子任务的角色绑定不满足要求，未启动任何进程："
                + "；".join(violations),
                "到 /roles 绑定可执行 Agent，或修正 subtasks.yaml 的 agent_role。",
                violations=violations,
            )

        handoff = _run_handoff_gates(
            task,
            "dispatch",
            source="advance" if _advance_owned else "manual",
        )
        if not bool(handoff.get("passed")):
            raise _handoff_action_error(handoff)

        with _task_lifecycle_lock(task.task_id):
            latest = _find_task(task.task_id)
            if latest is None:
                raise ActionError(
                    409,
                    "任务正在移动或已归档",
                    f"{task.task_id} 已不在 项目/ 中，本次没有启动任何进程。",
                    "刷新看板；归档任务请先恢复。",
                )
            if latest.archived:
                raise ActionError(
                    409,
                    "归档任务只读",
                    f"{task.task_id} 位于 归档/，不能分发实现。",
                    "先从归档库恢复项目。",
                )
            task = latest
            if ACTIVE_DISPATCH is not None:
                active_task = str(ACTIVE_DISPATCH.get("task") or "")
                raise ActionError(
                    409,
                    "已有分发在跑",
                    f"{active_task or '另一个任务'} 正在分发实现；"
                    "同一时刻只允许一个分发。",
                    "等待当前分发结束，或到 /runs 查看运行状态。",
                )
            if not _advance_owned:
                active_advance = _active_advance_task()
                if active_advance:
                    raise ActionError(
                        409,
                        "全自动推进正在运行",
                        f"{active_advance} 的全自动推进占用执行器；"
                        "本次没有启动任何进程。",
                        "等待推进结束后再手动分发。",
                    )
            if ACTIVE_REGISTRATIONS:
                raise ActionError(
                    409,
                    "总控登记正在运行",
                    "已有总控登记占用执行器；本次没有启动任何进程。",
                    "等待登记结束后再分发。",
                )
            if _handoff_blocked_for(task.task_id):
                raise ActionError(
                    409,
                    "交接体检未通过",
                    "交接体检已阻塞，本次没有启动任何进程。",
                    "先补齐缺件或重试体检。",
                )
            active_handoff = _active_handoff_task()
            if active_handoff:
                raise ActionError(
                    409,
                    "交接体检正在运行",
                    f"{active_handoff} 正在检查/补交交接文件，本次没有启动任何进程。",
                    "等待交接体检结束后再分发。",
                )
            if ACTIVE_REVIEW is not None:
                active_review_task = str(ACTIVE_REVIEW.get("task") or "")
                raise ActionError(
                    409,
                    "审核正在运行",
                    f"{active_review_task or '另一个任务'} 正在审核；"
                    "同一时刻只允许一个执行器写手，本次没有启动任何进程。",
                    "等待审核结束后再分发。",
                )
            try:
                lock = _runner().lock_info()
            except Exception:
                lock = {}
            if lock.get("active"):
                raise ActionError(
                    409,
                    "执行器正忙",
                    f"执行器正在运行 {lock.get('task') or '未知任务'}；"
                    "本次没有启动任何进程。",
                    "等待当前运行结束后再分发。",
                )
            subtasks, error = _load_subtasks(
                task.subtasks_path, task.task_id
            )
            if error:
                raise ActionError(
                    400,
                    "子任务清单无法解析",
                    f"{error} 未启动任何进程。",
                    "修正 对接/subtasks.yaml 后重试。",
                )
            pending = tuple(
                item for item in subtasks if item.status in {"pending", "failed"}
            )
            if not pending:
                raise ActionError(
                    400,
                    "没有待开始或失败的子任务",
                    "所有子任务都是 delivered/passed，未启动任何进程。",
                    "查看子任务表格；下一步是开始审核。",
                )
            roles, _role_issues = _load_roles()
            agents, _agent_issues = _load_agents()
            bindings, violations = _dispatch_preflight(pending, roles, agents)
            if violations:
                raise ActionError(
                    409,
                    "分发预检未通过",
                    "以下子任务的角色绑定不满足要求，未启动任何进程："
                    + "；".join(violations),
                    "到 /roles 绑定可执行 Agent，或修正 subtasks.yaml 的 agent_role。",
                    violations=violations,
                )

            retried = 0
            for item in pending:
                if item.status != "failed":
                    continue
                try:
                    _update_subtask_record(
                        task.subtasks_path,
                        item.id,
                        {
                            "status": "pending",
                            "started_at": "",
                            "finished_at": "",
                            "error": "",
                            "log": "",
                            "run_id": "",
                        },
                    )
                except Exception as exc:
                    raise ActionError(
                        500,
                        "失败子任务无法重置",
                        f"{item.id} 无法从 failed 重置为 pending：{exc}；未启动任何进程。",
                        "检查 对接/subtasks.yaml 后重试。",
                    ) from exc
                retried += 1

            first = pending[0]
            state: Dict[str, object] = {
                "ok": True,
                "task": task.task_id,
                "state": "running",
                "total": len(pending),
                "done": 0,
                "success": 0,
                "failed": 0,
                "retried": retried,
                "current_index": 1,
                "current_subtask": first.id,
                "current_title": first.title,
                "started_at": _now_iso(),
                "finished_at": "",
                "message": f"分发中… 当前子任务 {first.id} {first.title}",
                "results": [],
            }
            if revise_context:
                state["revise"] = True
                state["revise_round"] = str(revise_context.get("round") or "")
            initial_state = copy.deepcopy(state)
            thread = threading.Thread(
                target=_dispatch_worker,
                args=(task, pending, bindings, state, revise_context),
                name=f"dispatch-{task.task_id}",
                daemon=True,
            )
            with DISPATCH_STATE_LOCK:
                ACTIVE_DISPATCH = {"task": task.task_id, "state": state}
                _write_dispatch_state_file(state)
            try:
                thread.start()
            except RuntimeError as exc:
                with DISPATCH_STATE_LOCK:
                    ACTIVE_DISPATCH = None
                    state["state"] = "failed"
                    state["message"] = f"分发线程无法启动：{exc}"
                    _write_dispatch_state_file(state)
                raise ActionError(
                    500,
                    "分发未能启动",
                    f"分发线程无法启动：{exc}",
                    "查看控制台终端错误信息后重试。",
                ) from exc
            return initial_state


def _dispatch_status_payload(task: Task) -> Dict[str, object]:
    state = _dispatch_state_snapshot(task.task_id)
    can_start, reason = _dispatch_availability(task)
    pending = [item for item in task.subtasks if item.status == "pending"]
    failed_items = [item for item in task.subtasks if item.status == "failed"]
    delivered = sum(
        1 for item in task.subtasks if item.status in {"delivered", "passed"}
    )
    results = state.get("results")
    if not isinstance(results, list):
        results = []
    payload: Dict[str, object] = {
        "ok": True,
        "task": task.task_id,
        "state": str(state.get("state") or "idle"),
        "state_label": DISPATCH_STATE_NAMES.get(
            str(state.get("state") or "idle"), "未开始"
        ),
        "total": int(state.get("total") or 0),
        "done": int(state.get("done") or 0),
        "success": int(state.get("success") or 0),
        "failed": int(state.get("failed") or 0),
        "current_index": int(state.get("current_index") or 0),
        "current_subtask": str(state.get("current_subtask") or ""),
        "current_title": str(state.get("current_title") or ""),
        "started_at": str(state.get("started_at") or ""),
        "finished_at": str(state.get("finished_at") or ""),
        "message": str(state.get("message") or ""),
        "results": results,
        "pending": len(pending),
        "failed_subtasks": len(failed_items),
        "delivered": delivered,
        "retried": int(state.get("retried") or 0),
        "action_label": _dispatch_action_label(task),
        "phase": _task_phase(task),
        "phase_label": _phase_label(_task_phase(task)),
        "phase_tone": _phase_tone(_task_phase(task)),
        "can_start": can_start,
        "disabled_reason": "" if can_start else reason,
        "handoff_blocked": _handoff_blocked_for(task.task_id),
        "handoff": _handoff_state_snapshot(task.task_id),
        "handoff_status_url": f"/api/task/{task.task_id}/handoff-status",
        "subtasks": [
            {
                "id": item.id,
                "title": item.title,
                "role": item.agent_role,
                "status": item.status,
                "log": item.log,
                "error": item.error,
                "started_at": item.started_at,
                "finished_at": item.finished_at,
            }
            for item in task.subtasks
        ],
        "detail_url": f"/task/{task.task_id}",
    }
    return payload


# ---------------------------------------------------------------------------
# 审核阶段：全部子任务交付后，由 reviewer 角色串行复核
# ---------------------------------------------------------------------------


def _review_state_path(task_id: str) -> Path:
    return DISPATCH_DIR / f"review-{task_id.upper()}.status.json"


def _write_review_state_file(state: Dict[str, object]) -> None:
    """尽力持久化审核状态；页面轮询失败不应打断执行。"""
    task_id = str(state.get("task") or "")
    if not task_id:
        return
    path = _review_state_path(task_id)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    except OSError:
        traceback.print_exc()
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _read_review_state_file(task_id: str) -> Dict[str, object]:
    path = _review_state_path(task_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if str(data.get("task") or "").upper() != task_id.upper():
        return {}
    return data


def _active_review_task() -> str:
    with REVIEW_STATE_LOCK:
        if ACTIVE_REVIEW is None:
            return ""
        return str(ACTIVE_REVIEW.get("task") or "")


def _review_active_for(task_id: str) -> bool:
    """内存中的审核，或控制台重启后仍由旧实例启动的审核进程。"""
    wanted = task_id.strip().upper()
    active = _active_review_task()
    if active:
        return active.upper() == wanted
    state = _read_review_state_file(wanted)
    if str(state.get("state") or "") != "running":
        return False
    try:
        runner = _runner()
        lock = runner.lock_info()
        status = runner.read_status()
    except Exception:
        return False
    if not lock.get("active"):
        return False
    if str(lock.get("task") or "").upper() != wanted:
        return False
    return str(status.get("kind") or "") == "review"


def _review_state_snapshot(task_id: str) -> Dict[str, object]:
    task_id = task_id.upper()
    with REVIEW_STATE_LOCK:
        if ACTIVE_REVIEW is not None and str(
            ACTIVE_REVIEW.get("task") or ""
        ).upper() == task_id:
            state = copy.deepcopy(ACTIVE_REVIEW.get("state") or {})
            assert isinstance(state, dict)
            return state
    state = _read_review_state_file(task_id)
    if not state:
        return {}
    if str(state.get("state") or "") == "running" and not _review_active_for(task_id):
        state = dict(state)
        state["state"] = "review_failed"
        state["message"] = (
            "上次审核没有正常收尾（控制台重启或线程中断）；"
            "如 对接/审查.md 已落盘，以文件里的结论为准。"
        )
    return state


def _subtasks_all_delivered(task: Task) -> bool:
    return bool(task.subtasks) and all(
        item.status in {"delivered", "passed"} for item in task.subtasks
    )


def _workspace_product_entries(task: Task) -> List[FileEntry]:
    """workdir 里除运行日志以外的产物；任务级审核允许没有 subtasks.yaml。

    默认 workdir 就是 工作区/，外部 workdir 则直接扫外部目录，保持与
    子任务 output_dir 相同的路径口径。
    """
    products: List[FileEntry] = []
    for entry in _collect_files(task.workdir):
        relative = entry.relative.replace("\\", "/")
        if relative == "日志" or relative.startswith("日志/"):
            continue
        products.append(entry)
    return products


def _delivery_evidence(task: Task) -> Tuple[bool, str]:
    """任务级交付判据：DONE.md、全部子任务已交付或无子任务时的工作区产物。"""
    if task.delivery is not None:
        return True, f"对接/{task.delivery.folder}/DONE.md 已落盘"
    if _subtasks_all_delivered(task):
        return True, f"全部子任务已交付（{task.subtasks_done}/{len(task.subtasks)}）"
    if not task.subtasks:
        products = _workspace_product_entries(task)
        if products:
            return True, f"工作区已有 {len(products)} 个产物"
    return False, "尚未发现交付：对接/交付/**/DONE.md 或工作区产物"


def _handoff_state_path(task_id: str) -> Path:
    return DISPATCH_DIR / f"handoff-{task_id.upper()}.status.json"


def _write_handoff_state_file(state: Dict[str, object]) -> None:
    task_id = str(state.get("task") or "")
    if not task_id:
        return
    path = _handoff_state_path(task_id)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    except OSError:
        traceback.print_exc()
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _read_handoff_state_file(task_id: str) -> Dict[str, object]:
    path = _handoff_state_path(task_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if str(data.get("task") or "").upper() != task_id.upper():
        return {}
    return data


def _active_handoff_task() -> str:
    with HANDOFF_STATE_LOCK:
        if ACTIVE_HANDOFF is None:
            return ""
        return str(ACTIVE_HANDOFF.get("task") or "")


def _handoff_state_snapshot(task_id: str) -> Dict[str, object]:
    task_id = task_id.upper()
    with HANDOFF_STATE_LOCK:
        active_same = ACTIVE_HANDOFF is not None and str(
            ACTIVE_HANDOFF.get("task") or ""
        ).upper() == task_id
    if active_same:
        # 运行中的状态每一步都已原子落盘；从磁盘读取可避免 deepcopy 与
        # 后台补交线程同时改 dict 的竞态，且不会让页面卡住。
        persisted = _read_handoff_state_file(task_id)
        if persisted:
            return persisted
    state = _read_handoff_state_file(task_id)
    if not state:
        return {}
    if (
        str(state.get("state") or "") in {"checking", "supplementing"}
        and _active_handoff_task().upper() != task_id
    ):
        state = dict(state)
        state["state"] = "blocked"
        state["message"] = (
            "上次交接体检没有正常收尾（控制台重启或线程中断）；"
            "请人工核对文件后重试体检。"
        )
        state["error"] = "交接体检运行状态已失效。"
        state["finished_at"] = str(state.get("finished_at") or _now_iso())
    return state


def _handoff_blocked_for(task_id: str) -> bool:
    state = _handoff_state_snapshot(task_id)
    return str(state.get("state") or "") == "blocked"


def _new_handoff_log_path(task: Task) -> Path:
    log_dir = task.workspace_dir / "日志"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = log_dir / f"handoff-{stamp}.log"
    suffix = 1
    while candidate.exists():
        candidate = log_dir / f"handoff-{stamp}-{suffix}.log"
        suffix += 1
    return candidate


def _append_handoff_log(path: Path, lines: List[str]) -> None:
    """交接体检日志尽力追加；日志失败不改变体检结论。"""
    if not lines:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        traceback.print_exc()


def _handoff_gate_check(
    gate: str,
    owner_role: str,
    *,
    missing: Optional[List[str]] = None,
    detail: str = "",
    subtask: str = "",
    repair_paths: Optional[List[Path]] = None,
) -> Dict[str, object]:
    missing_items = [item for item in (missing or []) if item]
    key = f"{gate}:{subtask}" if subtask else gate
    if not detail:
        detail = "通过" if not missing_items else "缺少：" + "、".join(missing_items)
    return {
        "key": key,
        "gate": gate,
        "name": HANDOFF_GATE_LABELS.get(gate, gate),
        "owner_role": owner_role,
        "subtask": subtask,
        "passed": not missing_items,
        "missing": missing_items,
        "detail": detail,
        "repair_paths": [str(path) for path in (repair_paths or [])],
    }


def _gate_g1(task: Task) -> Dict[str, object]:
    """登记完成闸门：controller 必须交出四件登记文件和可解析的子任务。"""
    missing: List[str] = []
    if not task.meta_path.is_file():
        missing.append("对接/task.yaml")
    elif task.registration_status != REGISTERED:
        missing.append("对接/task.yaml（status=registered）")
    if not task.path.is_file():
        missing.append("对接/任务单.md")
    if not task.subtasks_path.is_file():
        missing.append("对接/subtasks.yaml")
    elif task.subtasks_error:
        missing.append(f"对接/subtasks.yaml 可解析（{task.subtasks_error}）")
    elif not task.subtasks:
        missing.append("对接/subtasks.yaml（至少 1 个子任务）")
    register_done = task.docking_dir / REGISTER_DONE_NAME
    if not register_done.is_file():
        missing.append("对接/登记完成.md")
    detail = ""
    if not missing:
        detail = f"通过：{len(task.subtasks)} 个子任务可解析"
    return _handoff_gate_check(
        "G1",
        CONTROLLER_ROLE_ID,
        missing=missing,
        detail=detail,
        repair_paths=[
            task.meta_path,
            task.path,
            task.subtasks_path,
            register_done,
        ],
    )


def _subtask_output_dir(task: Task, subtask: Subtask) -> Path:
    """唯一交付目录：workdir/子任务/<id>（绝对路径）。

    默认 workdir 与外部 workdir 走同一条规则；提示词、G2、审核与日志
    都引用这个结果，避免提示词多一层 `工作区/` 而 G2 查不到。
    ID 再次做边界校验；resolve 后必须仍在 workdir 或项目工作区内。
    """
    subtask_id = str(subtask.id or "")
    problem = _subtask_id_problem(subtask_id, task.task_id)
    if problem:
        raise ConfigError(f"子任务 id 不合法：{subtask_id or '（空）'}（{problem}）")
    workdir = Path(task.workdir).resolve()
    output = (workdir / "子任务" / subtask_id).resolve()
    allowed = (workdir, Path(task.workspace_dir).resolve())
    if not any(_is_within(output, root) for root in allowed):
        raise ConfigError(
            f"子任务 {subtask_id} 的输出目录越界：{output}"
            f"（允许根：{workdir} 或 {allowed[1]}）"
        )
    return output


def _path_label(path: Path) -> str:
    """页面/闸门提示用路径：值班台内显示相对路径，外部显示绝对路径。"""
    candidate = Path(path)
    try:
        return str(candidate.relative_to(PROJECT_DIR))
    except ValueError:
        return str(candidate)


def _subtask_products(output_dir: Path) -> List[Path]:
    if not output_dir.is_dir():
        return []
    products: List[Path] = []
    try:
        for path in output_dir.rglob("*"):
            if not path.is_file() or path.name == DONE_NAME:
                continue
            if any(part.startswith(".") for part in path.relative_to(output_dir).parts):
                continue
            products.append(path)
    except OSError:
        return []
    return products


def _done_marker_value(text: str, key: str) -> str:
    match = re.search(
        rf"(?im)^\s*(?:[-*+]\s*)?{re.escape(key)}\s*[:：]\s*([^\s`|]+)",
        text or "",
    )
    if match is None:
        return ""
    return match.group(1).strip().strip("`\"'")


def _done_freshness_problems(done_text: str, subtask: Subtask) -> List[str]:
    """DONE.md 必须声明与当前分发记录一致的 run_id 或 attempt。

    旧版任务没有 attempt/run_id 记录时保持兼容（不做新鲜度判定）；
    新分发一定写入 attempt，因此旧 DONE 无法蒙混过关。
    """
    recorded_run_id = str(subtask.run_id or "").strip()
    recorded_attempt = int(getattr(subtask, "attempt", 0) or 0)
    if recorded_attempt <= 0 and not recorded_run_id:
        return []
    done_run_id = _done_marker_value(done_text, "run_id")
    done_attempt_text = _done_marker_value(done_text, "attempt")
    if not done_run_id and not done_attempt_text:
        return [
            f"{DONE_NAME} 缺少本次分发记录（必须含 run_id: 或 attempt:，"
            f"本次 attempt={recorded_attempt or '—'}）"
        ]
    problems: List[str] = []
    if done_attempt_text:
        try:
            done_attempt = int(done_attempt_text)
        except ValueError:
            problems.append(f"{DONE_NAME} 的 attempt 不是整数：{done_attempt_text}")
        else:
            if recorded_attempt <= 0 or done_attempt != recorded_attempt:
                problems.append(
                    f"{DONE_NAME} 的 attempt={done_attempt} 与本次分发记录"
                    f"（{recorded_attempt or '空'}）不一致"
                )
    if done_run_id:
        if not recorded_run_id or done_run_id != recorded_run_id:
            problems.append(
                f"{DONE_NAME} 的 run_id={done_run_id} 与本次分发记录"
                f"（{recorded_run_id or '空'}）不一致"
            )
    return problems


def _gate_g2(task: Task) -> List[Dict[str, object]]:
    """每个已交付子任务都是一道 G2 闸门，责任方是其 agent_role。"""
    checks: List[Dict[str, object]] = []
    for subtask in task.subtasks:
        if subtask.status not in {"delivered", "passed"}:
            continue
        owner = subtask.agent_role.strip() or "（未填写）"
        try:
            output_dir = _subtask_output_dir(task, subtask)
        except ConfigError as exc:
            checks.append(
                _handoff_gate_check(
                    "G2",
                    owner,
                    missing=[f"子任务输出目录不安全：{exc}"],
                    subtask=subtask.id,
                    repair_paths=[Path(task.workdir)],
                )
            )
            continue
        done_path = output_dir / DONE_NAME
        missing: List[str] = []
        if not done_path.is_file():
            missing.append(_path_label(done_path))
        else:
            try:
                done_text = _read_text(done_path)
            except OSError:
                done_text = ""
            absent = [
                marker
                for marker in HANDOFF_REQUIRED_DONE_MARKERS
                if marker not in done_text
            ]
            if absent:
                missing.append(
                    f"{DONE_NAME} 缺少章节："
                    + "/".join(HANDOFF_REQUIRED_DONE_MARKERS)
                    + f"（缺 {'、'.join(absent)}）"
                )
            missing.extend(_done_freshness_problems(done_text, subtask))
        products = _subtask_products(output_dir)
        if not products:
            missing.append(
                f"{_path_label(output_dir)}/ 至少 1 个产物文件（非 {DONE_NAME}）"
            )
        detail = ""
        if not missing:
            detail = f"通过：{_path_label(done_path)} + {len(products)} 个产物"
        checks.append(
            _handoff_gate_check(
                "G2",
                owner,
                missing=missing,
                detail=detail,
                subtask=subtask.id,
                repair_paths=[done_path, output_dir],
            )
        )
    return checks


def _gate_g3(
    task: Task,
    *,
    review_baseline: object = None,
    review_done_baseline: object = None,
    require_new: bool = False,
) -> Dict[str, object]:
    """审核完成闸门：结论可识别、完成信号已落盘，本轮确实重写过。"""
    missing: List[str] = []
    review_path = task.docking_dir / REVIEW_NAME
    review_done_path = task.docking_dir / REVIEW_DONE_NAME
    if not review_path.is_file():
        missing.append(f"对接/{REVIEW_NAME}")
    else:
        try:
            review_text = _read_text(review_path)
        except OSError:
            review_text = ""
        if not _review_verdict(review_text):
            missing.append(f"对接/{REVIEW_NAME}（可识别结论：通过 / 须修正）")
        elif require_new and not _artifact_updated(review_path, review_baseline):
            missing.append(f"对接/{REVIEW_NAME}（本轮未重新写入）")
    if not review_done_path.is_file():
        missing.append(f"对接/{REVIEW_DONE_NAME}")
    elif require_new and not _artifact_updated(
        review_done_path, review_done_baseline
    ):
        missing.append(f"对接/{REVIEW_DONE_NAME}（本轮未重新写入）")
    return _handoff_gate_check(
        "G3",
        REVIEW_ROLE_ID,
        missing=missing,
        repair_paths=[review_path, review_done_path],
    )


def _gate_g4(task: Task, round_no: int) -> Dict[str, object]:
    """修正完成闸门：本轮历史快照存在，新一轮审查结论可识别。"""
    missing: List[str] = []
    history_path = (
        task.docking_dir / REVIEW_HISTORY_DIRNAME / f"round-{round_no}.md"
    )
    if round_no <= 0:
        missing.append("对接/审查历史/round-<n>.md（修正轮次缺失）")
    elif not history_path.is_file():
        missing.append(f"对接/审查历史/round-{round_no}.md")
    review_path = task.docking_dir / REVIEW_NAME
    if not review_path.is_file():
        missing.append(f"对接/{REVIEW_NAME}（新一轮结论）")
    else:
        try:
            review_text = _read_text(review_path)
        except OSError:
            review_text = ""
        if not _review_verdict(review_text):
            missing.append(f"对接/{REVIEW_NAME}（新一轮可识别结论）")
    return _handoff_gate_check(
        "G4",
        REVIEW_ROLE_ID,
        missing=missing,
        detail=(
            f"通过：round-{round_no}.md 与新一轮审查结论"
            if not missing
            else ""
        ),
        repair_paths=[history_path, review_path],
    )


def _handoff_checks_for(
    task: Task,
    stage: str,
    round_no: int = 0,
    *,
    review_baseline: object = None,
    review_done_baseline: object = None,
) -> List[Dict[str, object]]:
    if stage == "dispatch":
        return [_gate_g1(task), *_gate_g2(task)]
    if stage == "review":
        return _gate_g2(task)
    if stage == "review_complete":
        checks = [
            _gate_g3(
                task,
                review_baseline=review_baseline,
                review_done_baseline=review_done_baseline,
                require_new=True,
            )
        ]
        if round_no > 0:
            checks.append(_gate_g4(task, round_no))
        return checks
    if stage == "revise_before":
        return [_gate_g3(task)]
    if stage == "revise_complete":
        return [
            _gate_g3(
                task,
                review_baseline=review_baseline,
                review_done_baseline=review_done_baseline,
                require_new=True,
            ),
            _gate_g4(task, round_no),
        ]
    raise ValueError(f"未知交接阶段：{stage}")


def _handoff_role_agent(
    task: Task, role_id: str
) -> Tuple[Optional[Role], Optional[AgentApp], str]:
    roles, _role_issues = _load_roles()
    agents, _agent_issues = _load_agents()
    role = next((item for item in roles if item.id == role_id), None)
    if role is None:
        if role_id == CONTROLLER_ROLE_ID and task.controller_agent:
            agent = next(
                (item for item in agents if item.id == task.controller_agent), None
            )
            if agent is not None and agent.installed and agent.currently_executable:
                return None, agent, ""
        return None, None, f"角色 {role_id} 不存在"
    if not role.enabled:
        return role, None, f"角色 {role.name}（{role.id}）已停用"
    if not role.agent_id:
        return role, None, f"角色 {role.name}（{role.id}）未绑定 Agent"
    agent = next((item for item in agents if item.id == role.agent_id), None)
    if agent is None:
        return role, None, f"角色 {role.name} 绑定的 Agent {role.agent_id} 未登记"
    if not agent.installed:
        return role, None, f"角色 {role.name} 绑定的 Agent {agent.id} 未安装"
    if not agent.currently_executable:
        return role, None, f"角色 {role.name} 绑定的 Agent {agent.id} 当前不可执行"
    return role, agent, ""


def _handoff_prompt(
    task: Task,
    stage: str,
    checks: List[Dict[str, object]],
    *,
    role_id: str,
    role_name: str,
    agent_id: str = "",
) -> str:
    missing_lines: List[str] = []
    for check in checks:
        missing = check.get("missing")
        if not isinstance(missing, list):
            missing = []
        missing_lines.append(
            f"- {check.get('gate')} {check.get('name')}"
            f"（责任角色 {check.get('owner_role')}）"
            + (
                f" · 子任务 {check.get('subtask')}"
                if check.get("subtask")
                else ""
            )
            + "：缺少 "
            + "；".join(str(item) for item in missing)
        )
        repair_paths = check.get("repair_paths")
        if isinstance(repair_paths, list) and repair_paths:
            missing_lines.append(
                "  可写入路径：" + "、".join(str(path) for path in repair_paths)
            )
        if str(check.get("gate") or "") == "G2":
            subtask_id = str(check.get("subtask") or "")
            subtask = next(
                (item for item in task.subtasks if item.id == subtask_id), None
            )
            attempt = int(getattr(subtask, "attempt", 0) or 0) if subtask else 0
            if attempt > 0:
                missing_lines.append(
                    f"  ★ 本次分发记录 attempt: {attempt}；补写的 {DONE_NAME} "
                    f"必须包含一行 `attempt: {attempt}`，否则 G2 仍判缺件。"
                )
    missing_text = "\n".join(missing_lines)
    missing_summary = "；".join(
        f"{check.get('key')} "
        + "、".join(str(item) for item in list(check.get("missing") or []))
        for check in checks
    )
    gate_ids = {str(check.get("gate") or "") for check in checks}
    review_allowed = (
        role_id == REVIEW_ROLE_ID
        and bool(gate_ids)
        and gate_ids.issubset({"G3", "G4"})
    )
    fixed = (
        f"交接检查未通过：缺少 {missing_summary}。只补这些文件，不要重做其它工作；"
        f"补完即退出。不得写 对接/{REVIEW_NAME}（除非你是 reviewer）。"
    )
    if review_allowed:
        review_rule = (
            f"你是 reviewer，可以补写 对接/{REVIEW_NAME}；不得改子任务产物。"
        )
        if agent_id:
            review_rule += (
                "\n- 补写的 审查.md 开头必须原样写入下面两行"
                "（角色与 Agent 不得改写）：\n"
                + _review_attribution_header(role_name, agent_id)
                + "- 头部第三行写实际结论（二选一）："
                "`- 结论：**通过**` 或 `- 结论：**须修正**`。\n"
                "- ★ 不得自称 AI-A，也不得在审查中出现 `AI-A` 字样。"
            )
    elif role_id == REVIEW_ROLE_ID:
        review_rule = (
            f"★ 本次不是审核完成闸门（{','.join(sorted(gate_ids))}）："
            f"即使你是 reviewer，也不得写 对接/{REVIEW_NAME}。"
        )
    else:
        review_rule = f"★ 你不是 reviewer：绝对不得写 对接/{REVIEW_NAME}。"
    return (
        f"{fixed}\n"
        f"你是责任角色 {role_name}（{role_id}）。\n"
        f"任务：{task.task_id}「{task.title}」\n"
        f"交接阶段：{HANDOFF_STAGE_LABELS.get(stage, stage)}（{stage}）\n"
        f"值班台项目目录：{task.project_dir}\n"
        f"工作目录：{task.workdir}\n"
        "\n"
        "需要补齐：\n"
        f"{missing_text}\n"
        "\n"
        "补写要求：\n"
        "- 只补上面列出的缺失文件；已有文件不得删除或重做。\n"
        f"- {DONE_NAME} 必须含「做了什么 / 怎么验 / 结果」三部分。\n"
        "- 子任务产物与 DONE.md 写入上面「可写入路径」列出的绝对路径"
        "（统一为 <工作目录>/子任务/<子任务id>/）。\n"
        f"- {review_rule}\n"
        "- 不删除文件，不改其它子任务，不重启 8787。\n"
        "补完即退出。"
    )


def _launch_handoff_agent(
    task: Task,
    agent: AgentApp,
    role_id: str,
    prompt: str,
    *,
    attempt: int,
) -> Dict[str, object]:
    spec = runner_engine.AgentSpec(
        id=agent.id,
        name=agent.name,
        command=agent.command,
        args_template=agent.args_template,
    )
    safe_role = re.sub(r"[^A-Za-z0-9_-]+", "_", role_id)[:40] or "agent"
    runner = _runner()
    runner.launch(
        task_id=task.task_id,
        task_dir=task.project_dir,
        agent=spec,
        round_no=max(1, task.revise_round),
        workdir=task.workdir,
        prompt=prompt,
        kind="handoff",
        log_prefix=f"handoff-{safe_role}-{attempt}",
        cwd=task.project_dir,
    )
    deadline = time.monotonic() + runner.timeout_seconds + 120.0
    finished = False
    while time.monotonic() < deadline:
        if runner.wait_for_idle(0.2):
            finished = True
            break
        abort_state, abort_reason = _active_advance_abort(task.task_id)
        if abort_state:
            _stop_active_runner_for_task(task.task_id, abort_reason)
            break
    if not finished:
        finished = runner.wait_for_idle(0.0)
    snapshot = dict(runner.snapshot())
    return {
        "agent": agent.id,
        "agent_name": agent.name,
        "role": role_id,
        "attempt": attempt,
        "finished": finished,
        "state": str(snapshot.get("state") or "failed"),
        "log": str(snapshot.get("log") or ""),
        "error": str(snapshot.get("error") or ""),
    }


def _run_handoff_gates(
    task: Task,
    stage: str,
    *,
    round_no: int = 0,
    source: str = "manual",
    allow_supplement: bool = True,
    review_baseline: object = None,
    review_done_baseline: object = None,
) -> Dict[str, object]:
    """执行交接体检；缺件时回叫责任 Agent，最多补交两次。

    同步执行：调用方负责保证此刻没有其它分发/审核在跑。补交本身仍通过
    runner.launch 抢全局 runner.lock，因此与所有写入任务串行。
    """
    global ACTIVE_HANDOFF
    if task.archived:
        return {
            "ok": False,
            "task": task.task_id,
            "stage": stage,
            "state": "blocked",
            "passed": False,
            "checks": [],
            "missing": ["归档任务只读，不能执行交接体检"],
            "attempts": {},
            "agent_runs": [],
            "message": "归档任务只读，不能执行交接体检。",
            "error": "任务位于 归档/。",
            "log": "",
            "can_retry": False,
        }

    try:
        log_path = _new_handoff_log_path(task)
    except OSError as exc:
        return {
            "ok": False,
            "task": task.task_id,
            "stage": stage,
            "state": "blocked",
            "passed": False,
            "checks": [],
            "missing": [f"无法创建交接日志：{exc}"],
            "attempts": {},
            "agent_runs": [],
            "message": f"无法创建交接日志：{exc}",
            "error": str(exc),
            "log": "",
            "can_retry": False,
        }

    state: Dict[str, object] = {
        "ok": True,
        "task": task.task_id,
        "stage": stage,
        "stage_label": HANDOFF_STAGE_LABELS.get(stage, stage),
        "source": source,
        "round": int(round_no),
        "review_baseline": review_baseline,
        "review_done_baseline": review_done_baseline,
        "state": "checking",
        "passed": False,
        "checks": [],
        "missing": [],
        "attempts": {},
        "attempt_total": 0,
        "agent_runs": [],
        "message": "正在检查交接必要文件…",
        "error": "",
        "log": _display_path(log_path),
        "started_at": _now_iso(),
        "finished_at": "",
    }
    _append_handoff_log(
        log_path,
        [
            f"# 交接闸门 · {task.task_id} · "
            f"{HANDOFF_STAGE_LABELS.get(stage, stage)}",
            f"- 开始：{state['started_at']}",
            f"- 来源：{source}",
            f"- 任务：{task.task_id}「{task.title}」",
            f"- 阶段：{stage}",
            f"- 每道闸门最多补交：{HANDOFF_MAX_SUPPLEMENTS} 次",
        ],
    )

    result: Dict[str, object] = {}
    with HANDOFF_RUN_LOCK:
        with HANDOFF_STATE_LOCK:
            ACTIVE_HANDOFF = {"task": task.task_id, "state": state}
            _write_handoff_state_file(state)
        try:
            while True:
                abort_state, abort_reason = _active_advance_abort(task.task_id)
                if abort_state:
                    _stop_active_runner_for_task(task.task_id, abort_reason)
                    state["state"] = "blocked"
                    state["passed"] = False
                    state["finished_at"] = _now_iso()
                    state["error"] = (
                        "用户请求停止全自动推进，交接检查中止。"
                        if abort_state == "stopped"
                        else "推进预算用尽，交接检查中止：" + abort_reason
                    )
                    state["message"] = (
                        "用户停止推进；交接检查已中止。"
                        if abort_state == "stopped"
                        else "预算用尽；交接检查已中止。"
                    )
                    _append_handoff_log(
                        log_path,
                        [
                            "- 结论：中止（"
                            + (
                                "用户停止推进"
                                if abort_state == "stopped"
                                else "预算用尽"
                            )
                            + "）",
                            f"- 结束：{state['finished_at']}",
                        ],
                    )
                    break
                checks = _handoff_checks_for(
                    task,
                    stage,
                    round_no,
                    review_baseline=review_baseline,
                    review_done_baseline=review_done_baseline,
                )
                missing_checks = [
                    check for check in checks if not bool(check.get("passed"))
                ]
                state["checks"] = checks
                state["missing"] = [
                    item
                    for check in missing_checks
                    for item in list(check.get("missing") or [])
                ]
                state["attempts"] = dict(state.get("attempts") or {})
                attempts = state["attempts"]
                assert isinstance(attempts, dict)
                _append_handoff_log(
                    log_path,
                    [
                        (
                            f"- 检查 {check.get('key')} "
                            f"{check.get('name')}（责任角色 "
                            f"{check.get('owner_role')}）："
                            + ("通过" if check.get("passed") else "未通过")
                            + (
                                ""
                                if check.get("passed")
                                else "；缺少 " + "；".join(check.get("missing") or [])
                            )
                        )
                        for check in checks
                    ],
                )
                state["message"] = (
                    "交接体检通过。"
                    if not missing_checks
                    else "体检发现缺件：共 "
                    + str(len(state["missing"]))
                    + " 项。"
                )
                with HANDOFF_STATE_LOCK:
                    _write_handoff_state_file(state)
                if not missing_checks:
                    state["state"] = "passed"
                    state["passed"] = True
                    state["finished_at"] = _now_iso()
                    state["message"] = "交接体检通过，继续推进。"
                    _append_handoff_log(
                        log_path,
                        [f"- 结论：通过", f"- 结束：{state['finished_at']}"],
                    )
                    break

                if not allow_supplement:
                    state["state"] = "blocked"
                    state["finished_at"] = _now_iso()
                    state["error"] = "交接体检未通过；当前路径不允许自动补交。"
                    state["message"] = "交接体检未通过，需要人工处理。"
                    _append_handoff_log(
                        log_path,
                        [
                            "- 结论：阻塞（未允许自动补交）",
                            f"- 结束：{state['finished_at']}",
                        ],
                    )
                    break

                pending = [
                    check
                    for check in missing_checks
                    if int(attempts.get(str(check.get("key")), 0))
                    < HANDOFF_MAX_SUPPLEMENTS
                ]
                if not pending:
                    state["state"] = "blocked"
                    state["finished_at"] = _now_iso()
                    state["error"] = (
                        "交接体检连续补交 "
                        f"{HANDOFF_MAX_SUPPLEMENTS} 次仍未通过；需要人工处理。"
                    )
                    state["message"] = (
                        "已自动补交 "
                        f"{HANDOFF_MAX_SUPPLEMENTS} 次，仍有缺件；"
                        "推进已停止，等待人工处理。"
                    )
                    _append_handoff_log(
                        log_path,
                        [
                            "- 结论：阻塞（达到补交上限）",
                            f"- 缺件：{'；'.join(str(x) for x in state['missing'])}",
                            f"- 结束：{state['finished_at']}",
                        ],
                    )
                    break

                grouped: Dict[str, Dict[str, object]] = {}
                unresolved: List[str] = []
                for check in pending:
                    role_id = str(check.get("owner_role") or "").strip()
                    role, agent, issue = _handoff_role_agent(task, role_id)
                    if agent is None:
                        unresolved.append(
                            f"{check.get('key')}：{issue or '责任 Agent 不可用'}"
                        )
                        continue
                    group_key = f"{role_id}|{agent.id}"
                    group = grouped.setdefault(
                        group_key,
                        {
                            "role_id": role_id,
                            "role_name": role.name if role is not None else role_id,
                            "agent": agent,
                            "checks": [],
                        },
                    )
                    checks_in_group = group["checks"]
                    assert isinstance(checks_in_group, list)
                    checks_in_group.append(check)
                if unresolved:
                    state["error"] = "；".join(unresolved)
                    _append_handoff_log(
                        log_path,
                        ["- 无法补交：" + line for line in unresolved],
                    )
                if not grouped:
                    state["state"] = "blocked"
                    state["finished_at"] = _now_iso()
                    state["error"] = state["error"] or (
                        "缺少责任 Agent，无法自动补交。"
                    )
                    state["message"] = "交接体检未通过，责任 Agent 不可用。"
                    _append_handoff_log(
                        log_path,
                        [
                            "- 结论：阻塞（责任 Agent 不可用）",
                            f"- 结束：{state['finished_at']}",
                        ],
                    )
                    break

                state["state"] = "supplementing"
                state["message"] = "正在回叫责任 Agent 补交缺件…"
                with HANDOFF_STATE_LOCK:
                    _write_handoff_state_file(state)
                no_call = False
                for group in grouped.values():
                    group_checks = group["checks"]
                    assert isinstance(group_checks, list)
                    role_id = str(group["role_id"] or "")
                    role_name = str(group["role_name"] or role_id)
                    agent = group["agent"]
                    if not isinstance(agent, AgentApp):
                        continue
                    attempt = max(
                        int(attempts.get(str(check.get("key")), 0))
                        for check in group_checks
                    ) + 1
                    for check in group_checks:
                        attempts[str(check.get("key"))] = attempt
                    state["attempts"] = attempts
                    state["attempt_total"] = int(state.get("attempt_total") or 0) + 1
                    _append_handoff_log(
                        log_path,
                        [
                            "",
                            f"- 补交 #{attempt} · Agent {agent.id}（{role_name}）",
                            "- 缺件："
                            + "；".join(
                                item
                                for check in group_checks
                                for item in list(check.get("missing") or [])
                            ),
                        ],
                    )
                    prompt = _handoff_prompt(
                        task,
                        stage,
                        group_checks,
                        role_id=role_id,
                        role_name=role_name,
                        agent_id=agent.id,
                    )
                    with HANDOFF_STATE_LOCK:
                        _write_handoff_state_file(state)
                    try:
                        result = _launch_handoff_agent(
                            task,
                            agent,
                            role_id,
                            prompt,
                            attempt=attempt,
                        )
                    except Exception as exc:
                        result = {
                            "agent": agent.id,
                            "agent_name": agent.name,
                            "role": role_id,
                            "attempt": attempt,
                            "finished": False,
                            "state": "failed",
                            "log": "",
                            "error": str(exc),
                        }
                    runs = state.get("agent_runs")
                    if not isinstance(runs, list):
                        runs = []
                        state["agent_runs"] = runs
                    runs.append(result)
                    _append_handoff_log(
                        log_path,
                        [
                            f"- 补交结果：Agent {agent.id} → "
                            f"{result.get('state')}；log="
                            f"{result.get('log') or '（无）'}"
                            + (
                                "；error=" + str(result.get("error"))
                                if result.get("error")
                                else ""
                            ),
                        ],
                    )
                    if not result.get("finished"):
                        no_call = True
                    with HANDOFF_STATE_LOCK:
                        _write_handoff_state_file(state)
                task = _find_task(task.task_id) or task
                if no_call:
                    # 进程没有正常收尾时仍复检一次，但下一轮会达到上限。
                    _append_handoff_log(
                        log_path, ["- 警告：至少一次补交未正常收尾，继续复检。"]
                    )
        except Exception as exc:
            traceback.print_exc()
            state["state"] = "blocked"
            state["passed"] = False
            state["error"] = f"交接体检异常：{exc}"
            state["message"] = "交接体检异常，已停止推进，等待人工处理。"
            state["finished_at"] = _now_iso()
            _append_handoff_log(
                log_path,
                [f"- 异常：{exc}", f"- 结束：{state['finished_at']}"],
            )
        finally:
            with HANDOFF_STATE_LOCK:
                _write_handoff_state_file(state)
                if ACTIVE_HANDOFF is not None and (
                    ACTIVE_HANDOFF.get("state") is state
                ):
                    ACTIVE_HANDOFF = None
            result = copy.deepcopy(state)
            result["ok"] = str(state.get("state") or "") == "passed"
            result["can_retry"] = str(state.get("state") or "") == "blocked"
    return result


def _handoff_missing_text(result: Dict[str, object]) -> str:
    missing = result.get("missing")
    if not isinstance(missing, list):
        return "（未返回缺件清单）"
    return "、".join(str(item) for item in missing) or "（未返回缺件清单）"


def _handoff_action_error(result: Dict[str, object]) -> ActionError:
    log = str(result.get("log") or "")
    message = (
        "交接体检未通过，推进已停止。缺件："
        + _handoff_missing_text(result)
        + f"。最多自动补交 {HANDOFF_MAX_SUPPLEMENTS} 次。"
        + (f" 详见交接日志 {log}。" if log else "")
    )
    return ActionError(
        409,
        "交接体检未通过",
        message,
        "补齐缺件后点「重试体检」，或人工检查后再推进。",
        extra={
            "handoff": result,
            "missing": result.get("missing") or [],
            "log": log,
        },
    )


def _registration_aware(task: Task) -> bool:
    """该任务是否使用发布/登记流程；旧版裸任务不强行要求登记四件套。"""
    return bool(
        task.registration_status
        or task.idea_path.is_file()
        or (task.docking_dir / REGISTER_DONE_NAME).is_file()
    )


def _registration_structure_incomplete(task: Task) -> bool:
    return bool(
        not task.subtasks_path.is_file()
        or task.subtasks_error
        or not task.subtasks
    )


def _ensure_handoff_not_blocked(task: Task) -> None:
    state = _handoff_state_snapshot(task.task_id)
    if str(state.get("state") or "") != "blocked":
        return
    log = str(state.get("log") or "")
    raise ActionError(
        409,
        "交接体检未通过",
        "上一轮交接体检已停止推进。缺件："
        + _handoff_missing_text(state)
        + (f"。详见 {log}。" if log else ""),
        "补齐缺件后点「重试体检」。",
        extra={"handoff": state, "missing": state.get("missing") or []},
    )


def _review_terminal_state(task: Task) -> Tuple[bool, str]:
    """审核终态只认「审查.md 明确结论」＋「审核完成.md 存在」。

    新审查（review 状态文件带 attribution_required）还必须通过署名校验；
    历史审查没有该标记，按旧规则判定，避免改写/推翻 T05–T23 的既有结论。
    """
    if task.review_path is None:
        return False, ""
    verdict = _review_verdict(task.review_text)
    if not verdict:
        return False, ""
    if not (task.docking_dir / REVIEW_DONE_NAME).is_file():
        return False, ""
    review_state = _review_state_snapshot(task.task_id)
    if _review_attribution_required(task, review_state) and _review_attribution_issue(
        task.review_text, review_state
    ):
        return False, ""
    return True, verdict


def _review_terminal_verdict(task: Task) -> str:
    terminal, verdict = _review_terminal_state(task)
    return verdict if terminal else ""


def _quarantine_invalid_review(
    task: Task, *, reason: str = "invalid"
) -> Optional[Path]:
    """把无效/半份/冒名审查移入 对接/审查历史/，不删除。

    署名错误使用 invalid-attribution-<时间>.md，其余沿用 invalid-<时间>.md。
    """
    if task.archived:
        return None
    review_path = task.docking_dir / REVIEW_NAME
    review_done_path = task.docking_dir / REVIEW_DONE_NAME
    if not review_path.is_file() and not review_done_path.is_file():
        return None
    history_dir = task.docking_dir / REVIEW_HISTORY_DIRNAME
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = (
        REVIEW_ATTRIBUTION_HISTORY_PREFIX
        if reason == "attribution"
        else "invalid"
    )
    moved: Optional[Path] = None
    if review_path.is_file():
        moved = _unique_history_path(history_dir, f"{prefix}-{stamp}.md")
        shutil.move(str(review_path), str(moved))
    if review_done_path.is_file():
        done_target = _unique_history_path(
            history_dir, f"{prefix}-{stamp}-{REVIEW_DONE_NAME}"
        )
        shutil.move(str(review_done_path), str(done_target))
    return moved


def _review_round_incomplete(task: Task, state: Dict[str, object]) -> bool:
    """本轮审核失败时，判断文件是否构成无效/半份（旧有效结论不动）。"""
    review_path = task.docking_dir / REVIEW_NAME
    if not review_path.is_file():
        return False
    try:
        review_text = _read_text(review_path)
    except OSError:
        return False
    if _review_verdict(review_text) not in {"passed", "revise"}:
        return True
    if not _artifact_updated(review_path, state.get("baseline_review")):
        # 仍是上一轮的有效审查：保留，供人工核对。
        return False
    review_done_path = task.docking_dir / REVIEW_DONE_NAME
    if not review_done_path.is_file():
        return True
    return not _artifact_updated(
        review_done_path, state.get("baseline_review_done")
    )


def _quarantine_review_round(task: Task, state: Dict[str, object]) -> None:
    latest = _find_task(task.task_id) or task
    if not _review_round_incomplete(latest, state):
        return
    try:
        _quarantine_invalid_review(latest)
    except OSError:
        traceback.print_exc()


def _task_phase(task: Task) -> str:
    """阶段顺序：交接阻塞 → 修正推进 → 运行中 → 审核终态 → 交付 → 分发。

    运行中的审核/分发优先于旧的审查文件，避免新一轮运行期间误报终态。
    """
    if _handoff_blocked_for(task.task_id):
        return PHASE_HANDOFF_BLOCKED
    advance_state = _advance_state_snapshot(task.task_id)
    if str(advance_state.get("state") or "") == "handoff_blocked":
        return PHASE_HANDOFF_BLOCKED
    if _active_advance_task().upper() == task.task_id.upper():
        revise_state = _advance_state_snapshot(task.task_id)
        if revise_state.get("revise") and str(
            revise_state.get("state") or ""
        ) == "running":
            step = str(revise_state.get("step") or "")
            return PHASE_REVIEWING if step == "review" else PHASE_DISPATCHING
    if _active_review_task() == task.task_id or _review_active_for(task.task_id):
        return PHASE_REVIEWING
    if _active_dispatch_task() == task.task_id:
        return PHASE_DISPATCHING
    terminal, verdict = _review_terminal_state(task)
    if terminal:
        return PHASE_REVISE if verdict == "revise" else PHASE_PASSED
    delivered, _reason = _delivery_evidence(task)
    if delivered:
        return PHASE_REVIEW_READY
    return PHASE_UNDISPATCHED


def _phase_label(phase: str) -> str:
    return str(PHASE_META.get(phase, PHASE_META[PHASE_UNDISPATCHED])["label"])


def _phase_tone(phase: str) -> str:
    return str(PHASE_META.get(phase, PHASE_META[PHASE_UNDISPATCHED])["tone"])


def _task_status_from_phase(task: Task, phase: str) -> str:
    """看板状态只由阶段派生，不再另用“交付 DONE.md 是否存在”一套判定。"""
    if phase == PHASE_PASSED:
        return STATUS_PASSED
    if phase == PHASE_REVISE:
        return STATUS_REVISE
    if phase == PHASE_HANDOFF_BLOCKED:
        return STATUS_NEEDS_ACTION
    if phase in {PHASE_REVIEW_READY, PHASE_REVIEWING}:
        return STATUS_DELIVERED
    return STATUS_PENDING


def _review_role_binding() -> Tuple[Optional[Role], Optional[AgentApp], str]:
    """解析 reviewer 角色及其 Agent；返回可直接展示的阻塞原因。"""
    roles, _role_issues = _load_roles()
    agents, _agent_issues = _load_agents()
    role = next((item for item in roles if item.id == REVIEW_ROLE_ID), None)
    if role is None:
        return None, None, "审核角色未绑定：协作/roles/reviewer.yaml 不存在"
    if not role.enabled:
        return role, None, f"审核角色 {role.name}（{role.id}）已停用"
    if not role.agent_id:
        return role, None, "审核角色未绑定 Agent"
    agent = next((item for item in agents if item.id == role.agent_id), None)
    if agent is None:
        return role, None, f"审核角色绑定的 Agent {role.agent_id} 未登记"
    if not agent.installed:
        return role, agent, f"审核角色绑定的 Agent {agent.id} 未安装（installed=false）"
    if not agent.currently_executable:
        return role, agent, f"审核角色绑定的 Agent {agent.id} 命令当前不可执行"
    return role, agent, ""


def _review_prompt(
    task: Task,
    role: Optional[Role] = None,
    agent: Optional[AgentApp] = None,
) -> str:
    project_dir = task.project_dir
    workdir = task.workdir
    if role is None or agent is None:
        bound_role, bound_agent, _binding_issue = _review_role_binding()
        role = role or bound_role
        agent = agent or bound_agent
    role_name = role.name if role is not None else REVIEW_ROLE_ID
    agent_id = agent.id if agent is not None else "未绑定"
    attribution = _review_attribution_header(role_name, agent_id)
    output_dirs = {
        item.id: _subtask_output_dir(task, item) for item in task.subtasks
    }
    done_lines = "\n".join(
        f"  - {output_dirs[item.id] / DONE_NAME}（{item.id} · {item.title}）"
        for item in task.subtasks
    )
    if done_lines:
        subtask_guide = (
            "· 读 `对接/任务单.md`、`对接/subtasks.yaml`、每个\n"
            "  子任务输出目录（绝对路径）下的 DONE.md 及产物：\n"
        )
    else:
        subtask_guide = (
            "· 本任务没有拆分子任务（subtasks_total=0）；请审核任务级交付与工作区产物：\n"
        )
        done_lines = "  - （无子任务 DONE.md；以任务级 DONE.md 与工作区产物为准）"
    delivery_lines = ""
    if task.delivery is not None:
        delivery_lines = f"  - 任务级交付：{task.delivery.done_path}\n"
    review_path = project_dir / DOCKING_DIRNAME / REVIEW_NAME
    review_done_path = project_dir / DOCKING_DIRNAME / REVIEW_DONE_NAME
    return (
        f"你是审核角色。审核任务 {task.task_id}「{task.title}」。\n"
        f"值班台项目目录：{project_dir}\n"
        f"工作目录：{workdir}\n"
        "\n"
        f"{subtask_guide}"
        f"  - {project_dir}/对接/任务单.md\n"
        f"  - {project_dir}/对接/subtasks.yaml（如存在）\n"
        f"{delivery_lines}"
        f"{done_lines}\n"
        f"  - 工作目录/产物根（绝对路径）：{workdir}\n"
        "· 按任务单的验收标准逐条核：功能、边界、证据\n"
        f"· 写 `对接/审查.md`（绝对路径：{review_path}）："
        "结论（**通过** 或 **须修正**）+ 逐项证据 + 问题清单\n"
        f"· 写 `对接/审核完成.md`（绝对路径：{review_done_path}）作为完成信号\n"
        "· ★ 审查.md 开头必须原样写入下面两行（角色与 Agent 不得改写）：\n"
        f"{attribution}"
        "· 头部第三行写实际结论（二选一）：`- 结论：**通过**` 或 "
        "`- 结论：**须修正**`；不要照抄本提示行。\n"
        "· ★ 不得自称 AI-A，也不得在审查中出现 `AI-A` 字样；"
        "任务单/历史材料中的 AI-A 也不要摘录进审查文件；"
        "否则整份审查会被拒收并移入 对接/审查历史/invalid-attribution-*.md。\n"
        "· ★ 只允许审核角色写 审查.md；不得改子任务产物；不删文件\n"
        "完成后退出。"
    )


def _review_ready_subtasks(task: Task) -> List[str]:
    labels = SUBTASK_STATUS_LABELS
    return [
        f"{item.id}（{labels.get(item.status, item.status)}）"
        for item in task.subtasks
        if item.status not in {"delivered", "passed"}
    ]


def _review_availability(task: Task) -> Tuple[bool, str]:
    if task.archived:
        return False, "归档任务只读，不能审核。"
    if _handoff_blocked_for(task.task_id):
        return False, "交接体检未通过；请先点「重试体检」或人工补齐缺件。"
    if _active_handoff_task():
        return False, "交接体检或自动补交正在进行，请等待结束。"
    if task.subtasks_path.is_file() and task.subtasks_error:
        return False, f"subtasks.yaml 无法解析：{task.subtasks_error}"
    waiting = _review_ready_subtasks(task)
    if waiting:
        return False, "还有未交付的子任务：" + "、".join(waiting) + "；请先完成分发。"
    delivered, evidence = _delivery_evidence(task)
    if not delivered:
        return False, evidence + "；没有可审核的交付物。"
    terminal, _verdict = _review_terminal_state(task)
    if terminal:
        return False, "对接/审查.md 与 审核完成.md 已构成审核终态。"
    # 空/半份/结论不可识别的审查不构成终态：保留重试入口，
    # 真正重试时由 _start_review 移入 对接/审查历史/invalid-<时间>.md。
    if _review_active_for(task.task_id):
        return False, "审核进行中，请等待当前审核结束。"
    if _active_dispatch_task():
        return False, (
            f"执行器正在为 {_active_dispatch_task()} 分发实现，请等待分发结束。"
        )
    active_advance = _active_advance_task()
    if active_advance:
        return False, f"{active_advance} 的全自动推进正在进行，请等待推进结束。"
    if ACTIVE_REGISTRATIONS:
        return False, "总控登记正在运行，请等待登记结束后再审核。"
    _role, _agent, binding_issue = _review_role_binding()
    if binding_issue:
        return False, binding_issue
    try:
        lock = _runner().lock_info()
    except Exception:
        lock = {}
    if lock.get("active"):
        return False, (
            f"执行器正忙（{lock.get('task') or '未知任务'}），"
            "请等待当前运行结束。"
        )
    if not task.subtasks:
        return True, f"审核本任务交付物：{evidence}。"
    return True, "全部子任务已交付，将由审核角色复核验收标准与证据。"


def _review_status_payload(task: Task) -> Dict[str, object]:
    state = _review_state_snapshot(task.task_id)
    state_name = str(state.get("state") or "idle")
    if state_name not in REVIEW_STATE_NAMES:
        state_name = "idle"
    phase = _task_phase(task)
    running = state_name == "running" and _review_active_for(task.task_id)
    if running:
        phase = PHASE_REVIEWING
    terminal, verdict = _review_terminal_state(task)
    handoff_blocked = _handoff_blocked_for(task.task_id)
    if terminal and not running and not handoff_blocked:
        state_name = "completed"
    if not terminal and not handoff_blocked and not running:
        if task.review_path is not None:
            if state_name in {"completed", "idle"}:
                state_name = "review_failed"
                state["message"] = (
                    "对接/审查.md 缺少可识别结论或 审核完成.md 完成信号；"
                    "可重新审核，旧文件会移入 对接/审查历史/invalid-<时间>.md。"
                )
                state["error"] = state["message"]
        elif state_name == "completed":
            state_name = "idle"
    can_start, reason = _review_availability(task)
    role, agent, binding_issue = _review_role_binding()
    review_done_path = task.docking_dir / REVIEW_DONE_NAME
    log = str(state.get("log") or "")
    log_url = f"/log?path={quote(log, safe='')}" if log else ""
    total = len(task.subtasks)
    done = task.subtasks_done
    delivered, delivery_evidence = _delivery_evidence(task)
    conclusion = verdict if terminal and not running else ""
    if not terminal:
        conclusion = ""
    return {
        "ok": True,
        "task": task.task_id,
        "phase": phase,
        "phase_label": _phase_label(phase),
        "phase_tone": _phase_tone(phase),
        "state": state_name,
        "state_label": REVIEW_STATE_NAMES.get(state_name, state_name),
        "running": running,
        "can_start": can_start,
        "disabled_reason": "" if can_start else reason,
        "ready_reason": reason if can_start else "",
        "total": total,
        "done": done,
        "delivery_ready": delivered,
        "delivery_evidence": delivery_evidence,
        "action_label": (
            "重新开始审核" if state_name == "review_failed" else "开始审核"
        ),
        "started_at": str(state.get("started_at") or ""),
        "finished_at": str(state.get("finished_at") or ""),
        "message": str(state.get("message") or ""),
        "error": str(state.get("error") or ""),
        "log": log,
        "log_url": log_url,
        "conclusion": conclusion,
        "conclusion_label": (
            "通过" if conclusion == "passed"
            else "须修正" if conclusion == "revise"
            else ""
        ),
        "has_review": task.review_path is not None,
        "review_done": review_done_path.is_file(),
        "issues": _review_issue_items(task.review_text),
        "review_role": role.id if role is not None else REVIEW_ROLE_ID,
        "review_role_name": role.name if role is not None else "",
        "review_agent": agent.id if agent is not None else "",
        "review_agent_name": agent.name if agent is not None else "",
        "review_binding_error": binding_issue,
        "attribution_required": bool(state.get("attribution_required")),
        "attribution_issue": str(state.get("attribution_issue") or ""),
        "handoff_blocked": _handoff_blocked_for(task.task_id),
        "handoff": _handoff_state_snapshot(task.task_id),
        "handoff_status_url": f"/api/task/{task.task_id}/handoff-status",
        "status_url": f"/api/task/{task.task_id}/review-status",
        "detail_url": f"/task/{task.task_id}",
    }


def _review_commit(review_state: Dict[str, object], **updates: object) -> None:
    with REVIEW_STATE_LOCK:
        review_state.update(updates)
        _write_review_state_file(review_state)


def _review_worker(
    task: Task,
    role: Role,
    agent: AgentApp,
    state: Dict[str, object],
) -> None:
    global ACTIVE_REVIEW
    try:
        spec = runner_engine.AgentSpec(
            id=agent.id,
            name=agent.name,
            command=agent.command,
            args_template=agent.args_template,
        )
        runner = _runner()
        try:
            runner.launch(
                task_id=task.task_id,
                task_dir=task.project_dir,
                agent=spec,
                round_no=1,
                workdir=task.workdir,
                prompt=_review_prompt(task, role, agent),
                kind="review",
                log_prefix="review",
                cwd=task.project_dir,
            )
        except Exception as exc:
            _review_commit(
                state,
                state="review_failed",
                finished_at=_now_iso(),
                error=f"未能启动审核 Agent：{exc}"[:2_000],
                message=f"审核未能启动：{exc}",
            )
            return

        finished = runner.wait_for_idle(runner.timeout_seconds + 120.0)
        final = runner.snapshot()
        log = str(final.get("log") or "")
        run_state = str(final.get("state") or "failed")
        runner_error = str(final.get("error") or "")
        review_path = task.docking_dir / REVIEW_NAME
        review_done_path = task.docking_dir / REVIEW_DONE_NAME

        if not finished:
            _review_commit(
                state,
                state="review_failed",
                finished_at=_now_iso(),
                log=log,
                error="等待审核 Agent 收尾超时；请检查 /runs 与日志。",
                message="审核收尾超时。",
            )
            return

        if run_state == "success":
            handoff_round = task.revise_round if task.revise_round > 0 else 0
            handoff = _run_handoff_gates(
                task,
                "review_complete",
                round_no=handoff_round,
                source="review",
                review_baseline=state.get("baseline_review"),
                review_done_baseline=state.get("baseline_review_done"),
            )
            if not bool(handoff.get("passed")):
                _quarantine_review_round(task, state)
                _review_commit(
                    state,
                    state="review_failed",
                    finished_at=_now_iso(),
                    log=log,
                    error=(
                        "交接体检未通过："
                        + _handoff_missing_text(handoff)
                    )[:2_000],
                    message="审核完成信号缺失，自动补交后仍未通过，已停止。",
                    handoff_blocked=True,
                    handoff=handoff,
                )
                return
            refreshed = _find_task(task.task_id)
            if refreshed is not None:
                task = refreshed
            review_path = task.docking_dir / REVIEW_NAME
            review_done_path = task.docking_dir / REVIEW_DONE_NAME

        if not review_path.is_file():
            error = runner_error or f"审核 Agent 以 {run_state} 状态结束，但没有写 对接/审查.md"
            _review_commit(
                state,
                state="review_failed",
                finished_at=_now_iso(),
                log=log,
                error=error[:2_000],
                message="审核没有产出 对接/审查.md，可重试。",
            )
            return
        review_text = _read_text(review_path)
        verdict = _review_verdict(review_text)
        if not _artifact_updated(review_path, state.get("baseline_review")):
            _review_commit(
                state,
                state="review_failed",
                finished_at=_now_iso(),
                log=log,
                error=(
                    "对接/审查.md 仍是上一轮内容，本轮审核没有重新写入；"
                    "不能据此判定修正结果。"
                ),
                message="本轮审核没有更新 审查.md，可重试。",
            )
            return
        attribution_issue = _review_attribution_issue(review_text, state)
        if attribution_issue:
            moved: Optional[Path] = None
            try:
                moved = _quarantine_invalid_review(task, reason="attribution")
            except OSError:
                traceback.print_exc()
            detail = _review_attribution_message(attribution_issue)
            quarantine_note = (
                f"已移入 {_display_path(moved)}。" if moved is not None else ""
            )
            _review_commit(
                state,
                state="review_failed",
                finished_at=_now_iso(),
                log=log,
                conclusion="",
                error=(detail + "；" + quarantine_note).strip("；")[:2_000],
                message=(
                    "审查署名无效，已拒收；"
                    + (quarantine_note or "请人工核对 对接/审查.md。")
                ),
                quarantined_review=_display_path(moved) if moved else "",
                attribution_issue=attribution_issue,
            )
            return
        if not review_done_path.is_file():
            _quarantine_review_round(task, state)
            _review_commit(
                state,
                state="review_failed",
                finished_at=_now_iso(),
                log=log,
                conclusion="",
                error="已写 对接/审查.md，但缺少 对接/审核完成.md 完成信号；"
                "半份审查已移入 对接/审查历史/。",
                message="审核完成信号缺失，可重试或人工核对。",
            )
            return
        if not _artifact_updated(
            review_done_path, state.get("baseline_review_done")
        ):
            _quarantine_review_round(task, state)
            _review_commit(
                state,
                state="review_failed",
                finished_at=_now_iso(),
                log=log,
                conclusion="",
                error=(
                    "对接/审核完成.md 仍是上一轮的完成信号，"
                    "本轮审核没有重新写入；半份审查已移入 对接/审查历史/。"
                ),
                message="本轮审核没有更新完成信号，可重试。",
            )
            return
        if not verdict:
            _quarantine_review_round(task, state)
            _review_commit(
                state,
                state="review_failed",
                finished_at=_now_iso(),
                log=log,
                error="对接/审查.md 没有可识别的结论（通过 / 须修正）；"
                "无效审查已移入 对接/审查历史/。",
                message="审查结论无法识别，可重试或人工核对。",
            )
            return

        note = "审核完成。"
        if run_state != "success":
            note = f"审核产物已落盘（Agent 结束状态：{run_state}）。"
        _review_commit(
            state,
            state="completed",
            finished_at=_now_iso(),
            log=log,
            conclusion=verdict,
            message=note,
            error="" if run_state == "success" else runner_error[:2_000],
        )
    except Exception as exc:
        traceback.print_exc()
        try:
            _review_commit(
                state,
                state="review_failed",
                finished_at=_now_iso(),
                error=f"审核线程异常：{exc}"[:2_000],
                message=f"审核线程异常：{exc}",
            )
        except Exception:
            traceback.print_exc()
    finally:
        with REVIEW_STATE_LOCK:
            if ACTIVE_REVIEW is not None and (
                ACTIVE_REVIEW.get("state") is state
            ):
                ACTIVE_REVIEW = None


def _start_review(
    task: Task,
    *,
    _advance_owned: bool = False,
    _allow_existing_review: bool = False,
) -> Dict[str, object]:
    """预检通过后启动审核线程；任何预检失败都不启动进程。"""
    global ACTIVE_REVIEW
    with REVIEW_START_LOCK:
        if _advance_owned and _advance_stop_requested():
            raise ActionError(
                409,
                "推进已请求停止",
                "用户已请求停止全自动推进；本次没有启动审核 Agent。",
                "等待停止完成后刷新页面。",
            )
        if ACTIVE_REVIEW is not None:
            active_task = str(ACTIVE_REVIEW.get("task") or "")
            raise ActionError(
                409,
                "已有审核在跑",
                f"{active_task or '另一个任务'} 正在审核；同一时刻只允许一个审核。",
                "等待当前审核结束后再试。",
            )
        with _task_lifecycle_lock(task.task_id):
            current = _find_task(task.task_id)
            if current is None:
                raise ActionError(
                    409,
                    "任务正在移动或已归档",
                    f"{task.task_id} 已不在 项目/ 中，本次没有启动任何进程。",
                    "刷新看板；归档任务请先恢复。",
                )
            if current.archived:
                raise ActionError(
                    409,
                    "归档任务只读",
                    f"{task.task_id} 位于 归档/，不能审核。",
                    "先从归档库恢复项目。",
                )
            task = current
        _ensure_handoff_not_blocked(task)
        if not _advance_owned:
            active_advance = _active_advance_task()
            if active_advance:
                raise ActionError(
                    409,
                    "全自动推进正在运行",
                    f"{active_advance} 的全自动推进占用执行器；本次没有启动任何进程。",
                    "等待推进结束后再手动审核。",
                )
        if _active_dispatch_task():
            raise ActionError(
                409,
                "分发正在运行",
                f"{_active_dispatch_task()} 正在分发实现；本次没有启动任何进程。",
                "等待分发结束后再审核。",
            )
        if task.subtasks_path.is_file():
            subtasks, error = _load_subtasks(
                task.subtasks_path, task.task_id
            )
            if error:
                raise ActionError(
                    400,
                    "子任务清单无法解析",
                    f"{error} 未启动任何进程。",
                    "修正 对接/subtasks.yaml 后重试。",
                )
            waiting = _review_ready_subtasks(task)
            if waiting:
                raise ActionError(
                    409,
                    "审核预检未通过",
                    "以下子任务尚未全部交付（须为 delivered），未启动任何进程："
                    + "、".join(waiting),
                    "先完成分发实现，等待全部子任务交付后再审核。",
                )
        delivered, evidence = _delivery_evidence(task)
        if not delivered:
            raise ActionError(
                409,
                "还没有可审核的交付",
                evidence + "；未启动任何进程。",
                "等待子任务交付、任务级 DONE.md 或工作区产物出现后再审核。",
            )
        terminal, _terminal_verdict = _review_terminal_state(task)
        if terminal and not _allow_existing_review:
            raise ActionError(
                409,
                "已有审核结论",
                f"{task.task_id} 的 对接/{REVIEW_NAME} 与 "
                f"对接/{REVIEW_DONE_NAME} 已构成审核终态，"
                "本次没有启动任何进程。",
                "查看现有审查意见；如需重审，请先人工处理 对接/审查.md。",
            )
        role, agent, binding_issue = _review_role_binding()
        if binding_issue or role is None or agent is None:
            detail = binding_issue or "审核角色不可用"
            title = "审核角色未绑定" if "未绑定" in detail else "审核角色不可用"
            raise ActionError(
                409,
                title,
                detail + "；本次没有启动任何进程。",
                "到 /roles/reviewer 绑定一个已登记且可执行的 Agent。",
                violations=[detail],
            )
        if ACTIVE_REGISTRATIONS:
            raise ActionError(
                409,
                "总控登记正在运行",
                "已有总控登记占用执行器；本次没有启动任何进程。",
                "等待登记结束后再审核。",
            )
        try:
            lock = _runner().lock_info()
        except Exception:
            lock = {}
        if lock.get("active"):
            raise ActionError(
                409,
                "执行器正忙",
                f"执行器正在运行 {lock.get('task') or '未知任务'}；"
                "本次没有启动任何进程。",
                "等待当前运行结束后再审核。",
            )

        handoff = _run_handoff_gates(
            task,
            "review",
            source="advance" if _advance_owned else "manual",
        )
        if not bool(handoff.get("passed")):
            raise _handoff_action_error(handoff)

        with _task_lifecycle_lock(task.task_id):
            latest = _find_task(task.task_id)
            if latest is None:
                raise ActionError(
                    409,
                    "任务正在移动或已归档",
                    f"{task.task_id} 已不在 项目/ 中，本次没有启动任何进程。",
                    "刷新看板；归档任务请先恢复。",
                )
            if latest.archived:
                raise ActionError(
                    409,
                    "归档任务只读",
                    f"{task.task_id} 位于 归档/，不能审核。",
                    "先从归档库恢复项目。",
                )
            task = latest
            if _active_dispatch_task():
                raise ActionError(
                    409,
                    "分发正在运行",
                    f"{_active_dispatch_task()} 正在分发实现；本次没有启动任何进程。",
                    "等待分发结束后再审核。",
                )
            if ACTIVE_REGISTRATIONS:
                raise ActionError(
                    409,
                    "总控登记正在运行",
                    "已有总控登记占用执行器；本次没有启动任何进程。",
                    "等待登记结束后再审核。",
                )
            if not _advance_owned:
                active_advance = _active_advance_task()
                if active_advance:
                    raise ActionError(
                        409,
                        "全自动推进正在运行",
                        f"{active_advance} 的全自动推进占用执行器；"
                        "本次没有启动任何进程。",
                        "等待推进结束后再手动审核。",
                    )
            active_handoff = _active_handoff_task()
            if active_handoff:
                raise ActionError(
                    409,
                    "交接体检正在运行",
                    f"{active_handoff} 正在检查/补交交接文件；"
                    "本次没有启动任何进程。",
                    "等待交接体检结束后再审核。",
                )
            try:
                lock = _runner().lock_info()
            except Exception:
                lock = {}
            if lock.get("active"):
                raise ActionError(
                    409,
                    "执行器正忙",
                    f"执行器正在运行 {lock.get('task') or '未知任务'}；"
                    "本次没有启动任何进程。",
                    "等待当前运行结束后再审核。",
                )
            if _handoff_blocked_for(task.task_id):
                raise ActionError(
                    409,
                    "交接体检未通过",
                    "交接体检已阻塞，本次没有启动审核。",
                    "先补齐缺件或重试体检。",
                )
            waiting = _review_ready_subtasks(task)
            if waiting:
                raise ActionError(
                    409,
                    "审核预检未通过",
                    "以下子任务尚未全部交付（须为 delivered），未启动任何进程："
                    + "、".join(waiting),
                    "先完成分发实现，等待全部子任务交付后再审核。",
                )
            delivered, evidence = _delivery_evidence(task)
            if not delivered:
                raise ActionError(
                    409,
                    "还没有可审核的交付",
                    evidence + "；未启动任何进程。",
                    "等待子任务交付、任务级 DONE.md 或工作区产物出现后再审核。",
                )
            terminal, _terminal_verdict = _review_terminal_state(task)
            if terminal and not _allow_existing_review:
                raise ActionError(
                    409,
                    "已有审核结论",
                    f"{task.task_id} 的 对接/{REVIEW_NAME} 与 "
                    f"对接/{REVIEW_DONE_NAME} 已构成审核终态，"
                    "本次没有启动任何进程。",
                    "查看现有审查意见；如需重审，请先人工处理 对接/审查.md。",
                )
            role, agent, binding_issue = _review_role_binding()
            if binding_issue or role is None or agent is None:
                detail = binding_issue or "审核角色不可用"
                title = (
                    "审核角色未绑定" if "未绑定" in detail else "审核角色不可用"
                )
                raise ActionError(
                    409,
                    title,
                    detail + "；本次没有启动任何进程。",
                    "到 /roles/reviewer 绑定一个已登记且可执行的 Agent。",
                    violations=[detail],
                )

            quarantined = ""
            terminal, _terminal_verdict = _review_terminal_state(task)
            if task.review_path is not None and not terminal:
                previous_state = _review_state_snapshot(task.task_id)
                attribution_issue = ""
                if _review_attribution_required(task, previous_state):
                    attribution_issue = _review_attribution_issue(
                        task.review_text, previous_state
                    )
                try:
                    moved = _quarantine_invalid_review(
                        task,
                        reason="attribution" if attribution_issue else "invalid",
                    )
                except OSError as exc:
                    raise ActionError(
                        500,
                        "无效审查无法隔离",
                        f"无法把不完整的 对接/{REVIEW_NAME} 移入 "
                        f"{REVIEW_HISTORY_DIRNAME}/：{exc}；本次没有启动任何进程。",
                        "检查对接目录写权限后重试。",
                    ) from exc
                quarantined = _display_path(moved) if moved is not None else ""
                refreshed = _find_task(task.task_id)
                if refreshed is not None:
                    task = refreshed

            state: Dict[str, object] = {
                "ok": True,
                "task": task.task_id,
                "state": "running",
                "role": role.id,
                "role_name": role.name,
                "agent": agent.id,
                "agent_name": agent.name,
                "attribution_required": True,
                "attribution_role": role.id,
                "attribution_role_name": role.name,
                "attribution_agent": agent.id,
                "started_at": _now_iso(),
                "finished_at": "",
                "conclusion": "",
                "message": (
                    "重新审核中… 正在复核修正后的产物与证据。"
                    if _allow_existing_review
                    else "审核中… 正在由审核角色复核全部子任务。"
                ),
                "log": "",
                "error": "",
                "quarantined_review": quarantined,
                "allow_existing_review": bool(_allow_existing_review),
                "baseline_review": _file_signature(
                    task.docking_dir / REVIEW_NAME
                ),
                "baseline_review_done": _file_signature(
                    task.docking_dir / REVIEW_DONE_NAME
                ),
            }
            initial_state = copy.deepcopy(state)
            thread = threading.Thread(
                target=_review_worker,
                args=(task, role, agent, state),
                name=f"review-{task.task_id}",
                daemon=True,
            )
            with REVIEW_STATE_LOCK:
                ACTIVE_REVIEW = {"task": task.task_id, "state": state}
                _write_review_state_file(state)
            try:
                thread.start()
            except RuntimeError as exc:
                with REVIEW_STATE_LOCK:
                    ACTIVE_REVIEW = None
                    state["state"] = "review_failed"
                    state["message"] = f"审核线程无法启动：{exc}"
                    state["finished_at"] = _now_iso()
                    state["error"] = str(exc)
                    _write_review_state_file(state)
                raise ActionError(
                    500,
                    "审核未能启动",
                    f"审核线程无法启动：{exc}",
                    "查看控制台终端错误信息后重试。",
                ) from exc
            return initial_state


# ---------------------------------------------------------------------------
# 全自动推进：分发剩余 → 自动审核 → 停在通过 / 须修正 / 失败
# ---------------------------------------------------------------------------


def _advance_state_path(task_id: str) -> Path:
    return DISPATCH_DIR / f"advance-{task_id.upper()}.status.json"


def _write_advance_state_file(state: Dict[str, object]) -> None:
    """尽力持久化全自动推进状态；页面轮询失败不应打断推进。"""
    task_id = str(state.get("task") or "")
    if not task_id:
        return
    path = _advance_state_path(task_id)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    except OSError:
        traceback.print_exc()
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _read_advance_state_file(task_id: str) -> Dict[str, object]:
    path = _advance_state_path(task_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if str(data.get("task") or "").upper() != task_id.upper():
        return {}
    return data


def _active_advance_task() -> str:
    with ADVANCE_STATE_LOCK:
        if ACTIVE_ADVANCE is None:
            return ""
        return str(ACTIVE_ADVANCE.get("task") or "")


def _advance_state_snapshot(task_id: str) -> Dict[str, object]:
    task_id = task_id.upper()
    with ADVANCE_STATE_LOCK:
        if ACTIVE_ADVANCE is not None and str(
            ACTIVE_ADVANCE.get("task") or ""
        ).upper() == task_id:
            state = copy.deepcopy(ACTIVE_ADVANCE.get("state") or {})
            assert isinstance(state, dict)
            return state
    state = _read_advance_state_file(task_id)
    if not state:
        return {}
    if str(state.get("state") or "") == "running":
        state = dict(state)
        state["state"] = "failed"
        state["message"] = (
            "上次全自动推进没有正常收尾（控制台重启或线程中断）；"
            "可重新发起推进或手动分发/审核。"
        )
        state["finished_at"] = str(state.get("finished_at") or _now_iso())
    return state


def _advance_commit(advance_state: Dict[str, object], **updates: object) -> None:
    with ADVANCE_STATE_LOCK:
        if advance_state.get("force_stopped"):
            return
        advance_state.update(updates)
        _write_advance_state_file(advance_state)


def _advance_note(
    advance_state: Dict[str, object], message: str, *, step: str = ""
) -> None:
    with ADVANCE_STATE_LOCK:
        if advance_state.get("force_stopped"):
            return
        if step:
            advance_state["step"] = step
            advance_state["step_label"] = ADVANCE_STEP_LABELS.get(step, step)
        advance_state["message"] = message
        events = advance_state.get("events")
        if not isinstance(events, list):
            events = []
            advance_state["events"] = events
        events.append(
            {
                "at": _now_iso(),
                "step": str(advance_state.get("step") or ""),
                "message": message,
            }
        )
        if len(events) > 40:
            del events[:-40]
        _write_advance_state_file(advance_state)


def _advance_budget_limits(state: Dict[str, object]) -> Dict[str, float]:
    raw = state.get("budget")
    if not isinstance(raw, dict):
        raw = {}
    def _number(key: str, default: float) -> float:
        try:
            value = float(raw.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(0.0, value)
    return {
        "max_runtime_seconds": _number(
            "max_runtime_seconds", ADVANCE_MAX_RUNTIME_SECONDS
        ),
        "max_subtasks": _number("max_subtasks", ADVANCE_MAX_SUBTASKS),
        "max_revise_rounds": _number(
            "max_revise_rounds", ADVANCE_MAX_REVISE_ROUNDS
        ),
    }


def _advance_started_epoch(state: Dict[str, object]) -> float:
    try:
        value = float(state.get("started_epoch") or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0:
        return value
    started_at = str(state.get("started_at") or "")
    if started_at:
        try:
            return datetime.fromisoformat(started_at).timestamp()
        except ValueError:
            pass
    return 0.0


def _advance_budget_snapshot(state: Dict[str, object]) -> Dict[str, object]:
    limits = _advance_budget_limits(state)
    started_epoch = _advance_started_epoch(state)
    runtime = max(0.0, time.time() - started_epoch) if started_epoch else 0.0
    try:
        subtasks = int(state.get("subtasks_started") or 0)
    except (TypeError, ValueError):
        subtasks = 0
    try:
        revise_round = int(state.get("revise_round") or 0)
    except (TypeError, ValueError):
        revise_round = 0
    violations: List[str] = []
    if (
        limits["max_runtime_seconds"] > 0
        and runtime >= limits["max_runtime_seconds"]
    ):
        violations.append(
            "运行时长超限：已运行 "
            f"{runtime:.0f} 秒，上限 {limits['max_runtime_seconds']:.0f} 秒"
        )
    if limits["max_subtasks"] > 0 and subtasks > limits["max_subtasks"]:
        violations.append(
            f"子任务超限：已启动 {subtasks} 个，"
            f"上限 {limits['max_subtasks']:.0f} 个"
        )
    if (
        limits["max_revise_rounds"] > 0
        and revise_round > limits["max_revise_rounds"]
    ):
        violations.append(
            f"修订轮次超限：第 {revise_round} 轮，"
            f"上限 {limits['max_revise_rounds']:.0f} 轮"
        )
    diagnosed = str(state.get("budget_violation") or "").strip()
    if diagnosed and diagnosed not in violations:
        violations.insert(0, diagnosed)
    return {
        "limits": limits,
        "used": {
            "runtime_seconds": round(runtime, 1),
            "subtasks": subtasks,
            "revise_round": revise_round,
        },
        "exceeded": bool(violations),
        "reason": "；".join(violations),
    }


def _advance_budget_stop_reason(
    state: Dict[str, object],
    *,
    planned_subtasks: int = 0,
    revise_round: Optional[int] = None,
) -> str:
    snapshot = _advance_budget_snapshot(state)
    if snapshot["exceeded"]:
        return str(snapshot["reason"])
    limits = snapshot["limits"]
    assert isinstance(limits, dict)
    used = snapshot["used"]
    assert isinstance(used, dict)
    if (
        planned_subtasks > 0
        and limits["max_subtasks"] > 0
        and int(used["subtasks"]) + planned_subtasks
        > int(limits["max_subtasks"])
    ):
        return (
            "子任务预算超出：本次推进已启动 "
            f"{used['subtasks']} 个，准备再启动 {planned_subtasks} 个，"
            f"上限 {limits['max_subtasks']:.0f} 个"
        )
    if (
        revise_round is not None
        and limits["max_revise_rounds"] > 0
        and revise_round > limits["max_revise_rounds"]
    ):
        return (
            f"修订轮次预算超出：第 {revise_round} 轮，"
            f"上限 {limits['max_revise_rounds']:.0f} 轮"
        )
    return ""


def _advance_stop_requested() -> bool:
    with ADVANCE_STATE_LOCK:
        if ACTIVE_ADVANCE is None:
            return False
        state = ACTIVE_ADVANCE.get("state")
        if not isinstance(state, dict):
            return False
        return bool(state.get("stop_requested"))


def _active_advance_abort(task_id: str = "") -> Tuple[str, str]:
    """当前活动的推进是否要求中止；供交接补交等同步步骤检查。"""
    with ADVANCE_STATE_LOCK:
        if ACTIVE_ADVANCE is None:
            return "", ""
        if (
            task_id
            and str(ACTIVE_ADVANCE.get("task") or "").upper()
            != task_id.strip().upper()
        ):
            return "", ""
        state = ACTIVE_ADVANCE.get("state")
        if not isinstance(state, dict):
            return "", ""
        return _advance_abort_reason(state)


def _advance_abort_reason(
    advance_state: Dict[str, object],
) -> Tuple[str, str]:
    """返回 (stopped|stopped_with_budget, 原因)；空串表示继续。"""
    with ADVANCE_STATE_LOCK:
        if advance_state.get("stop_requested"):
            return "stopped", "用户请求停止全自动推进"
        budget_reason = _advance_budget_stop_reason(advance_state)
    if budget_reason:
        return "stopped_with_budget", budget_reason
    return "", ""


def _stop_active_runner_for_task(task_id: str, reason: str) -> bool:
    try:
        stopped = _runner().stop_active(reason, task_id=task_id)
    except Exception:
        traceback.print_exc()
        stopped = False
    try:
        _runner().release_stale_lock_for_task(task_id)
    except Exception:
        traceback.print_exc()
    return stopped


def _advance_finish(
    advance_state: Dict[str, object],
    state_name: str,
    message: str,
    *,
    error: str = "",
    conclusion: str = "",
    handoff: Optional[Dict[str, object]] = None,
) -> None:
    if advance_state.get("force_stopped"):
        return
    if state_name == "stopped_with_budget" and error:
        advance_state["budget_violation"] = error
    if (
        advance_state.get("stop_requested")
        and state_name in {"failed", "handoff_blocked"}
    ):
        state_name = "stopped"
        message = "用户已请求停止；当前步骤已终止，执行器锁已释放。"
        error = ""
        conclusion = ""
        handoff = None
    updates: Dict[str, object] = {
        "state": state_name,
        "step": "done",
        "step_label": ADVANCE_STEP_LABELS["done"],
        "message": message,
        "error": error[:2_000],
        "conclusion": conclusion,
        "finished_at": _now_iso(),
        "budget": _advance_budget_snapshot(advance_state),
        "stop_requested": bool(advance_state.get("stop_requested")),
    }
    if handoff is not None:
        updates.update(
            {
                "handoff_blocked": state_name == "handoff_blocked",
                "handoff_stage": str(handoff.get("stage") or ""),
                "handoff_stage_label": str(handoff.get("stage_label") or ""),
                "handoff_checks": handoff.get("checks") or [],
                "handoff_missing": handoff.get("missing") or [],
                "handoff_attempts": handoff.get("attempts") or {},
                "handoff_log": str(handoff.get("log") or ""),
                "handoff_agent_runs": handoff.get("agent_runs") or [],
            }
        )
    with ADVANCE_STATE_LOCK:
        advance_state.update(updates)
        events = advance_state.get("events")
        if not isinstance(events, list):
            events = []
            advance_state["events"] = events
        events.append(
            {"at": _now_iso(), "step": "done", "message": message}
        )
        if len(events) > 40:
            del events[:-40]
        _write_advance_state_file(advance_state)
    if advance_state.get("revise") and advance_state.get("revise_log"):
        log_path = Path(str(advance_state.get("revise_log") or ""))
        if not log_path.is_absolute():
            log_path = PROJECT_DIR / log_path
        _append_revise_log(
            log_path,
            [
                f"- 结束：{ADVANCE_STATE_NAMES.get(state_name, state_name)}",
                f"- 结论：{conclusion or '—'}",
                f"- 说明：{message}",
                f"- 结束时间：{advance_state.get('finished_at') or _now_iso()}",
            ],
        )


def _advance_availability(task: Task) -> Tuple[bool, str]:
    if task.archived:
        return False, "归档任务只读，不能全自动推进。"
    if _handoff_blocked_for(task.task_id):
        return False, "交接体检未通过；请先点「重试体检」或人工补齐缺件。"
    if _active_handoff_task():
        return False, "交接体检或自动补交正在进行，请等待结束。"
    terminal, terminal_verdict = _review_terminal_state(task)
    if terminal:
        verdict = "须修正" if terminal_verdict == "revise" else "通过"
        return False, (
            f"任务已有审核结论（{verdict}；终态需 审查.md + 审核完成.md），"
            "无需再次推进。"
        )
    active_advance = _active_advance_task()
    if active_advance:
        if active_advance.upper() == task.task_id:
            return False, "全自动推进正在进行，请等待当前步骤结束。"
        return False, f"{active_advance} 的全自动推进正在运行，请等待结束后再试。"
    if _active_dispatch_task():
        return False, f"{_active_dispatch_task()} 正在分发实现，请等待结束。"
    if _review_active_for(task.task_id):
        return False, "审核正在运行，请等待结束后再试。"
    if ACTIVE_REGISTRATIONS:
        return False, "总控登记正在运行，请等待登记结束后再试。"
    try:
        lock = _runner().lock_info()
    except Exception:
        lock = {}
    if lock.get("active"):
        return False, (
            f"执行器正忙（{lock.get('task') or '未知任务'}），"
            "请等待当前运行结束。"
        )

    if _registration_aware(task) and _registration_structure_incomplete(task):
        _role, agent, issue = _handoff_role_agent(task, CONTROLLER_ROLE_ID)
        if agent is None:
            return False, "登记交接文件不完整，且总控不可用：" + (
                issue or "没有可执行的 controller Agent。"
            )
        return True, "登记交接文件不完整，将先回叫总控补交，补完继续推进。"

    targets = _dispatch_targets(task)
    if targets:
        if not task.subtasks_path.is_file():
            return False, "没有 对接/subtasks.yaml，无法分发剩余子任务。"
        if task.subtasks_error:
            return False, f"subtasks.yaml 无法解析：{task.subtasks_error}"
        roles, _role_issues = _load_roles()
        agents, _agent_issues = _load_agents()
        _bindings, violations = _dispatch_preflight(targets, roles, agents)
        if violations:
            return False, "分发预检未通过：" + "；".join(violations)
    else:
        waiting = _review_ready_subtasks(task)
        if waiting:
            return False, (
                "有子任务处于未完成状态（非 pending/failed）："
                + "、".join(waiting)
            )
        delivered, evidence = _delivery_evidence(task)
        if not delivered:
            return False, evidence + "；没有可推进的步骤。"
    _review_role, _review_agent, binding_issue = _review_role_binding()
    if binding_issue:
        return False, "自动审核无法开始：" + binding_issue
    if targets:
        return True, (
            f"将先继续分发 {len(targets)} 个待开始/失败子任务，"
            "全部交付后自动审核。"
        )
    return True, "交付物已就绪，将直接开始自动审核。"


def _advance_finish_handoff(
    advance_state: Dict[str, object], handoff: Dict[str, object]
) -> None:
    missing = _handoff_missing_text(handoff)
    log = str(handoff.get("log") or "")
    message = (
        "交接体检未通过，全自动推进已停止。缺件："
        + missing
        + "。请人工处理或点「重试体检」。"
        + (f" 交接日志：{log}" if log else "")
    )
    _advance_finish(
        advance_state,
        "handoff_blocked",
        message,
        error="交接体检未通过： " + missing,
        handoff=handoff,
    )


def _wait_for_dispatch_finish(
    task_id: str,
    timeout: float,
    advance_state: Optional[Dict[str, object]] = None,
) -> Tuple[bool, Dict[str, object]]:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if advance_state is not None:
            abort_state, abort_reason = _advance_abort_reason(advance_state)
            if abort_state:
                _stop_active_runner_for_task(task_id, abort_reason)
                snapshot = dict(_dispatch_state_snapshot(task_id))
                snapshot.update(
                    {
                        "aborted": True,
                        "abort_state": abort_state,
                        "abort_reason": abort_reason,
                    }
                )
                return False, snapshot
        state = _dispatch_state_snapshot(task_id)
        snapshot = dict(state)
        if str(snapshot.get("state") or "") in {"completed", "failed"}:
            if _active_dispatch_task().upper() != task_id.upper():
                return True, snapshot
        time.sleep(0.08)
    return False, _dispatch_state_snapshot(task_id)


def _wait_for_review_finish(
    task_id: str,
    timeout: float,
    advance_state: Optional[Dict[str, object]] = None,
) -> Tuple[bool, Dict[str, object]]:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if advance_state is not None:
            abort_state, abort_reason = _advance_abort_reason(advance_state)
            if abort_state:
                _stop_active_runner_for_task(task_id, abort_reason)
                snapshot = dict(_review_state_snapshot(task_id))
                snapshot.update(
                    {
                        "aborted": True,
                        "abort_state": abort_state,
                        "abort_reason": abort_reason,
                    }
                )
                return False, snapshot
        state = _review_state_snapshot(task_id)
        snapshot = dict(state)
        if str(snapshot.get("state") or "") in {"completed", "review_failed"}:
            if _active_review_task().upper() != task_id.upper():
                return True, snapshot
        time.sleep(0.08)
    return False, _review_state_snapshot(task_id)


def _wait_runner_idle_for_advance(
    runner: runner_engine.Runner,
    task_id: str,
    advance_state: Dict[str, object],
    timeout: float,
) -> bool:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if runner.wait_for_idle(0.15):
            return True
        abort_state, abort_reason = _advance_abort_reason(advance_state)
        if abort_state:
            _stop_active_runner_for_task(task_id, abort_reason)
            return False
    return runner.wait_for_idle(0.0)


def _advance_worker(task_id: str, advance_state: Dict[str, object]) -> None:
    global ACTIVE_ADVANCE
    try:
        for _iteration in range(12):
            task = _find_task(task_id)
            if task is None:
                _advance_finish(
                    advance_state,
                    "failed",
                    f"找不到任务 {task_id}，全自动推进已停止。",
                    error=f"任务 {task_id} 不在活动项目目录中。",
                )
                return
            abort_state, abort_reason = _advance_abort_reason(advance_state)
            if abort_state:
                _stop_active_runner_for_task(task_id, abort_reason)
                _advance_finish(
                    advance_state,
                    abort_state,
                    (
                        "用户停止推进；当前步骤已终止，执行器锁已释放。"
                        if abort_state == "stopped"
                        else "推进预算用尽，已停止：" + abort_reason
                    ),
                    error="" if abort_state == "stopped" else abort_reason,
                )
                return
            if (
                not advance_state.get("whole_rework")
                and _registration_aware(task)
                and _registration_structure_incomplete(task)
            ):
                _advance_note(
                    advance_state,
                    "登记交接文件不完整，正在回叫总控补交…",
                    step="handoff",
                )
                handoff = _run_handoff_gates(
                    task, "dispatch", source="advance"
                )
                abort_state, abort_reason = _advance_abort_reason(advance_state)
                if abort_state:
                    _stop_active_runner_for_task(task_id, abort_reason)
                    _advance_finish(
                        advance_state,
                        abort_state,
                        (
                            "用户停止推进；当前步骤已终止，执行器锁已释放。"
                            if abort_state == "stopped"
                            else "推进预算用尽，已停止：" + abort_reason
                        ),
                        error="" if abort_state == "stopped" else abort_reason,
                    )
                    return
                if not bool(handoff.get("passed")):
                    _advance_finish_handoff(advance_state, handoff)
                    return
                continue

            if bool(advance_state.get("whole_rework")) and not bool(
                advance_state.get("whole_ran")
            ):
                round_no = max(
                    1,
                    int(
                        advance_state.get("revise_round")
                        or task.revise_round
                        or 1
                    ),
                )
                context = advance_state.get("revise_context")
                if not isinstance(context, dict):
                    context = {}
                _owner_role, owner_agent, owner_issue = _owner_role_binding(task)
                if _owner_role is None or owner_agent is None:
                    _advance_finish(
                        advance_state,
                        "failed",
                        "整单重修无法开始："
                        + (owner_issue or "owner_role 不可用"),
                        error=owner_issue or "owner_role 不可用",
                    )
                    return
                done_raw = str(context.get("done_path") or "")
                done_path = (
                    Path(done_raw)
                    if done_raw
                    else _whole_rework_done_path(task, round_no)
                )
                snapshot_path = str(context.get("review_snapshot") or "")
                prompt = _whole_rework_prompt(
                    task,
                    _owner_role,
                    round_no,
                    snapshot_path,
                    done_path,
                    context,
                )
                budget_reason = _advance_budget_stop_reason(
                    advance_state,
                    planned_subtasks=1,
                    revise_round=round_no,
                )
                if budget_reason:
                    _advance_finish(
                        advance_state,
                        "stopped_with_budget",
                        "推进预算用尽，已停止：" + budget_reason,
                        error=budget_reason,
                    )
                    return
                _advance_commit(
                    advance_state,
                    subtasks_started=int(
                        advance_state.get("subtasks_started") or 0
                    )
                    + 1,
                )
                _advance_note(
                    advance_state,
                    f"整单重修中… 回叫 owner_role={task.owner_role} 重做整单。",
                    step="dispatch",
                )
                spec = runner_engine.AgentSpec(
                    id=owner_agent.id,
                    name=owner_agent.name,
                    command=owner_agent.command,
                    args_template=owner_agent.args_template,
                )
                log_prefix = (
                    "rework-"
                    + re.sub(r"[^A-Za-z0-9_-]+", "_", task.task_id)[:20]
                    + f"-r{round_no}"
                )
                try:
                    runner = _runner()
                    launched = runner.launch(
                        task_id=task.task_id,
                        task_dir=task.project_dir,
                        agent=spec,
                        round_no=round_no,
                        workdir=task.workdir,
                        prompt=prompt,
                        kind="whole_rework",
                        log_prefix=log_prefix,
                    )
                    finished = _wait_runner_idle_for_advance(
                        runner,
                        task.task_id,
                        advance_state,
                        runner.timeout_seconds + 120.0,
                    )
                    final = dict(runner.snapshot())
                except Exception as exc:
                    abort_state, abort_reason = _advance_abort_reason(
                        advance_state
                    )
                    if abort_state:
                        _advance_finish(
                            advance_state,
                            abort_state,
                            (
                                "用户停止推进；当前步骤已终止，执行器锁已释放。"
                                if abort_state == "stopped"
                                else "推进预算用尽，已停止：" + abort_reason
                            ),
                            error=""
                            if abort_state == "stopped"
                            else abort_reason,
                        )
                        return
                    _advance_finish(
                        advance_state,
                        "failed",
                        f"整单重修未能启动或收尾：{exc}",
                        error=str(exc),
                    )
                    return
                abort_state, abort_reason = _advance_abort_reason(advance_state)
                if abort_state:
                    _advance_finish(
                        advance_state,
                        abort_state,
                        (
                            "用户停止推进；当前步骤已终止，执行器锁已释放。"
                            if abort_state == "stopped"
                            else "推进预算用尽，已停止：" + abort_reason
                        ),
                        error="" if abort_state == "stopped" else abort_reason,
                    )
                    return
                result_state = (
                    str(final.get("state") or "failed")
                    if finished
                    else "failed"
                )
                error = str(final.get("error") or "")
                if not finished:
                    error = "等待整单重修收尾超时。"
                elif result_state != "success" and not error:
                    error = f"Agent 以 {result_state} 状态结束。"
                if result_state != "success":
                    _advance_finish(
                        advance_state,
                        "failed",
                        "整单重修失败，全自动推进已停止：" + error,
                        error=error,
                    )
                    return
                if not done_path.is_file():
                    error = f"整单重修结束但没有写 {done_path}"
                    _advance_finish(
                        advance_state,
                        "failed",
                        error,
                        error=error,
                    )
                    return
                try:
                    done_text = _read_text(done_path)
                except OSError as exc:
                    _advance_finish(
                        advance_state,
                        "failed",
                        f"无法读取整单重修 DONE.md：{exc}",
                        error=str(exc),
                    )
                    return
                absent = [
                    marker
                    for marker in HANDOFF_REQUIRED_DONE_MARKERS
                    if marker not in done_text
                ]
                if absent:
                    error = (
                        f"{done_path.name} 缺少章节："
                        + "/".join(HANDOFF_REQUIRED_DONE_MARKERS)
                        + f"（缺 {'、'.join(absent)}）"
                    )
                    _advance_finish(
                        advance_state,
                        "failed",
                        error,
                        error=error,
                    )
                    return
                _advance_commit(
                    advance_state,
                    whole_ran=True,
                    whole_done_path=_display_path(done_path),
                    whole_run_id=str(launched.get("run_id") or ""),
                    whole_log=str(final.get("log") or launched.get("log") or ""),
                )
                _advance_note(
                    advance_state,
                    "整单重修已写新 DONE.md，准备自动审核。",
                    step="review",
                )
                continue

            terminal, terminal_verdict = _review_terminal_state(task)
            if terminal and not advance_state.get("revise"):
                if terminal_verdict == "revise":
                    _advance_finish(
                        advance_state,
                        "stopped",
                        "审核结论为「须修正」；已停止，等待人工处理。",
                        conclusion="revise",
                    )
                else:
                    _advance_finish(
                        advance_state,
                        "completed",
                        "审核结论为「通过」；全自动推进完成。",
                        conclusion="passed",
                    )
                return

            targets = _dispatch_targets(task)
            if targets:
                budget_reason = _advance_budget_stop_reason(
                    advance_state, planned_subtasks=len(targets)
                )
                if budget_reason:
                    _advance_finish(
                        advance_state,
                        "stopped_with_budget",
                        "推进预算用尽，已停止：" + budget_reason,
                        error=budget_reason,
                    )
                    return
                _advance_commit(
                    advance_state,
                    subtasks_started=int(
                        advance_state.get("subtasks_started") or 0
                    )
                    + len(targets),
                )
                _advance_note(
                    advance_state,
                    "正在继续分发 "
                    f"{len(targets)} 个待开始/失败子任务…",
                    step="dispatch",
                )
                revise_context = advance_state.get("revise_context")
                try:
                    _start_dispatch(
                        task,
                        _advance_owned=True,
                        revise_context=(
                            revise_context
                            if isinstance(revise_context, dict)
                            else None
                        ),
                    )
                except ActionError as exc:
                    abort_state, abort_reason = _advance_abort_reason(
                        advance_state
                    )
                    if abort_state:
                        _advance_finish(
                            advance_state,
                            abort_state,
                            (
                                "用户停止推进；当前步骤已终止，执行器锁已释放。"
                                if abort_state == "stopped"
                                else "推进预算用尽，已停止：" + abort_reason
                            ),
                            error=""
                            if abort_state == "stopped"
                            else abort_reason,
                        )
                        return
                    handoff = exc.extra.get("handoff")
                    if isinstance(handoff, dict) and str(
                        handoff.get("state") or ""
                    ) == "blocked":
                        _advance_finish_handoff(advance_state, handoff)
                        return
                    _advance_finish(
                        advance_state,
                        "failed",
                        f"分发未能启动：{exc.message}",
                        error=exc.message,
                    )
                    return
                except Exception as exc:
                    abort_state, abort_reason = _advance_abort_reason(
                        advance_state
                    )
                    if abort_state:
                        _advance_finish(
                            advance_state,
                            abort_state,
                            (
                                "用户停止推进；当前步骤已终止，执行器锁已释放。"
                                if abort_state == "stopped"
                                else "推进预算用尽，已停止：" + abort_reason
                            ),
                            error=""
                            if abort_state == "stopped"
                            else abort_reason,
                        )
                        return
                    _advance_finish(
                        advance_state,
                        "failed",
                        f"分发未能启动：{exc}",
                        error=str(exc),
                    )
                    return

                timeout = (
                    (RUNNER_TIMEOUT_SECONDS + 180.0)
                    * max(1, len(targets))
                    + 60.0
                )
                completed, final = _wait_for_dispatch_finish(
                    task_id, timeout, advance_state
                )
                abort_state, abort_reason = _advance_abort_reason(advance_state)
                if abort_state:
                    _advance_finish(
                        advance_state,
                        abort_state,
                        (
                            "用户停止推进；当前步骤已终止，执行器锁已释放。"
                            if abort_state == "stopped"
                            else "推进预算用尽，已停止：" + abort_reason
                        ),
                        error="" if abort_state == "stopped" else abort_reason,
                    )
                    return
                if not completed:
                    _advance_finish(
                        advance_state,
                        "failed",
                        "等待分发收尾超时；全自动推进已停止。",
                        error="分发未在预期时间内结束；请到 /runs 检查。",
                    )
                    return
                if str(final.get("state") or "") != "completed":
                    detail = str(final.get("message") or final.get("error") or "分发失败。")
                    _advance_finish(
                        advance_state,
                        "failed",
                        f"分发失败，全自动推进已停止：{detail}",
                        error=detail,
                    )
                    return

                refreshed = _find_task(task_id)
                if refreshed is None:
                    _advance_finish(
                        advance_state,
                        "failed",
                        f"分发结束后找不到任务 {task_id}。",
                        error="任务项目目录消失。",
                    )
                    return
                remaining = _dispatch_targets(refreshed)
                if remaining:
                    detail = "分发结束后仍有待开始/失败子任务：" + "、".join(
                        item.id for item in remaining
                    )
                    _advance_finish(
                        advance_state,
                        "failed",
                        detail,
                        error=detail,
                    )
                    return
                if refreshed.subtasks and not _subtasks_all_delivered(refreshed):
                    detail = "分发结束后存在未交付子任务，已停止。"
                    _advance_finish(
                        advance_state,
                        "failed",
                        detail,
                        error=(
                            "子任务状态："
                            + "、".join(
                                f"{item.id}={item.status}"
                                for item in refreshed.subtasks
                            )
                        ),
                    )
                    return
                _advance_note(
                    advance_state,
                    "子任务已全部交付，准备自动审核。",
                    step="review",
                )
                continue

            waiting = _review_ready_subtasks(task)
            if waiting:
                detail = "存在未完成子任务：" + "、".join(waiting)
                _advance_finish(
                    advance_state,
                    "failed",
                    detail + "；全自动推进已停止。",
                    error=detail,
                )
                return
            delivered, evidence = _delivery_evidence(task)
            if not delivered:
                _advance_finish(
                    advance_state,
                    "failed",
                    evidence + "；没有可推进的步骤。",
                    error=evidence,
                )
                return

            _advance_note(
                advance_state,
                "全部交付已就绪，正在自动审核…",
                step="review",
            )
            try:
                _start_review(
                    task,
                    _advance_owned=True,
                    _allow_existing_review=bool(advance_state.get("revise")),
                )
            except ActionError as exc:
                abort_state, abort_reason = _advance_abort_reason(advance_state)
                if abort_state:
                    _advance_finish(
                        advance_state,
                        abort_state,
                        (
                            "用户停止推进；当前步骤已终止，执行器锁已释放。"
                            if abort_state == "stopped"
                            else "推进预算用尽，已停止：" + abort_reason
                        ),
                        error="" if abort_state == "stopped" else abort_reason,
                    )
                    return
                handoff = exc.extra.get("handoff")
                if isinstance(handoff, dict) and str(
                    handoff.get("state") or ""
                ) == "blocked":
                    _advance_finish_handoff(advance_state, handoff)
                    return
                _advance_finish(
                    advance_state,
                    "failed",
                    f"自动审核未能启动：{exc.message}",
                    error=exc.message,
                )
                return
            except Exception as exc:
                abort_state, abort_reason = _advance_abort_reason(advance_state)
                if abort_state:
                    _advance_finish(
                        advance_state,
                        abort_state,
                        (
                            "用户停止推进；当前步骤已终止，执行器锁已释放。"
                            if abort_state == "stopped"
                            else "推进预算用尽，已停止：" + abort_reason
                        ),
                        error="" if abort_state == "stopped" else abort_reason,
                    )
                    return
                _advance_finish(
                    advance_state,
                    "failed",
                    f"自动审核未能启动：{exc}",
                    error=str(exc),
                )
                return

            completed, final = _wait_for_review_finish(
                task_id, RUNNER_TIMEOUT_SECONDS + 180.0, advance_state
            )
            abort_state, abort_reason = _advance_abort_reason(advance_state)
            if abort_state:
                _advance_finish(
                    advance_state,
                    abort_state,
                    (
                        "用户停止推进；当前步骤已终止，执行器锁已释放。"
                        if abort_state == "stopped"
                        else "推进预算用尽，已停止：" + abort_reason
                    ),
                    error="" if abort_state == "stopped" else abort_reason,
                )
                return
            if not completed:
                _advance_finish(
                    advance_state,
                    "failed",
                    "等待审核收尾超时；全自动推进已停止。",
                    error="审核未在预期时间内结束；请到 /runs 检查。",
                )
                return

            if _handoff_blocked_for(task_id):
                _advance_finish_handoff(
                    advance_state, _handoff_state_snapshot(task_id)
                )
                return

            review_completed = str(final.get("state") or "") == "completed"
            refreshed = _find_task(task_id)
            terminal, verdict = (
                _review_terminal_state(refreshed)
                if refreshed is not None
                else (False, "")
            )
            if review_completed and refreshed is not None and terminal:
                if verdict == "revise":
                    _advance_finish(
                        advance_state,
                        "stopped",
                        (
                            "整单重修完成，但审核结论仍为「须修正」；"
                            "已停止，可进行下一轮修正。"
                            if advance_state.get("whole_rework")
                            else (
                                "继续修正完成，但审核结论仍为「须修正」；"
                                "已停止，可进行下一轮修正。"
                                if advance_state.get("revise")
                                else "审核结论为「须修正」；已停止，等待人工处理。"
                            )
                        ),
                        conclusion="revise",
                    )
                    return
                if verdict == "passed":
                    _advance_finish(
                        advance_state,
                        "completed",
                        (
                            "整单重修完成，审核结论为「通过」。"
                            if advance_state.get("whole_rework")
                            else (
                                "继续修正完成，审核结论为「通过」。"
                                if advance_state.get("revise")
                                else "审核结论为「通过」；全自动推进完成。"
                            )
                        ),
                        conclusion="passed",
                    )
                    return

            detail = str(
                final.get("error")
                or final.get("message")
                or "审核结束但没有产出可识别的 对接/审查.md。"
            )
            _advance_finish(
                advance_state,
                "failed",
                f"自动审核失败，全自动推进已停止：{detail}",
                error=detail,
            )
            return

        _advance_finish(
            advance_state,
            "failed",
            "推进步骤超过安全上限，已停止。",
            error="全自动推进循环超过 12 步。",
        )
    except Exception as exc:
        traceback.print_exc()
        try:
            _advance_finish(
                advance_state,
                "failed",
                f"全自动推进线程异常：{exc}",
                error=str(exc),
            )
        except Exception:
            traceback.print_exc()
    finally:
        with ADVANCE_STATE_LOCK:
            if ACTIVE_ADVANCE is not None and (
                ACTIVE_ADVANCE.get("state") is advance_state
            ):
                ACTIVE_ADVANCE = None


def _start_advance(
    task: Task,
    *,
    auto: bool = False,
    revise: bool = False,
    whole_rework: bool = False,
    _prechecked: bool = False,
    revise_round: int = 0,
    strategy: str = "",
    affected_ids: Optional[List[str]] = None,
    revise_log: str = "",
    revise_context: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """预检后启动全自动推进线程；预检失败不会启动任何进程。"""
    global ACTIVE_ADVANCE
    with _task_lifecycle_lock(task.task_id):
        with ADVANCE_START_LOCK:
            current = _find_task(task.task_id)
            if current is None:
                raise ActionError(
                    409,
                    "任务正在移动或已归档",
                    f"{task.task_id} 已不在 项目/ 中，本次没有启动任何进程。",
                    "刷新看板；归档任务请先恢复。",
                )
            if current.archived:
                raise ActionError(
                    409,
                    "归档任务只读",
                    f"{task.task_id} 位于 归档/，不能全自动推进。",
                    "先从归档库恢复项目。",
                )
            task = current
            if ACTIVE_ADVANCE is not None:
                active_task = str(ACTIVE_ADVANCE.get("task") or "")
                raise ActionError(
                    409,
                    "已有全自动推进在跑",
                    f"{active_task or '另一个任务'} 正在全自动推进；"
                    "同一时刻只允许一个全自动推进。",
                    "等待当前推进结束后再试。",
                )
            if not _prechecked:
                if revise:
                    available, reason = _revise_availability(task)
                else:
                    available, reason = _advance_availability(task)
                if not available:
                    raise ActionError(
                        409,
                        "无法全自动推进",
                        reason,
                        "处理阻塞原因后重试；也可以手动分发或审核。",
                    )

            effective_round = max(
                int(revise_round),
                int(task.revise_round or 0),
                0,
            )
            started_epoch = time.time()
            state: Dict[str, object] = {
                "ok": True,
                "task": task.task_id,
                "state": "running",
                "step": "starting",
                "step_label": ADVANCE_STEP_LABELS["starting"],
                "auto": bool(auto),
                "revise": bool(revise),
                "whole_rework": bool(whole_rework),
                "whole_ran": False,
                "revise_round": effective_round,
                "revise_strategy": strategy,
                "revise_targets": list(affected_ids or []),
                "revise_log": revise_log,
                "revise_context": dict(revise_context or {}),
                "started_at": _now_iso(),
                "started_epoch": started_epoch,
                "finished_at": "",
                "subtasks_started": 0,
                "stop_requested": False,
                "stop_requested_at": "",
                "budget": {
                    "max_runtime_seconds": ADVANCE_MAX_RUNTIME_SECONDS,
                    "max_subtasks": ADVANCE_MAX_SUBTASKS,
                    "max_revise_rounds": ADVANCE_MAX_REVISE_ROUNDS,
                },
                "message": (
                    "整单重修已启动，准备回叫 owner_role 并自动审核…"
                    if whole_rework
                    else (
                        "继续修正已启动，准备重新分发/审核…"
                        if revise
                        else "全自动推进已启动，准备分发/审核…"
                    )
                ),
                "error": "",
                "conclusion": "",
                "events": [],
            }
            initial_state = copy.deepcopy(state)
            thread = threading.Thread(
                target=_advance_worker,
                args=(task.task_id, state),
                name=f"advance-{task.task_id}",
                daemon=True,
            )
            with ADVANCE_STATE_LOCK:
                ACTIVE_ADVANCE = {"task": task.task_id, "state": state}
                _write_advance_state_file(state)
            try:
                thread.start()
            except RuntimeError as exc:
                with ADVANCE_STATE_LOCK:
                    ACTIVE_ADVANCE = None
                    state["state"] = "failed"
                    state["finished_at"] = _now_iso()
                    state["message"] = f"全自动推进线程无法启动：{exc}"
                    state["error"] = str(exc)
                    _write_advance_state_file(state)
                raise ActionError(
                    500,
                    "全自动推进未能启动",
                    f"全自动推进线程无法启动：{exc}",
                    "查看控制台终端错误信息后重试。",
                ) from exc
            return initial_state


def _stop_advance(task_id: str) -> Dict[str, object]:
    """用户显式停止全自动推进：终止当前 Agent，等待线程收尾并确认锁释放。"""
    global ACTIVE_ADVANCE
    task_id = task_id.strip().upper()
    with ADVANCE_STATE_LOCK:
        active = ACTIVE_ADVANCE
        if active is None or str(active.get("task") or "").upper() != task_id:
            raise ActionError(
                409,
                "没有正在进行的全自动推进",
                f"{task_id} 当前没有运行中的全自动推进，未终止任何进程。",
                "刷新页面查看当前状态。",
            )
        state = active.get("state")
        if not isinstance(state, dict) or str(state.get("state") or "") != "running":
            raise ActionError(
                409,
                "全自动推进不在运行中",
                f"{task_id} 的推进已经结束或正在收尾，未重复终止。",
                "刷新页面查看最终状态。",
            )
        state["stop_requested"] = True
        state["stop_requested_at"] = _now_iso()
        state["message"] = "已请求停止全自动推进，正在终止当前步骤…"
        events = state.get("events")
        if not isinstance(events, list):
            events = []
            state["events"] = events
        events.append(
            {
                "at": _now_iso(),
                "step": str(state.get("step") or ""),
                "message": "收到「停止推进」请求。",
            }
        )
        if len(events) > 40:
            del events[:-40]
        _write_advance_state_file(state)

    reason = "用户停止全自动推进"
    deadline = time.monotonic() + 20.0
    next_stop = 0.0
    while time.monotonic() < deadline:
        if _active_advance_task().upper() != task_id:
            break
        now = time.monotonic()
        if now >= next_stop:
            _stop_active_runner_for_task(task_id, reason)
            next_stop = now + 0.5
        time.sleep(0.05)

    final = _advance_state_snapshot(task_id)
    if str(final.get("state") or "") == "running":
        # 推进线程卡在不可中断的同步步骤：强制收口并阻止后续写入。
        with ADVANCE_STATE_LOCK:
            active = ACTIVE_ADVANCE
            if active is not None and str(active.get("task") or "").upper() == task_id:
                state = active.get("state")
                if isinstance(state, dict):
                    state.update(
                        {
                            "state": "stopped",
                            "step": "done",
                            "step_label": ADVANCE_STEP_LABELS["done"],
                            "message": (
                                "用户停止推进；推进线程未在 20 秒内退出，"
                                "已强制收口并释放执行器锁。"
                            ),
                            "error": "",
                            "conclusion": "",
                            "finished_at": _now_iso(),
                            "budget": _advance_budget_snapshot(state),
                            "stop_requested": True,
                        }
                    )
                    _write_advance_state_file(state)
                    state["force_stopped"] = True
                ACTIVE_ADVANCE = None
        _stop_active_runner_for_task(task_id, reason)
        final = _advance_state_snapshot(task_id)

    _stop_active_runner_for_task(task_id, reason)
    try:
        lock = _runner().lock_info()
    except Exception:
        lock = {}
    final = dict(final)
    final["lock_released"] = not (
        bool(lock.get("active"))
        and str(lock.get("task") or "").upper() == task_id
    )
    return final


def _record_auto_advance_failure(task_id: str, message: str, *, auto: bool = True) -> None:
    if _active_advance_task():
        return
    state: Dict[str, object] = {
        "ok": True,
        "task": task_id,
        "state": "failed",
        "step": "done",
        "step_label": ADVANCE_STEP_LABELS["done"],
        "auto": bool(auto),
        "started_at": _now_iso(),
        "finished_at": _now_iso(),
        "message": message,
        "error": message,
        "conclusion": "",
        "events": [{"at": _now_iso(), "step": "done", "message": message}],
    }
    _write_advance_state_file(state)


def _schedule_auto_advance(task_id: str) -> None:
    """登记成功后的自动入口：等登记锁释放，再启动全自动推进。"""
    task_id = task_id.upper()
    thread = threading.Thread(
        target=_auto_advance_wait_and_start,
        args=(task_id,),
        name=f"auto-advance-{task_id}",
        daemon=True,
    )
    thread.start()


def _auto_advance_wait_and_start(task_id: str) -> None:
    deadline = time.monotonic() + AUTO_ADVANCE_WAIT_SECONDS
    while time.monotonic() < deadline:
        task = _find_task(task_id)
        if task is None or task.archived:
            return
        if task.review_path is not None:
            return
        if (
            task_id in ACTIVE_REGISTRATIONS
            or _active_advance_task()
            or _active_dispatch_task()
            or _active_review_task()
        ):
            time.sleep(0.25)
            continue
        try:
            lock = _runner().lock_info()
        except Exception:
            lock = {}
        if lock.get("active"):
            time.sleep(0.25)
            continue
        try:
            _start_advance(task, auto=True)
        except ActionError as exc:
            _record_auto_advance_failure(
                task_id, f"发布后自动推进未启动：{exc.message}"
            )
        except Exception as exc:
            _record_auto_advance_failure(
                task_id, f"发布后自动推进未启动：{exc}"
            )
        return
    _record_auto_advance_failure(
        task_id, "发布后自动推进等待执行器空闲超时，未启动。"
    )


def _advance_status_payload(task: Task) -> Dict[str, object]:
    state = _advance_state_snapshot(task.task_id)
    state_name = str(state.get("state") or "idle")
    if state_name not in ADVANCE_STATE_NAMES:
        state_name = "idle"
    running = state_name == "running" and _active_advance_task() == task.task_id
    can_start, reason = _advance_availability(task)
    if running:
        can_start = False
        reason = "全自动推进正在进行，请等待当前步骤结束。"
    phase = _task_phase(task)
    terminal = state_name in ADVANCE_TERMINAL_STATES
    budget = _advance_budget_snapshot(state)
    events = state.get("events")
    if not isinstance(events, list):
        events = []
    step = str(state.get("step") or "")
    dispatch_state = _dispatch_state_snapshot(task.task_id)
    current_subtask = ""
    current_title = ""
    if running and step == "dispatch":
        current_subtask = str(dispatch_state.get("current_subtask") or "")
        current_title = str(dispatch_state.get("current_title") or "")
    return {
        "ok": True,
        "task": task.task_id,
        "state": state_name,
        "state_label": ADVANCE_STATE_NAMES.get(state_name, state_name),
        "state_tone": ADVANCE_STATE_TONES.get(state_name, "idle"),
        "running": running,
        "terminal": terminal,
        "can_stop": running,
        "stop_url": f"/advance/{task.task_id}/stop",
        "stop_requested": bool(state.get("stop_requested")),
        "stopped_with_budget": state_name == "stopped_with_budget",
        "budget": budget,
        "auto": bool(state.get("auto")),
        "revise": bool(state.get("revise")),
        "whole_rework": bool(state.get("whole_rework")),
        "whole_ran": bool(state.get("whole_ran")),
        "whole_done_path": str(state.get("whole_done_path") or ""),
        "owner_role": task.owner_role,
        "revise_round": _meta_int(state.get("revise_round")),
        "revise_strategy": str(state.get("revise_strategy") or ""),
        "revise_targets": state.get("revise_targets")
        if isinstance(state.get("revise_targets"), list)
        else [],
        "step": step,
        "step_label": str(
            state.get("step_label")
            or ADVANCE_STEP_LABELS.get(step, "")
        ),
        "current_subtask": current_subtask,
        "current_title": current_title,
        "phase": phase,
        "phase_label": _phase_label(phase),
        "phase_tone": _phase_tone(phase),
        "can_start": can_start,
        "disabled_reason": "" if can_start else reason,
        "message": str(state.get("message") or ""),
        "error": str(state.get("error") or ""),
        "conclusion": str(state.get("conclusion") or ""),
        "started_at": str(state.get("started_at") or ""),
        "finished_at": str(state.get("finished_at") or ""),
        "events": events,
        "handoff_blocked": state_name == "handoff_blocked",
        "handoff_stage": str(
            state.get("handoff_stage")
            or _handoff_state_snapshot(task.task_id).get("stage")
            or ""
        ),
        "handoff_missing": state.get("handoff_missing")
        if isinstance(state.get("handoff_missing"), list)
        else [],
        "handoff_checks": state.get("handoff_checks")
        if isinstance(state.get("handoff_checks"), list)
        else [],
        "handoff_log": str(
            state.get("handoff_log")
            or _handoff_state_snapshot(task.task_id).get("log")
            or ""
        ),
        "handoff_status_url": f"/api/task/{task.task_id}/handoff-status",
        "handoff_retry_url": f"/handoff/retry/{task.task_id}",
        "status_url": f"/api/task/{task.task_id}/advance-status",
        "detail_url": f"/task/{task.task_id}",
    }


def _handoff_status_payload(task: Task) -> Dict[str, object]:
    state = _handoff_state_snapshot(task.task_id)
    state_name = str(state.get("state") or "idle")
    if state_name not in HANDOFF_STATE_NAMES:
        state_name = "idle"
    log = str(state.get("log") or "")
    log_url = f"/log?path={quote(log, safe='')}" if log else ""
    checks = state.get("checks")
    if not isinstance(checks, list):
        checks = []
    missing = state.get("missing")
    if not isinstance(missing, list):
        missing = []
    attempts = state.get("attempts")
    if not isinstance(attempts, dict):
        attempts = {}
    agent_runs = state.get("agent_runs")
    if not isinstance(agent_runs, list):
        agent_runs = []
    return {
        "ok": True,
        "task": task.task_id,
        "state": state_name,
        "state_label": HANDOFF_STATE_NAMES.get(state_name, state_name),
        "state_tone": HANDOFF_STATE_TONES.get(state_name, "idle"),
        "stage": str(state.get("stage") or ""),
        "stage_label": str(state.get("stage_label") or ""),
        "round": _meta_int(state.get("round")),
        "passed": state_name == "passed",
        "blocked": state_name == "blocked",
        "can_retry": state_name == "blocked",
        "checks": checks,
        "missing": missing,
        "attempts": attempts,
        "attempt_total": _meta_int(state.get("attempt_total")),
        "max_supplements": HANDOFF_MAX_SUPPLEMENTS,
        "agent_runs": agent_runs,
        "message": str(state.get("message") or ""),
        "error": str(state.get("error") or ""),
        "log": log,
        "log_url": log_url,
        "started_at": str(state.get("started_at") or ""),
        "finished_at": str(state.get("finished_at") or ""),
        "phase": _task_phase(task),
        "phase_label": _phase_label(_task_phase(task)),
        "retry_url": f"/handoff/retry/{task.task_id}",
        "status_url": f"/api/task/{task.task_id}/handoff-status",
        "detail_url": f"/task/{task.task_id}",
    }


def _clear_handoff_advance_block(task_id: str) -> None:
    state = _advance_state_snapshot(task_id)
    if str(state.get("state") or "") != "handoff_blocked":
        return
    with ADVANCE_STATE_LOCK:
        state["state"] = "stopped"
        state["message"] = (
            "交接体检已通过；请重新发起「全自动推进」或手动继续。"
        )
        state["error"] = ""
        state["finished_at"] = _now_iso()
        events = state.get("events")
        if not isinstance(events, list):
            events = []
            state["events"] = events
        events.append(
            {
                "at": _now_iso(),
                "step": "handoff",
                "message": "重试体检通过，交接阻塞已解除。",
            }
        )
        if len(events) > 40:
            del events[:-40]
        _write_advance_state_file(state)


def _complete_review_after_handoff(task_id: str) -> None:
    """G3/G4 重试通过后，把因缺件停在 review_failed 的审核收口为完成。"""
    state = _read_review_state_file(task_id)
    if str(state.get("state") or "") != "review_failed":
        return
    if not bool(state.get("handoff_blocked")):
        return
    task = _find_task(task_id)
    if task is None:
        return
    terminal, verdict = _review_terminal_state(task)
    if not terminal:
        return
    with REVIEW_STATE_LOCK:
        state["state"] = "completed"
        state["conclusion"] = verdict
        state["error"] = ""
        state["message"] = "重试交接体检通过，审核完成。"
        state["finished_at"] = _now_iso()
        state["handoff_blocked"] = False
        _write_review_state_file(state)


def _retry_handoff(task: Task) -> Dict[str, object]:
    state = _handoff_state_snapshot(task.task_id)
    stage = str(state.get("stage") or "")
    if not stage or str(state.get("state") or "") not in {
        "blocked",
        "passed",
    }:
        raise ActionError(
            409,
            "没有可重试的交接体检",
            f"{task.task_id} 当前没有交接阻塞记录。",
            "先执行会触发交接体检的操作。",
        )
    if _active_handoff_task():
        raise ActionError(
            409,
            "交接体检正在进行",
            "已有交接体检或自动补交正在运行，请等待结束。",
            "稍后重试。",
        )
    if _active_dispatch_task() or _active_review_task() or _active_advance_task():
        raise ActionError(
            409,
            "执行器正在运行",
            "分发、审核或自动推进尚未结束，本次没有启动补交。",
            "等待运行结束后再重试体检。",
        )
    try:
        lock = _runner().lock_info()
    except Exception:
        lock = {}
    if lock.get("active"):
        raise ActionError(
            409,
            "执行器正忙",
            f"执行器正在运行 {lock.get('task') or '未知任务'}；"
            "本次没有启动补交。",
            "等待当前运行结束后再重试体检。",
        )
    round_no = _meta_int(state.get("round"))
    result = _run_handoff_gates(
        task,
        stage,
        round_no=round_no,
        source="retry",
        review_baseline=state.get("review_baseline"),
        review_done_baseline=state.get("review_done_baseline"),
    )
    if bool(result.get("passed")):
        if stage == "review_complete":
            _complete_review_after_handoff(task.task_id)
        _clear_handoff_advance_block(task.task_id)
    return result


# ---------------------------------------------------------------------------
# 继续修正（重修）：须修正 → 归档旧审查 → 重置受影响子任务 → 自动推进
# ---------------------------------------------------------------------------


def _revise_active_for(task_id: str) -> bool:
    wanted = task_id.strip().upper()
    if _active_advance_task().upper() != wanted:
        return False
    state = _advance_state_snapshot(wanted)
    return bool(state.get("revise")) and str(state.get("state") or "") == "running"


def _revise_plan(task: Task) -> Dict[str, object]:
    target_ids = _review_target_ids(task)
    target_set = set(target_ids)
    targets = tuple(item for item in task.subtasks if item.id in target_set)
    return {
        "targets": targets,
        "target_ids": [item.id for item in targets],
        "passed_target_ids": [
            item.id for item in targets if item.status == "passed"
        ],
        "all_ids": [item.id for item in task.subtasks],
        "issues": _review_issue_items(task.review_text),
        "has_issue_section": bool(_review_issue_section(task.review_text)),
    }


def _revise_next_round(task: Task) -> int:
    return task.revise_round + 1


def _owner_role_binding(task: Task) -> Tuple[Optional[Role], Optional[AgentApp], str]:
    """整单重修的责任角色：task.yaml 的 owner_role（缺省 designer）。"""
    role_id = str(task.owner_role or DEFAULT_OWNER_ROLE).strip() or DEFAULT_OWNER_ROLE
    role = _find_role(role_id)
    if role is None:
        return None, None, f"owner_role={role_id} 不存在（请改 task.yaml 或角色页）"
    if not role.enabled:
        return role, None, f"owner_role 角色 {role.name}（{role.id}）已停用"
    if not role.agent_id:
        return role, None, f"owner_role 角色 {role.name}（{role.id}）未绑定 Agent"
    agents, _agent_issues = _load_agents()
    agent = next((item for item in agents if item.id == role.agent_id), None)
    if agent is None:
        return role, None, (
            f"owner_role 角色 {role.name} 绑定的 Agent {role.agent_id} 未登记"
        )
    if not agent.installed:
        return role, agent, (
            f"owner_role 绑定的 Agent {agent.id} 未安装（installed=false）"
        )
    if not agent.currently_executable:
        return role, agent, (
            f"owner_role 绑定的 Agent {agent.id} 命令当前不可执行"
        )
    return role, agent, ""


def _whole_rework_done_path(task: Task, round_no: int) -> Path:
    """整单重修的新 DONE.md：对接/交付/r<轮次>/DONE.md；已存在则加时间戳。"""
    root = task.docking_dir / DELIVERY_DIRNAME
    name = f"r{max(1, int(round_no))}"
    candidate = root / name
    if candidate.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate = root / f"{name}-{stamp}"
        suffix = 1
        while candidate.exists():
            candidate = root / f"{name}-{stamp}-{suffix}"
            suffix += 1
    return candidate / DONE_NAME


def _whole_rework_prompt(
    task: Task,
    role: Role,
    round_no: int,
    review_snapshot: str,
    done_path: Path,
    context: Dict[str, object],
) -> str:
    """整单重修提示词：任务单、问题全文、禁止事项、轮次、回归验收。"""
    try:
        task_sheet = _read_text(task.path)
    except OSError:
        task_sheet = "（任务单不可读）"
    issues = str(context.get("issues_full") or "").strip()
    if not issues:
        issues = task.review_text.strip() or "（审查未提供问题清单）"
    prohibitions = str(
        context.get("prohibitions") or _task_sheet_prohibitions(task)
    ).strip()
    snapshot = review_snapshot or "（无历史快照）"
    return (
        f"你是 {role.name}（{role.id}）。任务 {task.task_id}「{task.title}」"
        f"进入{REVISE_MODE_LABELS[REVISE_MODE_WHOLE]}（第 {round_no} 轮）。\n"
        f"值班台项目目录：{task.project_dir}\n"
        f"工作目录（cwd）：{task.workdir}\n"
        f"任务单（绝对路径）：{task.path}\n"
        f"上一轮审查快照（绝对路径）：{snapshot}\n"
        "\n"
        "=== 任务单全文（必须逐条满足） ===\n"
        f"{task_sheet}\n"
        "=== 本轮审查问题全文（原文，必须逐条修正） ===\n"
        f"{issues}\n"
        "=== 父任务单「禁止事项」全文 ===\n"
        f"{prohibitions}\n"
        "\n"
        "=== 回归验收要求 ===\n"
        f"- 轮次：第 {round_no}/{MAX_REVISE_ROUNDS} 轮；只写本轮允许的范围。\n"
        "- 重做后必须重新满足任务单全部验收标准，并逐条对应上面的问题。\n"
        f"- 完成后必须写新的 DONE.md（绝对路径）：{done_path}\n"
        "  DONE.md 必须含「做了什么 / 怎么验 / 结果」三部分；"
        "「怎么验」写清本轮复跑命令与结果。\n"
        "- 不得回退上一轮已通过的内容，不得缩小原有验收范围。\n"
        "- 只写工作目录内产物与上述 DONE.md；不得写 对接/审查.md；"
        "不得删除文件、不改其它项目、不重启 8787。\n"
        "完成后退出；系统会自动重新审核。"
    )


def _revise_availability(task: Task) -> Tuple[bool, str]:
    if task.archived:
        return False, "归档任务只读，不能继续修正。"
    if _handoff_blocked_for(task.task_id):
        return False, "交接体检未通过；请先点「重试体检」或人工补齐缺件。"
    if _active_handoff_task():
        return False, "交接体检或自动补交正在进行，请等待结束。"
    if task.review_path is None:
        return False, "还没有审核结论，没有需要继续修正的内容。"
    terminal, verdict = _review_terminal_state(task)
    if not terminal:
        return False, "审查缺少可识别结论或 审核完成.md，没有可继续修正的终态。"
    if verdict != "revise":
        return False, "当前审查结论不是「须修正」，无需继续修正。"
    if task.revise_round >= MAX_REVISE_ROUNDS:
        return False, f"已达 {MAX_REVISE_ROUNDS} 轮修正上限，请人工介入。"
    active_advance = _active_advance_task()
    if active_advance:
        if active_advance.upper() == task.task_id:
            return False, "继续修正正在进行，请等待当前步骤结束。"
        return False, f"{active_advance} 的全自动推进正在进行，请等待推进结束。"
    active_dispatch = _active_dispatch_task()
    if active_dispatch:
        return False, f"{active_dispatch} 正在分发实现，请等待分发结束。"
    if _review_active_for(task.task_id):
        return False, "审核正在运行，请等待审核结束后再继续修正。"
    if ACTIVE_REGISTRATIONS:
        return False, "总控登记正在运行，请等待登记结束后再继续修正。"
    if task.subtasks_path.is_file() and task.subtasks_error:
        return False, f"subtasks.yaml 无法解析：{task.subtasks_error}"
    targets = _review_target_ids(task)
    candidates = [
        item for item in task.subtasks if item.status in {"pending", "failed"}
    ]
    candidates.extend(
        item
        for item in task.subtasks
        if item.id in targets and item.status == "delivered"
    )
    if candidates:
        roles, _role_issues = _load_roles()
        agents, _agent_issues = _load_agents()
        _bindings, violations = _dispatch_preflight(
            tuple(candidates), roles, agents
        )
        if violations:
            return False, "分发预检未通过：" + "；".join(violations)
    _review_role, _review_agent, binding_issue = _review_role_binding()
    if binding_issue:
        return False, "自动审核无法开始：" + binding_issue
    try:
        lock = _runner().lock_info()
    except Exception:
        lock = {}
    if lock.get("active"):
        return False, (
            f"执行器正忙（{lock.get('task') or '未知任务'}），"
            "请等待当前运行结束。"
        )
    if not task.subtasks:
        owner_role, _owner_agent, owner_issue = _owner_role_binding(task)
        if owner_issue:
            return False, "整单重修无法开始：" + owner_issue
        return True, (
            f"本任务没有子任务；将回叫 owner_role={owner_role.id if owner_role else task.owner_role}"
            " 绑定的 Agent 整单重修，完成后自动重新审核。"
        )
    if targets:
        return True, (
            "将重置问题清单涉及的子任务，然后自动分发并重新审核；"
            "未受影响的子任务不会重跑。"
        )
    return True, "问题清单里没有具体子任务编号；请选择全部重做或只重新审核。"


def _revise_status_payload(task: Task) -> Dict[str, object]:
    plan = _revise_plan(task)
    terminal, terminal_verdict = _review_terminal_state(task)
    advance_state = _advance_state_snapshot(task.task_id)
    revise_state = advance_state if advance_state.get("revise") else {}
    running = _revise_active_for(task.task_id)
    state_name = str(revise_state.get("state") or "idle")
    if state_name not in ADVANCE_STATE_NAMES:
        state_name = "idle"
    can_start, reason = _revise_availability(task)
    if running:
        can_start = False
        reason = "继续修正正在进行，请等待当前步骤结束。"
    at_limit = task.revise_round >= MAX_REVISE_ROUNDS
    step = str(revise_state.get("step") or "")
    dispatch_state = _dispatch_state_snapshot(task.task_id)
    current_subtask = ""
    current_title = ""
    if running and step == "dispatch":
        current_subtask = str(dispatch_state.get("current_subtask") or "")
        current_title = str(dispatch_state.get("current_title") or "")
        if not task.subtasks:
            current_subtask = f"整单 · {task.owner_role or DEFAULT_OWNER_ROLE}"
            current_title = "owner_role Agent 重做整单"
    log = str(revise_state.get("revise_log") or "")
    log_url = f"/log?path={quote(log, safe='')}" if log else ""
    target_ids = plan["target_ids"]
    events = revise_state.get("events")
    if not isinstance(events, list):
        events = []
    return {
        "ok": True,
        "task": task.task_id,
        "phase": _task_phase(task),
        "phase_label": _phase_label(_task_phase(task)),
        "phase_tone": _phase_tone(_task_phase(task)),
        "state": state_name,
        "state_label": ADVANCE_STATE_NAMES.get(state_name, state_name),
        "state_tone": ADVANCE_STATE_TONES.get(state_name, "idle"),
        "running": running,
        "step": step,
        "step_label": ADVANCE_STEP_LABELS.get(step, step),
        "message": str(revise_state.get("message") or ""),
        "error": str(revise_state.get("error") or ""),
        "conclusion": str(revise_state.get("conclusion") or ""),
        "current_subtask": current_subtask,
        "current_title": current_title,
        "revise_round": task.revise_round,
        "next_round": _revise_next_round(task),
        "max_rounds": MAX_REVISE_ROUNDS,
        "at_limit": at_limit,
        "can_start": can_start,
        "disabled_reason": "" if can_start else reason,
        "ready_reason": reason if can_start else "",
        "needs_choice": bool(task.subtasks) and not target_ids,
        "has_subtasks": bool(task.subtasks),
        "whole_rework": not bool(task.subtasks),
        "owner_role": task.owner_role,
        "target_ids": target_ids,
        "passed_target_ids": plan["passed_target_ids"],
        "all_ids": plan["all_ids"],
        "issues": plan["issues"],
        "has_issue_section": plan["has_issue_section"],
        "review_conclusion": terminal_verdict if terminal else "",
        "log": log,
        "log_url": log_url,
        "events": events,
        "status_url": f"/api/task/{task.task_id}/revise-status",
        "detail_url": f"/task/{task.task_id}",
    }


def _write_revise_round(task: Task, round_no: int) -> None:
    with TASK_WRITE_LOCK:
        meta = _read_task_meta(
            task.project_dir,
            task.task_id,
            title=task.title,
            title_mtime=task.mtime,
        )
        meta["revise_round"] = str(max(0, int(round_no)))
        _write_task_meta(task.meta_path, meta)


def _new_revise_log_path(task: Task) -> Path:
    log_dir = task.workspace_dir / "日志"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = log_dir / f"revise-{stamp}.log"
    suffix = 1
    while candidate.exists():
        candidate = log_dir / f"revise-{stamp}-{suffix}.log"
        suffix += 1
    return candidate


def _append_revise_log(path: Path, lines: List[str]) -> None:
    """最佳努力写修订日志；日志失败不阻断修订本身。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        traceback.print_exc()


def _unique_history_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem = candidate.stem
    suffix = candidate.suffix
    index = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{index}{suffix}"
        index += 1
    return candidate


def _archive_revise_review(
    task: Task, round_no: int, *, move: bool = False
) -> Dict[str, str]:
    """把本轮审查（和完成信号）归档到 对接/审查历史/round-<n>.md。

    子任务重修默认复制（原件保留供核对）；无子任务整单重修必须 move，
    否则旧终态会继续生效，无法进入新一轮审核。
    """
    source = task.docking_dir / REVIEW_NAME
    if not source.is_file():
        raise ConfigError(f"没有找到 对接/{REVIEW_NAME}，无法归档旧审查。")
    history_dir = task.docking_dir / REVIEW_HISTORY_DIRNAME
    history_dir.mkdir(parents=True, exist_ok=True)
    review_target = _unique_history_path(history_dir, f"round-{round_no}.md")
    transfer = shutil.move if move else shutil.copy2
    transfer(str(source), str(review_target))
    done_target = ""
    done_source = task.docking_dir / REVIEW_DONE_NAME
    if done_source.is_file():
        done_path = _unique_history_path(
            history_dir, f"round-{round_no}-{REVIEW_DONE_NAME}"
        )
        transfer(str(done_source), str(done_path))
        done_target = _display_path(done_path)
    return {
        "review": _display_path(review_target),
        "review_abs": str(review_target.resolve()),
        "review_done": done_target,
        "directory": _display_path(history_dir),
        "moved": bool(move),
    }


def _archive_subtask_outputs(
    task: Task, subtask_ids: List[str], round_no: int
) -> List[Dict[str, str]]:
    """修订前把受影响子任务的 output_dir 整体移入工作区历史，保留不删。"""
    if not subtask_ids:
        return []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    results: List[Dict[str, str]] = []
    by_id = {item.id: item for item in task.subtasks}
    for subtask_id in subtask_ids:
        item = by_id.get(subtask_id)
        if item is None:
            continue
        try:
            source = _subtask_output_dir(task, item)
        except ConfigError as exc:
            raise ConfigError(f"{subtask_id} 输出目录不可用：{exc}") from exc
        if not source.exists():
            results.append(
                {
                    "id": subtask_id,
                    "from": str(source),
                    "to": "",
                    "moved": False,
                }
            )
            continue
        base = (
            task.workspace_dir
            / HISTORY_DIRNAME
            / SUBTASK_HISTORY_DIRNAME
            / subtask_id
        )
        base.mkdir(parents=True, exist_ok=True)
        target = _unique_history_path(
            base, f"round-{round_no}-{stamp}"
        )
        try:
            shutil.move(str(source), str(target))
        except OSError as exc:
            raise ConfigError(
                f"无法把 {subtask_id} 的旧产物移入 {target}：{exc}"
            ) from exc
        results.append(
            {
                "id": subtask_id,
                "from": str(source),
                "to": str(target),
                "moved": True,
            }
        )
    return results


def _build_revision_context(
    task: Task, round_no: int, snapshot_path: str
) -> Dict[str, object]:
    """修订分发所需的不可丢失上下文；随 advance/dispatch 状态传递。"""
    issues_full = _review_issue_section(task.review_text).strip()
    if not issues_full:
        # 没有标准「问题清单」标题时保留整份审查，避免上下文丢失。
        issues_full = task.review_text.strip()
    return {
        "round": int(round_no),
        "parent_task_sheet": str(Path(task.path).resolve()),
        "prohibitions": _task_sheet_prohibitions(task),
        "issues_full": issues_full,
        "review_snapshot": (
            str(Path(snapshot_path).resolve()) if snapshot_path else ""
        ),
        "review_verdict": "revise",
    }


def _reset_subtasks_for_revise(
    task: Task, ids: List[str], *, include_passed: bool = False
) -> List[str]:
    """把指定子任务重置为 pending；默认不重跑 passed，保留日志文件。"""
    wanted = set(ids)
    reset_statuses = {"delivered", "passed"} if include_passed else {"delivered"}
    changed: List[str] = []
    for item in task.subtasks:
        if item.id not in wanted:
            continue
        if item.status not in reset_statuses:
            continue
        _update_subtask_record(
            task.subtasks_path,
            item.id,
            {
                "status": "pending",
                "started_at": "",
                "finished_at": "",
                "error": "",
                "log": "",
                "run_id": "",
            },
        )
        changed.append(item.id)
    return changed


def _start_revise(task: Task, mode: str = "") -> Dict[str, object]:
    """预检后归档旧审查、重置受影响子任务，并启动既有自动推进流程。

    无子任务（subtasks_total=0）时改走「整单重修」：移动旧审查到
    对接/审查历史/round-<n>.md，回叫 owner_role 重做整单，再自动审核。
    """
    with REVISE_START_LOCK:
        with _task_lifecycle_lock(task.task_id):
            current = _find_task(task.task_id)
            if current is None:
                raise ActionError(
                    409,
                    "任务正在移动或已归档",
                    f"{task.task_id} 已不在 项目/ 中，本次没有启动任何进程。",
                    "刷新看板；归档任务请先恢复。",
                )
            if current.archived:
                raise ActionError(
                    409,
                    "归档任务只读",
                    f"{task.task_id} 位于 归档/，不能继续修正。",
                    "先从归档库恢复项目。",
                )
            task = current
        _ensure_handoff_not_blocked(task)
        terminal, verdict = _review_terminal_state(task)
        if not terminal or verdict != "revise":
            raise ActionError(
                409,
                "当前不是须修正阶段",
                f"{task.task_id} 没有可继续修正的审查终态"
                "（须同时具备 审查.md 明确结论与 审核完成.md）。",
                "只有审查结论为「须修正」且完成信号齐备的任务可以继续修正。",
            )
        if task.revise_round >= MAX_REVISE_ROUNDS:
            raise ActionError(
                409,
                "已达修正轮次上限",
                f"{task.task_id} 已连续修正 {task.revise_round} 轮，"
                f"达到上限 {MAX_REVISE_ROUNDS} 轮；请人工介入。",
                "人工检查产物与审查意见后再决定下一步。",
            )

        plan = _revise_plan(task)
        target_ids = list(plan["target_ids"])
        mode = (mode or "").strip()
        if mode and mode not in REVISE_MODES:
            raise ActionError(
                400,
                "修正方式不正确",
                f"未知的修正方式：{mode}。",
                "使用 affected / all / review / whole 之一。",
            )
        whole = not task.subtasks and mode != REVISE_MODE_REVIEW
        if whole:
            mode = REVISE_MODE_WHOLE
        elif mode == REVISE_MODE_WHOLE and task.subtasks:
            raise ActionError(
                400,
                "修正方式不正确",
                "整单重修只适用于没有子任务的任务；本任务有 subtasks.yaml。",
                "改用 affected / all / review。",
            )
        if not whole and not mode:
            if target_ids:
                mode = REVISE_MODE_AFFECTED
            else:
                raise ActionError(
                    409,
                    "未识别到具体子任务",
                    "问题清单里没有出现可识别的子任务编号（Txx-n）。"
                    "请选择「全部子任务重做」或「只重新审核（不改产物）」。",
                    "在详情页二选一后再次提交。",
                    extra={
                        "ok": False,
                        "needs_choice": True,
                        "target_ids": [],
                        "all_ids": plan["all_ids"],
                        "options": [
                            {"mode": REVISE_MODE_ALL, "label": "全部子任务重做"},
                            {
                                "mode": REVISE_MODE_REVIEW,
                                "label": "只重新审核（不改产物）",
                            },
                        ],
                    },
                )
        if mode == REVISE_MODE_AFFECTED and not target_ids:
            raise ActionError(
                409,
                "未识别到具体子任务",
                "问题清单里没有出现可识别的子任务编号（Txx-n），"
                "无法只重做受影响子任务。",
                "选择「全部子任务重做」或「只重新审核（不改产物）」。",
                extra={
                    "ok": False,
                    "needs_choice": True,
                    "all_ids": plan["all_ids"],
                },
            )
        if mode == REVISE_MODE_REVIEW:
            waiting = [
                item
                for item in task.subtasks
                if item.status not in {"delivered", "passed"}
            ]
            if waiting:
                raise ActionError(
                    409,
                    "不能只重新审核",
                    "以下子任务尚未交付："
                    + "、".join(f"{item.id}（{item.status}）" for item in waiting)
                    + "；只重新审核不会补做产物。",
                    "选择重做子任务的修正方式。",
                )

        available, reason = _revise_availability(task)
        if not available:
            raise ActionError(
                409,
                "无法继续修正",
                reason,
                "处理阻塞原因后再试。",
            )

        handoff = _run_handoff_gates(task, "revise_before", source="revise")
        if not bool(handoff.get("passed")):
            raise _handoff_action_error(handoff)

        with _task_lifecycle_lock(task.task_id):
            latest = _find_task(task.task_id)
            if latest is None:
                raise ActionError(
                    409,
                    "任务正在移动或已归档",
                    f"{task.task_id} 已不在 项目/ 中，本次没有启动任何进程。",
                    "刷新看板；归档任务请先恢复。",
                )
            if latest.archived:
                raise ActionError(
                    409,
                    "归档任务只读",
                    f"{task.task_id} 位于 归档/，不能继续修正。",
                    "先从归档库恢复项目。",
                )
            task = latest
            if _active_dispatch_task():
                raise ActionError(
                    409,
                    "分发正在运行",
                    f"{_active_dispatch_task()} 正在分发实现，未修改任何文件。",
                    "等待分发结束后再继续修正。",
                )
            active_review = _active_review_task()
            if active_review:
                raise ActionError(
                    409,
                    "审核正在运行",
                    f"{active_review} 正在审核，未修改任何文件。",
                    "等待审核结束后再继续修正。",
                )
            active_handoff = _active_handoff_task()
            if active_handoff:
                raise ActionError(
                    409,
                    "交接体检正在运行",
                    f"{active_handoff} 正在检查/补交交接文件，未修改任何文件。",
                    "等待交接体检结束后再继续修正。",
                )
            if _active_advance_task():
                raise ActionError(
                    409,
                    "全自动推进正在运行",
                    f"{_active_advance_task()} 正在全自动推进，未修改任何文件。",
                    "等待推进结束后再继续修正。",
                )
            if ACTIVE_REGISTRATIONS:
                raise ActionError(
                    409,
                    "总控登记正在运行",
                    "已有总控登记占用执行器，未修改任何文件。",
                    "等待登记结束后再继续修正。",
                )
            try:
                lock = _runner().lock_info()
            except Exception:
                lock = {}
            if lock.get("active"):
                raise ActionError(
                    409,
                    "执行器正忙",
                    f"执行器正在运行 {lock.get('task') or '未知任务'}，"
                    "未修改任何文件。",
                    "等待当前运行结束后再继续修正。",
                )
            terminal, verdict = _review_terminal_state(task)
            if not terminal or verdict != "revise":
                raise ActionError(
                    409,
                    "当前不是须修正阶段",
                    f"{task.task_id} 的审查终态在启动前发生变化，未修改任何文件。",
                    "刷新后确认审查结论再试。",
                )
            if task.revise_round >= MAX_REVISE_ROUNDS:
                raise ActionError(
                    409,
                    "已达修正轮次上限",
                    f"{task.task_id} 已连续修正 {task.revise_round} 轮，"
                    f"达到上限 {MAX_REVISE_ROUNDS} 轮；请人工介入。",
                    "人工检查产物与审查意见后再决定下一步。",
                )
            plan = _revise_plan(task)
            target_ids = list(plan["target_ids"])
            whole = not task.subtasks and mode != REVISE_MODE_REVIEW
            if whole:
                mode = REVISE_MODE_WHOLE

            ids = (
                list(plan["all_ids"])
                if mode == REVISE_MODE_ALL
                else target_ids if mode == REVISE_MODE_AFFECTED else []
            )
            reset_ids = set(ids)
            reset_statuses = (
                {"delivered", "passed"}
                if mode == REVISE_MODE_ALL
                else {"delivered"}
            )
            post_targets = [
                item
                for item in task.subtasks
                if item.status in {"pending", "failed"}
                or (item.id in reset_ids and item.status in reset_statuses)
            ]
            if post_targets:
                roles, _role_issues = _load_roles()
                agents, _agent_issues = _load_agents()
                _bindings, violations = _dispatch_preflight(
                    tuple(post_targets), roles, agents
                )
                if violations:
                    raise ActionError(
                        409,
                        "分发预检未通过",
                        "以下子任务的角色绑定不满足要求，未修改任何文件："
                        + "；".join(violations),
                        "到 /roles 绑定可执行 Agent，或修正 subtasks.yaml 的 agent_role。",
                        violations=violations,
                    )
            if whole:
                _owner_role, _owner_agent, owner_issue = _owner_role_binding(task)
                if owner_issue:
                    raise ActionError(
                        409,
                        "整单重修无法开始",
                        owner_issue + "；未修改任何文件。",
                        "在任务详情编辑 owner_role，或到 /roles 绑定可执行 Agent。",
                    )

            round_no = _revise_next_round(task)
            log_path = _new_revise_log_path(task)
            log_rel = _display_path(log_path)
            _append_revise_log(
                log_path,
                [
                    f"# 继续修正 · {task.task_id} · 第 {round_no} 轮",
                    f"- 时间：{_now_iso()}",
                    f"- 任务：{task.task_id}「{task.title}」",
                    f"- 修正方式：{REVISE_MODE_LABELS.get(mode, mode)}（{mode}）",
                    f"- 审查结论：须修正",
                    f"- 问题清单涉及：{'、'.join(target_ids) or '（未识别到子任务编号）'}",
                    (
                        f"- 本次计划范围：整单重修（owner_role={task.owner_role}）"
                        if whole
                        else f"- 本次计划范围：{'、'.join(ids) or '（不改产物）'}"
                    ),
                    f"- 修正轮次：{round_no}/{MAX_REVISE_ROUNDS}",
                ],
            )

            try:
                archived = _archive_revise_review(task, round_no, move=whole)
            except (OSError, ConfigError) as exc:
                _append_revise_log(log_path, [f"- 失败：旧审查归档失败：{exc}"])
                raise ActionError(
                    500,
                    "旧审查归档失败",
                    f"无法把 对接/{REVIEW_NAME} 归档到 "
                    f"{REVIEW_HISTORY_DIRNAME}/：{exc}；未修改子任务。",
                    "检查对接目录写权限后重试。",
                ) from exc
            revise_context = _build_revision_context(
                task, round_no, str(archived.get("review_abs") or "")
            )
            _append_revise_log(
                log_path,
                [
                    f"- 旧审查快照：{archived['review']}"
                    f"（{'已移入' if whole else '已复制'}）",
                    f"- 完成信号快照：{archived.get('review_done') or '（无）'}",
                    f"- 修订上下文：父任务单={revise_context['parent_task_sheet']}",
                    f"- 修订上下文：问题全文 {len(str(revise_context['issues_full']))} 字符；"
                    f"禁止事项 {len(str(revise_context['prohibitions']))} 字符",
                ],
            )

            archived_outputs: List[Dict[str, str]] = []
            changed: List[str] = []
            done_path = Path()
            if whole:
                done_path = _whole_rework_done_path(task, round_no)
                revise_context["whole_rework"] = True
                revise_context["owner_role"] = task.owner_role
                revise_context["done_path"] = str(done_path.resolve())
                revise_context["round"] = round_no
                _append_revise_log(
                    log_path,
                    [
                        f"- 整单重修：回叫 owner_role={task.owner_role} 绑定的 Agent",
                        f"- 本轮新 DONE.md：{_display_path(done_path)}",
                    ],
                )
            else:
                moving_ids = [
                    item.id
                    for item in task.subtasks
                    if item.id in set(ids)
                    and (mode == REVISE_MODE_ALL or item.status != "passed")
                ]
                try:
                    archived_outputs = _archive_subtask_outputs(
                        task, moving_ids, round_no
                    )
                except ConfigError as exc:
                    _append_revise_log(
                        log_path, [f"- 失败：旧子任务产物无法移入历史：{exc}"]
                    )
                    raise ActionError(
                        500,
                        "旧产物无法移入历史",
                        f"{exc}；旧审查已归档到 {archived['review']}，"
                        "子任务尚未重置。",
                        "检查工作区写权限后重试。",
                    ) from exc
                moved_count = sum(
                    1 for item in archived_outputs if item.get("moved")
                )
                _append_revise_log(
                    log_path,
                    [
                        f"- 旧产物移入工作区历史：{moved_count} 项"
                        + (
                            "（"
                            + "、".join(
                                item["id"]
                                for item in archived_outputs
                                if item.get("moved")
                            )
                            + "）"
                            if moved_count
                            else ""
                        )
                    ],
                )
                try:
                    changed = _reset_subtasks_for_revise(
                        task, ids, include_passed=(mode == REVISE_MODE_ALL)
                    )
                except ConfigError as exc:
                    _append_revise_log(
                        log_path, [f"- 失败：重置 subtasks.yaml 失败：{exc}"]
                    )
                    raise ActionError(
                        500,
                        "子任务无法重置",
                        f"{exc}；旧审查已归档到 {archived['review']}。",
                        "检查 对接/subtasks.yaml 后重试。",
                    ) from exc
                changed_ids = set(changed)
                _append_revise_log(
                    log_path,
                    [
                        f"- 重置为 pending：{'、'.join(changed) or '（无）'}",
                        f"- 未改动的子任务："
                        + (
                            "、".join(
                                item.id
                                for item in task.subtasks
                                if item.id not in changed_ids
                            )
                            or "（无）"
                        ),
                    ],
                )

            try:
                _write_revise_round(task, round_no)
            except ConfigError as exc:
                _append_revise_log(
                    log_path, [f"- 失败：写入 revise_round 失败：{exc}"]
                )
                raise ActionError(
                    500,
                    "修正轮次无法写入",
                    f"{exc}；旧审查已归档到 {archived['review']}。",
                    "检查 对接/task.yaml 后重试。",
                ) from exc

            refreshed = _find_task(task.task_id)
            if refreshed is None:
                _append_revise_log(log_path, ["- 失败：重置后找不到任务。"])
                raise ActionError(
                    500,
                    "找不到任务",
                    f"{task.task_id} 在重置后不在活动项目目录中。",
                    "检查项目目录是否被移动或归档。",
                )
            try:
                advance_state = _start_advance(
                    refreshed,
                    revise=True,
                    whole_rework=whole,
                    _prechecked=True,
                    revise_round=round_no,
                    strategy=mode,
                    affected_ids=ids,
                    revise_log=log_rel,
                    revise_context=revise_context,
                )
            except ActionError as exc:
                _append_revise_log(
                    log_path, [f"- 失败：自动推进未能启动：{exc.message}"]
                )
                raise ActionError(
                    exc.status,
                    "自动推进未能启动",
                    f"旧审查已归档为 {archived['review']}，子任务重置："
                    f"{'、'.join(changed) or '（无）'}；但自动推进未能启动：{exc.message}",
                    "处理阻塞原因后重试；轮次已记为 "
                    f"{round_no}/{MAX_REVISE_ROUNDS}。",
                ) from exc
            except Exception as exc:
                _append_revise_log(
                    log_path, [f"- 失败：自动推进未能启动：{exc}"]
                )
                raise ActionError(
                    500,
                    "自动推进未能启动",
                    f"旧审查已归档为 {archived['review']}，子任务重置："
                    f"{'、'.join(changed) or '（无）'}；但自动推进未能启动：{exc}",
                    "查看控制台错误后重试；轮次已记为 "
                    f"{round_no}/{MAX_REVISE_ROUNDS}。",
                ) from exc
            _append_revise_log(
                log_path,
                [
                    f"- 自动推进：已启动（{ADVANCE_STATE_NAMES.get('running', '推进中')}）",
                    f"- 推进状态：{_display_path(_advance_state_path(task.task_id))}",
                    "- 分发与审核复用执行器单实例锁，串行执行。",
                ],
            )
            return {
                "ok": True,
                "task": task.task_id,
                "revise_round": round_no,
                "max_rounds": MAX_REVISE_ROUNDS,
                "mode": mode,
                "mode_label": REVISE_MODE_LABELS.get(mode, mode),
                "whole_rework": whole,
                "owner_role": task.owner_role if whole else "",
                "rework_done": _display_path(done_path) if whole else "",
                "target_ids": target_ids if mode == REVISE_MODE_AFFECTED else (
                    list(plan["all_ids"]) if mode == REVISE_MODE_ALL else []
                ),
                "reset_subtasks": changed,
                "archived_outputs": archived_outputs,
                "archived_review": archived["review"],
                "archived_review_done": archived.get("review_done") or "",
                "history_directory": archived["directory"],
                "log": log_rel,
                "log_url": f"/log?path={quote(log_rel, safe='')}",
                "advance": advance_state,
                "status_url": f"/api/task/{task.task_id}/revise-status",
            }


def _run_log_tail(record: Dict[str, object]) -> List[str]:
    return _runner().tail_log(record.get("log"), RUN_LOG_TAIL_LINES)


def _runner_api_payload() -> dict:
    snapshot = _runner().snapshot()
    history = _runner().history(RUN_HISTORY_LIMIT)
    return {
        "generated_at": _now_iso(),
        "status_path": _display_path(_runner().status_path),
        "lock_path": _display_path(_runner().lock_path),
        "history_path": _display_path(_runner().history_path),
        "timeout_seconds": RUNNER_TIMEOUT_SECONDS,
        "current": _public_run_payload(snapshot),
        "recent": [_public_run_payload(record) for record in history],
    }


def _public_run_payload(record: Dict[str, object]) -> dict:
    task_id = str(record.get("task") or "")
    return {
        "run_id": str(record.get("run_id") or ""),
        "state": str(record.get("state") or "idle"),
        "running": bool(record.get("running", False)),
        "task": task_id,
        "task_url": f"/task/{task_id}" if TASK_ID_RE.fullmatch(task_id) else None,
        "agent": str(record.get("agent") or ""),
        "agent_name": str(record.get("agent_name") or ""),
        "kind": str(record.get("kind") or "run"),
        "subtask": str(record.get("subtask") or ""),
        "subtask_title": str(record.get("subtask_title") or ""),
        "pid": record.get("pid", ""),
        "owner_pid": record.get("owner_pid", ""),
        "round": record.get("round", ""),
        "started": str(record.get("started") or ""),
        "started_epoch": record.get("started_epoch", ""),
        "finished": str(record.get("finished") or ""),
        "duration_seconds": record.get("duration_seconds", ""),
        "duration_display": _format_duration(record.get("duration_seconds")),
        "exit": _format_exit(record.get("exit")),
        "timeout_seconds": record.get("timeout_seconds", RUNNER_TIMEOUT_SECONDS),
        "log": str(record.get("log") or ""),
        "error": str(record.get("error") or ""),
    }


# ---------------------------------------------------------------------------
# 角色与 Agent 配置（YAML，只用标准库）
# ---------------------------------------------------------------------------


def _local_base_url() -> str:
    return f"http://{HOST}:{PORT}"


def _allowed_hosts() -> set:
    return {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


def _allowed_origins() -> set:
    return {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}


def _read_config_text(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ConfigError(f"无法读取文件大小：{exc.strerror or exc}") from exc
    if size > MAX_CONFIG_FILE_BYTES:
        raise ConfigError(f"文件超过 {MAX_CONFIG_FILE_BYTES // 1024} KB，拒绝读取。")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"无法按 UTF-8 读取文件：{exc}") from exc


def _strip_yaml_comment(value: str) -> str:
    """去掉未加引号值末尾的 ` # 注释`；路径和普通文本不受影响。"""
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.rstrip()


def _yaml_scalar(value: str) -> object:
    value = value.strip()
    if not value:
        return ""
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigError("双引号字符串格式无效。") from exc
        if not isinstance(parsed, str):
            raise ConfigError("双引号字段必须是字符串。")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ConfigError("单引号字符串没有闭合。")
        return value[1:-1].replace("''", "'")
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "~"}:
        return ""
    return value


def _yaml_inline_list(value: str) -> List[str]:
    body = value.strip()
    if not (body.startswith("[") and body.endswith("]")):
        raise ConfigError("行内列表必须用 [ ] 包围。")
    body = body[1:-1].strip()
    if not body:
        return []
    parts: List[str] = []
    current: List[str] = []
    quote = ""
    escaped = False
    for char in body:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote == '"':
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
            continue
        if char == ",":
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if quote:
        raise ConfigError("行内列表的引号没有闭合。")
    parts.append("".join(current).strip())
    items = []
    for part in parts:
        parsed = _yaml_scalar(part)
        if not isinstance(parsed, str):
            raise ConfigError("列表项必须是字符串。")
        items.append(parsed)
    return items


def _yaml_block_text(lines: List[str], indicator: str) -> str:
    content: List[str] = []
    for line in lines:
        if not line.strip():
            content.append("")
        elif line.startswith("  "):
            content.append(line[2:])
        elif line.startswith(" "):
            content.append(line[1:])
        elif line.startswith("\t"):
            content.append(line[1:])
        else:
            raise ConfigError("块文本的缩进不一致。")
    if indicator.startswith(">"):
        folded: List[str] = []
        for line in content:
            if line and folded and folded[-1]:
                folded[-1] += " " + line
            else:
                folded.append(line)
        content = folded
    text = "\n".join(content)
    if indicator.endswith("-"):
        return text.rstrip("\n")
    if indicator.endswith("+"):
        return text + "\n"
    return text.rstrip("\n") + ("\n" if text else "")


def _parse_simple_yaml(text: str) -> Dict[str, object]:
    """解析配置所需的小型 YAML 子集：平面映射、布尔、标量、块/行内列表、块文本。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in normalized:
        raise ConfigError("文件包含 NUL 字符。")
    lines = normalized.split("\n")
    data: Dict[str, object] = {}
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if raw.startswith(" ") or raw.startswith("\t"):
            raise ConfigError(f"第 {index + 1} 行出现顶层缩进。")
        match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*)$", raw)
        if not match:
            raise ConfigError(f"第 {index + 1} 行不是可识别的 key: value。")
        key = match.group(1)
        rest = _strip_yaml_comment(match.group(2))
        if key in data:
            raise ConfigError(f"字段 {key} 重复出现。")
        index += 1
        block: List[str] = []
        while index < len(lines) and (
            not lines[index].strip() or lines[index].startswith((" ", "\t"))
        ):
            block.append(lines[index])
            index += 1
        nonblank = [line for line in block if line.strip() and not line.strip().startswith("#")]

        if not rest:
            if not nonblank:
                data[key] = ""
            elif all(line.strip().startswith("- ") for line in nonblank):
                items = []
                for line in nonblank:
                    item_text = line.strip()[2:].strip()
                    parsed = _yaml_scalar(_strip_yaml_comment(item_text))
                    if not isinstance(parsed, str):
                        raise ConfigError(f"字段 {key} 的列表项必须是字符串。")
                    items.append(parsed)
                data[key] = items
            else:
                raise ConfigError(f"字段 {key} 下方的缩进内容无法识别。")
            continue

        if rest.startswith(("|", ">")):
            if rest not in {"|", "|-", "|+", ">", ">-", ">+"}:
                raise ConfigError(f"字段 {key} 的块文本标记不受支持。")
            data[key] = _yaml_block_text(block, rest)
            continue

        if nonblank:
            raise ConfigError(f"字段 {key} 后存在多余的缩进内容。")
        if rest.startswith("["):
            data[key] = _yaml_inline_list(rest)
        else:
            data[key] = _yaml_scalar(rest)
    return data


def _subtask_id_problem(
    subtask_id: str, expected_task_id: str = ""
) -> str:
    """子任务 ID 边界：只允许 <任务编号>-<数字>，不允许路径片段。"""
    value = str(subtask_id or "")
    if not value:
        return "缺少 id"
    if any(ord(char) < 32 for char in value):
        return "包含控制字符"
    if "/" in value or "\\" in value or ".." in value:
        return "包含路径分隔符或 .."
    if not SUBTASK_ID_RE.fullmatch(value):
        return "必须形如 <任务编号>-<序号>（如 T23-1）"
    if expected_task_id:
        prefix = str(expected_task_id).strip().upper()
        if not re.fullmatch(re.escape(prefix) + r"-\d+", value, re.IGNORECASE):
            return f"前缀必须是任务编号 {prefix}"
    return ""


def _parse_subtasks_text(
    text: str, expected_task_id: str = ""
) -> Tuple[Subtask, ...]:
    """解析 对接/subtasks.yaml 的固定结构。

    只接受 `subtasks:` 下的映射列表；支持引号、注释、手写缩进和 |/> 块文本。
    这是控制台展示用的窄解析器，不试图实现完整 YAML。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in normalized:
        raise ConfigError("subtasks.yaml 包含 NUL 字符。")
    lines = normalized.split("\n")
    start = None
    for index, raw in enumerate(lines):
        if re.match(r"^subtasks\s*:\s*(?:#.*)?$", raw):
            start = index + 1
            break
    if start is None:
        raise ConfigError("缺少顶层 `subtasks:` 列表。")

    records: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    block_key = ""
    block_parent_indent = 0
    block_style = ""
    block_lines: List[str] = []

    def finish_block() -> None:
        nonlocal block_key, block_style, block_lines
        if current is None or not block_key:
            block_key = ""
            block_style = ""
            block_lines = []
            return
        nonblank = [line for line in block_lines if line.strip()]
        indent = min(
            (len(line) - len(line.lstrip(" ")) for line in nonblank),
            default=0,
        )
        prepared = [
            line[indent:] if line.strip() else "" for line in block_lines
        ]
        if block_style.startswith(">"):
            folded: List[str] = []
            for line in prepared:
                if not line:
                    folded.append("")
                elif folded and folded[-1]:
                    folded[-1] += " " + line
                else:
                    folded.append(line)
            value = "\n".join(folded).rstrip("\n")
        else:
            value = "\n".join(prepared).rstrip("\n")
        current[block_key] = value
        block_key = ""
        block_style = ""
        block_lines = []

    def assign(key: str, raw_value: str, *, indent: int) -> None:
        nonlocal block_key, block_parent_indent, block_style, block_lines
        if current is None:
            raise ConfigError("子任务字段出现在列表项之外。")
        value = _strip_yaml_comment(raw_value.strip())
        if value.startswith(("|", ">")):
            if value not in {"|", "|-", "|+", ">", ">-", ">+"}:
                raise ConfigError(f"子任务字段 {key} 的块文本标记不受支持。")
            block_key = key
            block_parent_indent = indent
            block_style = value
            block_lines = []
            return
        parsed = _yaml_scalar(value)
        if parsed is None:
            parsed = ""
        current[key] = str(parsed)

    index = start
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            if block_key:
                block_lines.append("")
            index += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if block_key:
            if indent > block_parent_indent:
                block_lines.append(raw)
                index += 1
                continue
            finish_block()

        dash = re.match(r"^(\s*)-\s*(.*)$", raw)
        if dash:
            current = {}
            records.append(current)
            rest = dash.group(2).strip()
            if rest:
                field = re.match(
                    r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", rest
                )
                if not field:
                    raise ConfigError(
                        f"第 {index + 1} 行的列表项应为 key: value。"
                    )
                assign(field.group(1), field.group(2), indent=indent + 2)
            index += 1
            continue

        if indent == 0:
            # 列表结束，遇到下一个顶层字段。
            break
        if current is None:
            raise ConfigError(f"第 {index + 1} 行不属于任何子任务。")
        field = re.match(
            r"^(\s+)([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", raw
        )
        if not field:
            raise ConfigError(f"第 {index + 1} 行不是可识别的子任务字段。")
        assign(field.group(2), field.group(3), indent=indent)
        index += 1

    finish_block()

    subtasks: List[Subtask] = []
    seen_ids = set()
    for number, record in enumerate(records, start=1):
        task_id = str(record.get("id") or "").strip()
        title = str(record.get("title") or "").strip()
        problem = _subtask_id_problem(task_id, expected_task_id)
        if problem:
            raise ConfigError(
                f"第 {number} 个子任务 id 不合法：{task_id or '（空）'}（{problem}）。"
            )
        key = task_id.casefold()
        if key in seen_ids:
            raise ConfigError(f"子任务 id 重复：{task_id}")
        seen_ids.add(key)
        if not title:
            raise ConfigError(f"子任务 {task_id} 缺少 title。")
        status = str(record.get("status") or "pending").strip().lower()
        if status not in SUBTASK_STATUSES:
            raise ConfigError(
                f"子任务 {task_id} 的 status 不在允许范围：{status or '（空）'}"
            )
        subtasks.append(
            Subtask(
                id=task_id,
                title=title,
                goal=str(record.get("goal") or "").strip(),
                acceptance=str(record.get("acceptance") or "").strip(),
                status=status,
                agent_role=str(record.get("agent_role") or "").strip(),
                log=str(record.get("log") or "").strip(),
                run_id=str(record.get("run_id") or "").strip(),
                error=str(record.get("error") or "").strip(),
                started_at=str(record.get("started_at") or "").strip(),
                finished_at=str(record.get("finished_at") or "").strip(),
                attempt=_meta_int(record.get("attempt")),
            )
        )
    return tuple(subtasks)


def _load_subtasks(
    path: Path, expected_task_id: str = ""
) -> Tuple[Tuple[Subtask, ...], str]:
    if not path.is_file():
        return (), ""
    try:
        size = path.stat().st_size
        if size > MAX_SUBTASKS_FILE_BYTES:
            return (), f"{SUBTASKS_NAME} 超过 {MAX_SUBTASKS_FILE_BYTES // 1024} KB。"
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return (), f"读取 {SUBTASKS_NAME} 失败：{exc}"
    if not expected_task_id:
        try:
            match = PROJECT_NAME_RE.fullmatch(Path(path).parent.parent.name)
        except (OSError, ValueError):
            match = None
        if match:
            expected_task_id = match.group(1)
    try:
        return _parse_subtasks_text(text, expected_task_id), ""
    except ConfigError as exc:
        return (), str(exc)


def _subtask_record_blocks(text: str) -> List[Dict[str, object]]:
    """定位 subtasks.yaml 中每个列表项的物理行范围。

    只用于原子更新状态行；识别 `|`/`>` 块文本，避免把块内容误认为字段。
    返回值键：id、start、last、dash_indent、field_indent、fields。
    """
    lines = text.split("\n")
    start = None
    for index, raw in enumerate(lines):
        if re.match(r"^subtasks\s*:\s*(?:#.*)?$", raw):
            start = index + 1
            break
    if start is None:
        raise ConfigError("缺少顶层 `subtasks:` 列表。")

    records: List[Dict[str, object]] = []
    current: Optional[Dict[str, object]] = None
    list_indent: Optional[int] = None
    in_block = False
    block_parent_indent = 0

    def finish() -> None:
        nonlocal current
        if current is not None:
            current["last"] = current.get("last", current["start"])
            records.append(current)
            current = None

    index = start
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if in_block:
            indent = len(raw) - len(raw.lstrip(" "))
            if not stripped or indent > block_parent_indent:
                if current is not None:
                    current["last"] = index
                index += 1
                continue
            in_block = False
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        dash = re.match(r"^\s*-\s*(.*)$", raw)
        if dash and (list_indent is None or indent == list_indent):
            finish()
            if list_indent is None:
                list_indent = indent
            current = {
                "id": "",
                "start": index,
                "last": index,
                "dash_indent": indent,
                "field_indent": indent + 2,
                "fields": {},
            }
            rest = dash.group(1).strip()
            if rest:
                field = re.match(
                    r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", rest
                )
                if field:
                    key = field.group(1)
                    value = _strip_yaml_comment(field.group(2).strip())
                    current["fields"][key] = index
                    current["field_indent"] = indent + 2
                    if key == "id":
                        parsed = _yaml_scalar(value)
                        current["id"] = str(parsed or "").strip()
                    if value.startswith(("|", ">")):
                        in_block = True
                        block_parent_indent = indent
            index += 1
            continue

        if current is None:
            index += 1
            continue
        if indent == 0:
            finish()
            break
        field = re.match(r"^(\s+)([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", raw)
        if field:
            key = field.group(2)
            value = _strip_yaml_comment(field.group(3).strip())
            current["fields"][key] = index
            current["last"] = index
            current["field_indent"] = len(field.group(1))
            if key == "id" and not current.get("id"):
                parsed = _yaml_scalar(value)
                current["id"] = str(parsed or "").strip()
            if value.startswith(("|", ">")):
                in_block = True
                block_parent_indent = indent
            index += 1
            continue
        current["last"] = index
        index += 1

    finish()
    return records


def _render_subtask_field(key: str, value: str) -> str:
    if key == "status":
        return value
    if key in {"started_at", "finished_at"}:
        return value
    return json.dumps(value, ensure_ascii=False)


def _update_subtask_text(
    text: str, subtask_id: str, updates: Dict[str, str]
) -> str:
    """在原文上更新一个子任务的字段，保留其它行、注释与顺序。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    records = _subtask_record_blocks(normalized)
    target = next(
        (record for record in records if str(record.get("id") or "") == subtask_id),
        None,
    )
    if target is None:
        raise ConfigError(f"subtasks.yaml 中找不到子任务 {subtask_id}。")

    lines = normalized.split("\n")
    fields = target["fields"]
    assert isinstance(fields, dict)
    dash_indent = int(target["dash_indent"])
    field_indent = int(target["field_indent"])
    insert_at = int(target["last"]) + 1
    new_lines: List[str] = []

    for key, value in updates.items():
        rendered = _render_subtask_field(key, value)
        line_index = fields.get(key)
        if line_index is not None:
            line_index = int(line_index)
            old_line = lines[line_index]
            old_value = old_line.split(":", 1)[1].strip() if ":" in old_line else ""
            old_value = _strip_yaml_comment(old_value)
            if old_value.startswith(("|", ">")):
                raise ConfigError(
                    f"子任务 {subtask_id} 的 {key} 是块文本，拒绝自动改写。"
                )
            prefix = old_line[: len(old_line) - len(old_line.lstrip(" "))]
            lines[line_index] = f"{prefix}{key}: {rendered}"
        else:
            new_lines.append(f"{' ' * field_indent}{key}: {rendered}")

    if new_lines:
        if insert_at > len(lines):
            insert_at = len(lines)
        lines[insert_at:insert_at] = new_lines
    return "\n".join(lines)


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(str(temporary), path.stat().st_mode & 0o777)
        except OSError:
            pass
        os.replace(str(temporary), str(path))
    except OSError as exc:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        raise ConfigError(f"无法写入 {path.name}：{exc.strerror or exc}") from exc


def _update_subtask_record(
    path: Path, subtask_id: str, updates: Dict[str, str]
) -> None:
    with TASK_WRITE_LOCK:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"无法读取 {SUBTASKS_NAME}：{exc}") from exc
        updated = _update_subtask_text(text, subtask_id, updates)
        _atomic_write_text(path, updated)


def _yaml_plain_safe(value: str) -> bool:
    if not value or value != value.strip() or "\n" in value or "\t" in value:
        return False
    if value.lower() in {
        "true",
        "false",
        "null",
        "~",
        "yes",
        "no",
        "on",
        "off",
        "y",
        "n",
    }:
        return False
    if re.fullmatch(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?", value, re.IGNORECASE):
        return False
    if value[0] in "-?:,[]{}#&*!|>'\"%@`":
        return False
    if ": " in value or value.endswith(":") or " #" in value:
        return False
    return True


def _yaml_emit_scalar(value: str) -> str:
    if _yaml_plain_safe(value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _yaml_emit_field(key: str, value: str) -> str:
    if "\n" not in value:
        return f"{key}: {_yaml_emit_scalar(value)}\n"
    indicator = "|" if value.endswith("\n") else "|-"
    body = value.rstrip("\n")
    rows = "".join(f"  {line}\n" if line else "\n" for line in body.split("\n"))
    return f"{key}: {indicator}\n{rows}"


def _role_yaml(role: Role) -> str:
    lines = [
        _yaml_emit_field("id", role.id),
        _yaml_emit_field("name", role.name),
        _yaml_emit_field("description", role.description),
        _yaml_emit_field("agent_id", role.agent_id),
    ]
    if role.write_scope:
        lines.append("write_scope:\n")
        lines.extend(f"  - {_yaml_emit_scalar(item)}\n" for item in role.write_scope)
    else:
        lines.append("write_scope: []\n")
    lines.append(f"enabled: {'true' if role.enabled else 'false'}\n")
    lines.append(_yaml_emit_field("notes", role.notes))
    return "".join(lines)


def _agent_yaml(agent: AgentApp) -> str:
    lines = [
        _yaml_emit_field("id", agent.id),
        _yaml_emit_field("name", agent.name),
        _yaml_emit_field("command", agent.command),
        _yaml_emit_field("args_template", agent.args_template),
        _yaml_emit_field("cwd", agent.cwd),
        f"installed: {'true' if agent.installed else 'false'}\n",
        _yaml_emit_field("version", agent.version),
        _yaml_emit_field("notes", agent.notes),
    ]
    return "".join(lines)


def _text_value(data: Dict[str, object], key: str, default: str = "") -> str:
    raw = data.get(key, default)
    if raw is None:
        return ""
    if isinstance(raw, (dict, list)):
        raise ConfigError(f"字段 {key} 必须是文本。")
    if isinstance(raw, bool):
        raise ConfigError(f"字段 {key} 必须是文本，不能是布尔值。")
    return str(raw).strip()


def _bool_value(data: Dict[str, object], key: str, default: bool) -> bool:
    raw = data.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise ConfigError(f"字段 {key} 必须是 true 或 false。")


def _scope_value(data: Dict[str, object]) -> Tuple[str, ...]:
    raw = data.get("write_scope", [])
    if raw in ("", None):
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ConfigError("字段 write_scope 必须是字符串列表。")
    scopes: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ConfigError("字段 write_scope 的列表项必须是字符串。")
        scope = item.strip()
        if not scope:
            continue
        if len(scope) > MAX_SCOPE_CHARS:
            raise ConfigError(f"写入范围单项不能超过 {MAX_SCOPE_CHARS} 字符。")
        if scope not in scopes:
            scopes.append(scope)
    if len(scopes) > MAX_SCOPE_ITEMS:
        raise ConfigError(f"写入范围最多 {MAX_SCOPE_ITEMS} 项。")
    return tuple(scopes)


def _role_from_data(data: Dict[str, object], path: Path) -> Role:
    role_id = _text_value(data, "id")
    if role_id != path.stem:
        raise ConfigError("id 必须与 YAML 文件名一致。")
    if not CONFIG_ID_RE.fullmatch(role_id):
        raise ConfigError("id 只能包含小写字母、数字、连字符与下划线，且须以字母或数字开头。")
    name = _text_value(data, "name")
    if not name:
        raise ConfigError("name 不能为空。")
    if len(name) > MAX_CONFIG_NAME_CHARS:
        raise ConfigError(f"name 不能超过 {MAX_CONFIG_NAME_CHARS} 字符。")
    description = _text_value(data, "description")
    notes = _text_value(data, "notes")
    if len(description) > MAX_CONFIG_TEXT_CHARS or len(notes) > MAX_CONFIG_TEXT_CHARS:
        raise ConfigError(f"description 与 notes 分别不能超过 {MAX_CONFIG_TEXT_CHARS} 字符。")
    agent_id = _text_value(data, "agent_id")
    if agent_id and not CONFIG_ID_RE.fullmatch(agent_id):
        raise ConfigError("agent_id 格式不正确。")
    try:
        stat = path.stat()
    except OSError as exc:
        raise ConfigError(f"无法读取文件信息：{exc}") from exc
    return Role(
        id=role_id,
        name=name,
        description=description,
        agent_id=agent_id,
        write_scope=_scope_value(data),
        enabled=_bool_value(data, "enabled", True),
        notes=notes,
        path=path,
        mtime=stat.st_mtime,
    )


def _agent_from_data(data: Dict[str, object], path: Path) -> AgentApp:
    agent_id = _text_value(data, "id")
    if agent_id != path.stem:
        raise ConfigError("id 必须与 YAML 文件名一致。")
    if not CONFIG_ID_RE.fullmatch(agent_id):
        raise ConfigError("id 只能包含小写字母、数字、连字符与下划线，且须以字母或数字开头。")
    name = _text_value(data, "name")
    if not name:
        raise ConfigError("name 不能为空。")
    if len(name) > MAX_CONFIG_NAME_CHARS:
        raise ConfigError(f"name 不能超过 {MAX_CONFIG_NAME_CHARS} 字符。")
    command = _text_value(data, "command")
    if not command or not Path(command).is_absolute():
        raise ConfigError("command 必须是绝对路径。")
    args_template = _text_value(data, "args_template")
    cwd = _text_value(data, "cwd")
    if cwd and not Path(cwd).is_absolute():
        raise ConfigError("cwd 必须是绝对路径。")
    notes = _text_value(data, "notes")
    if len(args_template) > MAX_CONFIG_TEXT_CHARS or len(notes) > MAX_CONFIG_TEXT_CHARS:
        raise ConfigError(f"args_template 与 notes 分别不能超过 {MAX_CONFIG_TEXT_CHARS} 字符。")
    version = _text_value(data, "version")[:MAX_STORED_VERSION_CHARS]
    try:
        stat = path.stat()
    except OSError as exc:
        raise ConfigError(f"无法读取文件信息：{exc}") from exc
    return AgentApp(
        id=agent_id,
        name=name,
        command=command,
        args_template=args_template,
        cwd=cwd,
        installed=_bool_value(data, "installed", False),
        version=version,
        notes=notes,
        path=path,
        mtime=stat.st_mtime,
    )


def _load_roles() -> Tuple[List[Role], List[ConfigIssue]]:
    if not ROLES_DIR.is_dir():
        return [], []
    try:
        paths = sorted(ROLES_DIR.glob("*.yaml"), key=lambda item: item.name.casefold())
    except OSError as exc:
        return [], [ConfigIssue(ROLES_DIR, f"无法列出角色目录：{exc}")]
    roles: List[Role] = []
    issues: List[ConfigIssue] = []
    for path in paths:
        try:
            if not path.is_file():
                continue
            data = _parse_simple_yaml(_read_config_text(path))
            roles.append(_role_from_data(data, path))
        except ConfigError as exc:
            issues.append(ConfigIssue(path, str(exc)))
        except OSError as exc:
            issues.append(ConfigIssue(path, f"读取失败：{exc}"))
    roles.sort(key=lambda role: role.path.name.casefold())
    return roles, issues


def _load_agents() -> Tuple[List[AgentApp], List[ConfigIssue]]:
    if not AGENTS_DIR.is_dir():
        return [], []
    try:
        paths = sorted(AGENTS_DIR.glob("*.yaml"), key=lambda item: item.name.casefold())
    except OSError as exc:
        return [], [ConfigIssue(AGENTS_DIR, f"无法列出 Agent 目录：{exc}")]
    agents: List[AgentApp] = []
    issues: List[ConfigIssue] = []
    for path in paths:
        try:
            if not path.is_file():
                continue
            data = _parse_simple_yaml(_read_config_text(path))
            agents.append(_agent_from_data(data, path))
        except ConfigError as exc:
            issues.append(ConfigIssue(path, str(exc)))
        except OSError as exc:
            issues.append(ConfigIssue(path, f"读取失败：{exc}"))
    agents.sort(key=lambda agent: agent.id)
    return agents, issues


def _write_config_text(path: Path, text: str, *, create_only: bool = False) -> None:
    """只允许在 协作/roles 与 协作/agents 下写 YAML，不提供删除操作。"""
    allowed_parents = {
        ROLES_DIR.resolve(),
        AGENTS_DIR.resolve(),
        ROLES_ARCHIVE_DIR.resolve(),
    }
    if path.suffix != ".yaml" or path.parent.resolve() not in allowed_parents:
        raise ConfigError("拒绝写入配置目录之外的路径。")
    path.parent.mkdir(parents=True, exist_ok=True)
    if create_only:
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            raise
        except OSError as exc:
            raise ConfigError(f"写入 {path.name} 失败：{exc.strerror or exc}") from exc
        return
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    except OSError as exc:
        raise ConfigError(f"写入 {path.name} 失败：{exc.strerror or exc}") from exc


def _ensure_default_roles() -> List[Path]:
    """仅在 roles 目录尚不存在时播种三个默认角色，不覆盖、不复活已移出的角色。"""
    if ROLES_DIR.exists():
        return []
    created: List[Path] = []
    with CONFIG_WRITE_LOCK:
        if ROLES_DIR.exists():
            return []
        try:
            ROLES_DIR.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            print(f"[配置] 无法创建 协作/roles：{exc}")
            return []
        for role_id, name, description in DEFAULT_ROLE_SPECS:
            path = ROLES_DIR / f"{role_id}.yaml"
            role = Role(
                id=role_id,
                name=name,
                description=description,
                agent_id="",
                write_scope=(),
                enabled=True,
                notes="首次运行生成的默认角色；可按需修改。",
                path=path,
                mtime=0.0,
            )
            try:
                _write_config_text(path, _role_yaml(role), create_only=True)
                created.append(path)
            except (ConfigError, FileExistsError) as exc:
                print(f"[配置] 未能写入默认角色 {path.name}：{exc}")
    return created


def _find_role(role_id: str) -> Optional[Role]:
    roles, _issues = _load_roles()
    for role in roles:
        if role.id == role_id:
            return role
    return None


def _find_agent(agent_id: str, agents: Optional[List[AgentApp]] = None) -> Optional[AgentApp]:
    entries = agents if agents is not None else _load_agents()[0]
    for agent in entries:
        if agent.id == agent_id:
            return agent
    return None


def _agent_dispatch_ready(agent: Optional[AgentApp]) -> bool:
    return bool(agent and agent.installed and agent.currently_executable)


def _resolve_controller() -> ControllerResolution:
    """按 controller 角色绑定 → openclaw-main → deepseek-harness 解析总控。"""
    agents, _issues = _load_agents()
    role = _find_role(CONTROLLER_ROLE_ID)
    bound = role.agent_id.strip() if role and role.agent_id else ""
    if bound:
        agent = _find_agent(bound, agents)
        if _agent_dispatch_ready(agent):
            assert agent is not None
            return ControllerResolution(
                agent=agent,
                source=f"角色 controller → {agent.name}（{agent.id}）",
                note="使用角色 controller 绑定的 Agent。",
            )
        reason = (
            f"角色 controller 绑定的 {bound} 未登记"
            if agent is None
            else f"角色 controller 绑定的 {bound} 当前不可执行"
        )
    else:
        reason = "角色 controller 尚未绑定 Agent"

    for fallback_id in CONTROLLER_FALLBACK_IDS:
        agent = _find_agent(fallback_id, agents)
        if _agent_dispatch_ready(agent):
            assert agent is not None
            return ControllerResolution(
                agent=agent,
                source=f"回退总控 → {agent.name}（{agent.id}）",
                note=f"{reason}，已按顺序回退到 {fallback_id}。",
            )
    return ControllerResolution(
        agent=None,
        source="没有可用总控",
        note=(
            f"{reason}；回退候选 {'、'.join(CONTROLLER_FALLBACK_IDS)} "
            "也都未登记或不可执行。请到 /agents 检查。"
        ),
    )


def _normalize_form_text(value: str, label: str, limit: int, *, required: bool = False) -> str:
    text = (
        value.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\x00", "")
        .strip()
    )
    if required and not text:
        raise FormError(f"{label}不能为空。")
    if len(text) > limit:
        raise FormError(f"{label}不能超过 {limit} 字符。")
    return text


def _validate_scope_lines(value: str) -> Tuple[str, ...]:
    scopes: List[str] = []
    for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        scope = line.strip()
        if not scope:
            continue
        if len(scope) > MAX_SCOPE_CHARS:
            raise FormError(f"写入范围单项不能超过 {MAX_SCOPE_CHARS} 字符。")
        if "\x00" in scope:
            raise FormError("写入范围包含无效字符。")
        if scope not in scopes:
            scopes.append(scope)
    if len(scopes) > MAX_SCOPE_ITEMS:
        raise FormError(f"写入范围最多 {MAX_SCOPE_ITEMS} 项。")
    return tuple(scopes)


def _build_role_values(fields: Dict[str, str], role_id: Optional[str] = None) -> Dict[str, str]:
    values = {
        "id": role_id if role_id is not None else fields.get("id", "").strip().lower(),
        "name": fields.get("name", ""),
        "description": fields.get("description", ""),
        "agent_id": fields.get("agent_id", "").strip(),
        "write_scope": fields.get("write_scope", ""),
        "enabled": "1" if fields.get("enabled") else "",
        "notes": fields.get("notes", ""),
    }
    return values


def _role_from_form(fields: Dict[str, str], role_id: str) -> Role:
    if not CONFIG_ID_RE.fullmatch(role_id):
        raise FormError("角色 ID 只能包含小写字母、数字、连字符与下划线，且须以字母或数字开头。")
    name = _normalize_form_text(fields.get("name", ""), "角色名称", MAX_CONFIG_NAME_CHARS, required=True)
    description = _normalize_form_text(
        fields.get("description", ""), "角色说明", MAX_CONFIG_TEXT_CHARS
    )
    notes = _normalize_form_text(fields.get("notes", ""), "备注", MAX_CONFIG_TEXT_CHARS)
    agent_id = fields.get("agent_id", "").strip()
    if agent_id:
        if not CONFIG_ID_RE.fullmatch(agent_id) or _find_agent(agent_id) is None:
            raise FormError("绑定的 Agent 不在已登记列表中；请先在 /agents 登记。")
    path = ROLES_DIR / f"{role_id}.yaml"
    return Role(
        id=role_id,
        name=name,
        description=description,
        agent_id=agent_id,
        write_scope=_validate_scope_lines(fields.get("write_scope", "")),
        enabled=bool(fields.get("enabled")),
        notes=notes,
        path=path,
        mtime=0.0,
    )


def _save_new_role(fields: Dict[str, str]) -> str:
    role_id = fields.get("id", "").strip().lower()
    if not role_id:
        raise FormError("角色 ID 不能为空。")
    role = _role_from_form(fields, role_id)
    path = ROLES_DIR / f"{role_id}.yaml"
    with CONFIG_WRITE_LOCK:
        if path.exists() or path.is_symlink():
            raise FormError(f"角色 ID「{role_id}」已存在，没有覆盖原文件。")
        try:
            _write_config_text(path, _role_yaml(role), create_only=True)
        except FileExistsError as exc:
            raise FormError(f"角色 ID「{role_id}」刚刚被占用，请换一个。") from exc
    return role_id


def _save_existing_role(role_id: str, fields: Dict[str, str]) -> str:
    role = _role_from_form(fields, role_id)
    path = ROLES_DIR / f"{role_id}.yaml"
    with CONFIG_WRITE_LOCK:
        if not path.is_file():
            raise FormError("角色文件已不存在，没有写入。")
        _write_config_text(path, _role_yaml(role))
    return role_id


def _move_role_to_archive(role_id: str) -> Path:
    """把角色移出列表但保留原 YAML，满足可删除语义与零删除约束。"""
    with CONFIG_WRITE_LOCK:
        role = _find_role(role_id)
        if role is None:
            raise FormError("没有找到这个角色，未做任何移动。")
        try:
            ROLES_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FormError(f"无法创建角色归档目录：{exc.strerror or exc}") from exc
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = ROLES_ARCHIVE_DIR / f"{role_id}.{stamp}.yaml"
        sequence = 2
        while target.exists() or target.is_symlink():
            target = ROLES_ARCHIVE_DIR / f"{role_id}.{stamp}-{sequence}.yaml"
            sequence += 1
        try:
            os.replace(str(role.path), str(target))
        except OSError as exc:
            raise FormError(f"角色文件移动失败：{exc.strerror or exc}") from exc
        return target


def _run_version_probe(command: str) -> str:
    path = Path(command)
    if not path.is_absolute() or not path.is_file() or not os.access(str(path), os.X_OK):
        return "不可执行，未探测版本"
    try:
        result = subprocess.run(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=VERSION_TIMEOUT_SECONDS,
            text=True,
            errors="replace",
            cwd=str(Path.home()),
        )
    except subprocess.TimeoutExpired:
        return f"--version 超时（>{VERSION_TIMEOUT_SECONDS} 秒）"
    except OSError as exc:
        return f"版本探测失败：{exc.strerror or exc}"
    output = " ".join((result.stdout or "").split())
    if not output:
        output = f"退出码 {result.returncode}，无版本输出"
    return output[:MAX_STORED_VERSION_CHARS]


def _candidate_paths() -> List[Tuple[Path, str]]:
    candidates: List[Tuple[Path, str]] = []
    seen = set()
    known_bin = Path.home() / "claude-model" / "bin"
    if known_bin.is_dir():
        try:
            entries = sorted(known_bin.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            entries = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if not (entry.is_file() or entry.is_symlink()):
                    continue
            except OSError:
                continue
            path = entry.absolute()
            key = str(path)
            if key not in seen:
                seen.add(key)
                candidates.append((path, "~/claude-model/bin"))

    for name in KNOWN_AGENT_COMMANDS:
        found = shutil.which(name)
        if not found:
            continue
        path = Path(found).absolute()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        candidates.append((path, "command -v"))
    candidates.sort(key=lambda item: str(item[0]).casefold())
    return candidates


def _detect_agents() -> List[DetectedAgent]:
    candidates = _candidate_paths()
    if not candidates:
        return []

    def probe(item: Tuple[Path, str]) -> DetectedAgent:
        path, source = item
        executable = path.is_file() and os.access(str(path), os.X_OK)
        version = (
            _run_version_probe(str(path))
            if executable
            else "不可执行，未探测版本"
        )
        return DetectedAgent(path=str(path), executable=executable, version=version, source=source)

    max_workers = max(1, min(8, len(candidates)))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent-probe") as pool:
        futures = [pool.submit(probe, item) for item in candidates]
        detected: List[DetectedAgent] = []
        for future in futures:
            try:
                detected.append(future.result())
            except Exception:
                traceback.print_exc()
    detected.sort(key=lambda item: item.path.casefold())
    return detected


def _same_agent_command(command: str, detected_path: str) -> bool:
    try:
        return os.path.realpath(os.path.expanduser(command)) == os.path.realpath(detected_path)
    except OSError:
        return os.path.abspath(os.path.expanduser(command)) == os.path.abspath(detected_path)


def _suggest_agent_id(path: str, existing_ids: List[str]) -> str:
    candidate = re.sub(r"[^a-z0-9_-]+", "-", Path(path).name.lower()).strip("-_")
    if not candidate:
        candidate = "agent"
    if not candidate[0].isalnum():
        candidate = f"agent-{candidate}"
    candidate = candidate[:48].rstrip("-_") or "agent"
    if candidate not in existing_ids:
        return candidate
    base = candidate[:44]
    sequence = 2
    while f"{base}-{sequence}" in existing_ids:
        sequence += 1
    return f"{base}-{sequence}"


def _build_agent_values(fields: Dict[str, str]) -> Dict[str, str]:
    return {
        "id": fields.get("id", "").strip().lower(),
        "name": fields.get("name", ""),
        "command": fields.get("command", "").strip(),
        "args_template": fields.get("args_template", ""),
        "cwd": fields.get("cwd", "").strip(),
        "notes": fields.get("notes", ""),
    }


def _save_new_agent(fields: Dict[str, str]) -> str:
    agent_values = _build_agent_values(fields)
    agent_id = agent_values["id"]
    if not agent_id:
        raise FormError("Agent ID 不能为空。")
    if not CONFIG_ID_RE.fullmatch(agent_id):
        raise FormError("Agent ID 只能包含小写字母、数字、连字符与下划线，且须以字母或数字开头。")
    name = _normalize_form_text(agent_values["name"], "Agent 名称", MAX_CONFIG_NAME_CHARS, required=True)
    command = agent_values["command"].replace("\x00", "")
    if not command:
        raise FormError("CLI 绝对路径不能为空。")
    if not Path(command).is_absolute():
        raise FormError("CLI 路径必须是绝对路径，例如 /Users/me/bin/claude-ds。")
    cwd = agent_values["cwd"].replace("\x00", "")
    if cwd and not Path(cwd).is_absolute():
        raise FormError("工作目录必须是绝对路径。")
    args_template = _normalize_form_text(
        agent_values["args_template"], "参数模板", MAX_CONFIG_TEXT_CHARS
    )
    notes = _normalize_form_text(agent_values["notes"], "备注", MAX_CONFIG_TEXT_CHARS)
    installed = Path(command).is_file() and os.access(command, os.X_OK)
    version = _run_version_probe(command) if installed else "不可执行，未探测版本"
    path = AGENTS_DIR / f"{agent_id}.yaml"
    agent = AgentApp(
        id=agent_id,
        name=name,
        command=command,
        args_template=args_template,
        cwd=cwd,
        installed=installed,
        version=version,
        notes=notes,
        path=path,
        mtime=0.0,
    )
    with CONFIG_WRITE_LOCK:
        if path.exists() or path.is_symlink():
            raise FormError(f"Agent ID「{agent_id}」已登记，没有覆盖原文件。")
        try:
            _write_config_text(path, _agent_yaml(agent), create_only=True)
        except FileExistsError as exc:
            raise FormError(f"Agent ID「{agent_id}」刚刚被占用，请换一个。") from exc
    return agent_id


# ---------------------------------------------------------------------------
# Markdown 安全渲染（先转义，再构造受限标签）
# ---------------------------------------------------------------------------


def _inline_markdown(value: str) -> str:
    escaped = html.escape(value, quote=False)
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", escaped)
    return escaped


def render_markdown(text: str) -> str:
    """覆盖协作档案会用到的 Markdown 子集；不支持原始 HTML，所有文本均转义。"""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    output: List[str] = []
    paragraph: List[str] = []
    list_kind: Optional[str] = None
    in_code = False
    code_lines: List[str] = []

    def close_paragraph() -> None:
        if paragraph:
            output.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_kind
        if list_kind:
            output.append(f"</{list_kind}>")
            list_kind = None

    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()

        if in_code:
            if stripped.startswith("```"):
                output.append(
                    "<pre><code>"
                    + html.escape("\n".join(code_lines), quote=False)
                    + "</code></pre>"
                )
                in_code = False
                code_lines = []
            else:
                code_lines.append(raw)
            index += 1
            continue

        if stripped.startswith("```"):
            close_paragraph()
            close_list()
            in_code = True
            code_lines = []
            index += 1
            continue

        if not stripped:
            close_paragraph()
            close_list()
            index += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_paragraph()
            close_list()
            level = len(heading.group(1))
            output.append(f"<h{level}>{_inline_markdown(heading.group(2).strip())}</h{level}>")
            index += 1
            continue

        if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", stripped):
            close_paragraph()
            close_list()
            output.append("<hr>")
            index += 1
            continue

        if stripped.startswith(">"):
            close_paragraph()
            close_list()
            quoted: List[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quoted.append(_inline_markdown(lines[index].strip()[1:].lstrip()))
                index += 1
            output.append("<blockquote>" + "<br>".join(quoted) + "</blockquote>")
            continue

        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        ordered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if bullet or ordered:
            close_paragraph()
            kind = "ul" if bullet else "ol"
            if list_kind != kind:
                close_list()
                output.append(f"<{kind}>")
                list_kind = kind
            item = (bullet or ordered).group(1)  # type: ignore[union-attr]
            output.append(f"<li>{_inline_markdown(item)}</li>")
            index += 1
            continue

        close_list()
        paragraph.append(_inline_markdown(stripped))
        index += 1

    if in_code:
        output.append(
            "<pre><code>" + html.escape("\n".join(code_lines), quote=False) + "</code></pre>"
        )
    close_paragraph()
    close_list()
    return "\n".join(output)


# ---------------------------------------------------------------------------
# HTML 页面
# ---------------------------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _tone(status: str) -> str:
    return STATUS_TONES[status]


def _stamp(status: str, extra_class: str = "") -> str:
    extra = f" {extra_class}" if extra_class else ""
    return (
        f'<span class="stamp stamp--{_tone(status)}{extra}">'
        f"{_esc(status)}</span>"
    )


def _render_move_form(
    *,
    action: str,
    project_name: str,
    label: str,
    confirm: str,
    button_class: str,
    aria_label: str,
) -> str:
    return f"""
      <form class="move-form" method="post" action="{_esc(action)}" data-confirm="{_esc(confirm)}">
        <input type="hidden" name="project" value="{_esc(project_name)}">
        <button class="{button_class}" type="submit" aria-label="{_esc(aria_label)}">{_esc(label)}</button>
      </form>
    """


def _archive_form(task: Task, *, button_class: str = "button button--ghost") -> str:
    return _render_move_form(
        action=f"/archive/{task.task_id}",
        project_name=task.project_name,
        label="归档",
        confirm=(
            f"确认归档 {task.task_id}「{task.title}」？"
            "整个项目目录会移动到 归档/，不会删除文件，可随时恢复。"
        ),
        button_class=button_class,
        aria_label=f"归档 {task.task_id} {task.title}",
    )


def _restore_form(task: Task, *, button_class: str = "button button--ghost") -> str:
    return _render_move_form(
        action=f"/restore/{task.task_id}",
        project_name=task.project_name,
        label="恢复",
        confirm=(
            f"确认恢复 {task.task_id}「{task.title}」？"
            "整个项目目录会移回 项目/；若同名目录已存在，将加时间戳后缀。"
        ),
        button_class=button_class,
        aria_label=f"恢复 {task.task_id} {task.title}",
    )


def _phase_pill(phase: str) -> str:
    return (
        f'<span class="run-pill run-pill--{_esc(_phase_tone(phase))}">'
        f"阶段 · {_esc(_phase_label(phase))}</span>"
    )


def _render_dispatch_panel(task: Task, phase: str = PHASE_UNDISPATCHED) -> str:
    """任务详情里的分发面板：按 subtasks.yaml 的角色绑定自动串行执行。"""
    if task.archived:
        return ""
    state = _dispatch_state_snapshot(task.task_id)
    state_name = str(state.get("state") or "idle")
    if state_name not in DISPATCH_STATE_NAMES:
        state_name = "idle"
    can_start, reason = _dispatch_availability(task)
    pending = sum(1 for item in task.subtasks if item.status == "pending")
    failed_items = sum(1 for item in task.subtasks if item.status == "failed")
    delivered = sum(
        1 for item in task.subtasks if item.status in {"delivered", "passed"}
    )
    target_count = pending + failed_items
    action_label = _dispatch_action_label(task)
    continuing = action_label.startswith("继续分发")
    total = int(state.get("total") or 0) or (target_count or len(task.subtasks))
    done = int(state.get("done") or 0)
    success = int(state.get("success") or 0)
    failed = int(state.get("failed") or 0)
    current_index = int(state.get("current_index") or 0)
    current_subtask = str(state.get("current_subtask") or "")
    current_title = str(state.get("current_title") or "")
    message = str(state.get("message") or "")
    running = state_name == "running"

    if running:
        live_html = f"""
        <div class="dispatch-live" id="dispatch-live" role="status" aria-live="polite">
          <p class="dispatch-live-title"><span class="dispatch-pulse" aria-hidden="true"></span>
            <span id="dispatch-live-label">分发中…</span></p>
          <dl class="dispatch-live-meta">
            <div><dt>当前子任务</dt>
              <dd><code id="dispatch-current">{_esc(current_subtask or '准备中')}</code>
              <span id="dispatch-current-title">{_esc(current_title)}</span></dd></div>
            <div><dt>进度</dt>
              <dd><strong id="dispatch-counter">{done}/{total}</strong></dd></div>
          </dl>
          <p class="dispatch-live-note">{_esc(message or '正在按角色绑定串行启动 Agent。')}</p>
        </div>
        """
    else:
        live_html = f"""
        <div class="dispatch-live" id="dispatch-live" hidden
          data-idle="{_esc(str(current_index))}">
          <p class="dispatch-live-title"><span class="dispatch-pulse" aria-hidden="true"></span>
            <span id="dispatch-live-label">分发中…</span></p>
          <dl class="dispatch-live-meta">
            <div><dt>当前子任务</dt>
              <dd><code id="dispatch-current">{_esc(current_subtask)}</code>
              <span id="dispatch-current-title">{_esc(current_title)}</span></dd></div>
            <div><dt>进度</dt>
              <dd><strong id="dispatch-counter">{done}/{total}</strong></dd></div>
          </dl>
        </div>
        """

    if state_name in {"completed", "failed"}:
        tone = "notice--ok" if state_name == "completed" else "notice--error"
        heading = "分发完成" if state_name == "completed" else "分发已停止"
        summary_html = f"""
        <div class="notice {tone} dispatch-summary" role="status">
          <strong>{heading}：成功 {success} / 失败 {failed}。</strong>
          <span>{_esc(message)}</span>
        </div>
        """
    else:
        summary_html = ""

    button_label = "分发中…" if running else action_label
    disabled_attr = "" if can_start else " disabled"
    retry_note = (
        f"，并把 {failed_items} 个失败子任务重置为 pending 后重试" if failed_items else ""
    )
    confirm = (
        f"确认{'继续分发' if continuing else '开始分发实现'} {task.task_id}？"
        f"系统会按 subtasks.yaml 的 agent_role 逐个串行启动 Agent"
        f"（本次 {target_count} 个待开始/失败子任务，已交付 {delivered} 个不会重跑{retry_note}）；"
        "同一时刻只允许一个 Agent 运行。"
    )
    hint = reason
    state_badge = (
        f'<span class="run-pill run-pill--{DISPATCH_STATE_TONES.get(state_name, "idle")}">'
        f"{_esc(DISPATCH_STATE_NAMES.get(state_name, state_name))}</span>"
    )
    return f"""
      <section class="dispatch-panel" id="dispatch-panel" aria-labelledby="dispatch-panel-title"
        data-status-url="/api/task/{_esc(task.task_id)}/dispatch-status"
        data-state="{_esc(state_name)}"
        data-phase="{_esc(phase)}"
        data-poll-ms="{DISPATCH_POLL_INTERVAL_MS}">
        <div class="dispatch-panel-head">
          <div>
            <p class="aside-kicker">NEXT STEP / 分发实现</p>
            <h2 id="dispatch-panel-title">下一步：{'继续分发' if continuing else '开始分发实现'}</h2>
            <p>{f'已交付 {delivered} 个；本次只处理待开始与失败的子任务，不会重跑已交付项。' if continuing else '不需要在本页选择 Agent：系统读取 <code>对接/subtasks.yaml</code> 的 <code>agent_role</code>，找到角色绑定的 Agent，按顺序逐个执行。'}</p>
          </div>
          <div class="dispatch-panel-state">
            {_phase_pill(phase)}
            {state_badge}
            <a class="text-link" href="/runs">运行记录</a>
          </div>
        </div>

        {summary_html}
        {live_html}

        <div class="dispatch-action-row">
          <form class="dispatch-action" method="post" action="/dispatch/{_esc(task.task_id)}"
            data-confirm="{_esc(confirm)}">
            <button class="button button--signal" id="dispatch-start" type="submit"{disabled_attr}>
              {_esc(button_label)}<span class="button-mark" aria-hidden="true">→</span>
            </button>
          </form>
          <p class="dispatch-hint">{_esc(hint)}</p>
        </div>

        <p class="dispatch-panel-foot">
          串行执行复用执行器<strong>单实例锁</strong>；cwd 为该任务的<strong>工作目录</strong>：
          <code>{_esc(str(task.workdir))}</code>。每个子任务完成后原子更新
          <code>对接/subtasks.yaml</code>（status / log / finished_at），日志写入
          <code>工作区/日志/dispatch-&lt;子任务&gt;-&lt;时间戳&gt;.log</code>。
          固定提示词要求产物写在工作目录，并禁止写 <code>对接/审查.md</code>、改其它子任务或删文件。
        </p>
      </section>
    """


def _render_review_panel(task: Task, phase: str) -> str:
    """全部子任务交付后的下一步：由 reviewer 角色复核并写审查结论。"""
    payload = _review_status_payload(task)
    phase = str(payload.get("phase") or phase)
    state_name = str(payload.get("state") or "idle")
    running = bool(payload.get("running"))
    can_start = bool(payload.get("can_start"))
    reason = str(payload.get("disabled_reason") or "")
    ready_reason = str(payload.get("ready_reason") or "")
    message = str(payload.get("message") or "")
    error = str(payload.get("error") or "")
    log_url = str(payload.get("log_url") or "")
    conclusion = str(payload.get("conclusion") or "")
    conclusion_label = str(payload.get("conclusion_label") or "")
    issues = payload.get("issues")
    if not isinstance(issues, list):
        issues = []
    review_done = bool(payload.get("review_done"))
    done = int(payload.get("done") or 0)
    total = int(payload.get("total") or 0)
    delivery_evidence = str(payload.get("delivery_evidence") or "")
    started_at = str(payload.get("started_at") or "")
    finished_at = str(payload.get("finished_at") or "")
    role_name = str(payload.get("review_role_name") or "")
    role_id = str(payload.get("review_role") or REVIEW_ROLE_ID)
    agent_id = str(payload.get("review_agent") or "")
    binding_error = str(payload.get("review_binding_error") or "")
    state_tone = REVIEW_STATE_TONES.get(state_name, "idle")
    state_badge = (
        f'<span class="run-pill run-pill--{_esc(state_tone)}">'
        f"{_esc(REVIEW_STATE_NAMES.get(state_name, state_name))}</span>"
        if state_name != "idle"
        else ""
    )
    dispatch_state = _dispatch_state_snapshot(task.task_id)
    dispatch_state_name = str(dispatch_state.get("state") or "")
    if dispatch_state_name in {"completed", "failed"}:
        dispatch_success = int(dispatch_state.get("success") or 0)
        dispatch_failed = int(dispatch_state.get("failed") or 0)
        dispatch_tone = "notice--ok" if dispatch_state_name == "completed" else "notice--error"
        dispatch_heading = "分发完成" if dispatch_state_name == "completed" else "分发已停止"
        dispatch_html = f"""
        <div class="notice {dispatch_tone} dispatch-summary review-dispatch-summary" role="status">
          <strong>{dispatch_heading}：成功 {dispatch_success} / 失败 {dispatch_failed}。</strong>
          <span>{_esc(str(dispatch_state.get("message") or ""))}</span>
        </div>
        """
    else:
        dispatch_html = ""
    log_link = (
        f'<a class="text-link" href="{_esc(log_url)}" target="_blank" rel="noopener">'
        "查看审核日志</a>"
        if log_url
        else '<span class="subtask-muted">暂无审核日志</span>'
    )

    if binding_error:
        reviewer_html = (
            '<div class="notice notice--error" role="alert">'
            f"<strong>审核角色未就绪。</strong> {_esc(binding_error)}。"
            f'到 <a class="text-link" href="/roles/{_esc(role_id)}">/roles/{_esc(role_id)}</a>'
            " 绑定一个已登记且可执行的 Agent。"
            "</div>"
        )
    else:
        reviewer_html = f"""
        <div class="review-binding">
          <p class="aside-kicker">REVIEWER / 审核角色</p>
          <p class="review-binding-line">
            <strong>{_esc(role_name or role_id)}</strong>
            <code>{_esc(role_id)}</code>
            <span aria-hidden="true">→</span>
            <code>{_esc(agent_id)}</code>
          </p>
          <p class="review-binding-note">固定提示词由值班台生成；只允许审核角色写
            <code>对接/审查.md</code>，不得改子任务产物，不删文件。</p>
        </div>
        """

    if state_name == "review_failed" and phase == PHASE_REVIEW_READY:
        failure_html = f"""
        <div class="notice notice--error review-failure" role="alert">
          <strong>上次审核未完成，可重试。</strong>
          <span>{_esc(error or message)}</span>
          <span class="review-failure-links">{log_link}</span>
        </div>
        """
    else:
        failure_html = ""

    if phase == PHASE_REVIEWING or running:
        title = "审核中…"
        description = (
            "审核角色正在按任务单验收标准逐条核对交付与证据。"
            "本页会自动轮询进度，审核结束后刷新结论。"
        )
        live_html = f"""
        <div class="dispatch-live" id="review-live" role="status" aria-live="polite">
          <p class="dispatch-live-title"><span class="dispatch-pulse" aria-hidden="true"></span>
            <span id="review-live-label">审核中…</span></p>
          <dl class="dispatch-live-meta">
            <div><dt>{'子任务' if total else '交付'}</dt>
              <dd><strong id="review-counter">{f'{done}/{total}' if total else '任务级'}</strong></dd></div>
            <div><dt>审核角色</dt>
              <dd><code>{_esc(role_id)}</code> · {_esc(agent_id or "—")}</dd></div>
            <div><dt>开始</dt><dd>{_esc(started_at or "—")}</dd></div>
          </dl>
          <p class="dispatch-live-note" id="review-live-note">{_esc(message or "正在复核验收标准与证据。")}</p>
          <p class="review-live-log">{log_link}</p>
        </div>
        """
        action_html = f"""
        <div class="dispatch-action-row">
          <button class="button button--signal" id="review-start" type="button" disabled>
            审核中…<span class="button-mark" aria-hidden="true">→</span>
          </button>
          <p class="dispatch-hint">审核复用执行器<strong>单实例锁</strong>；
            同一时刻只有一个 Agent 在运行。页面会自动刷新结论。</p>
        </div>
        """
    elif phase == PHASE_PASSED or phase == PHASE_REVISE:
        ok = conclusion == "passed" or phase == PHASE_PASSED
        title = "审核已通过" if ok else "审核结论：须修正"
        description = (
            "审核角色已完成复核，结论与证据保存在 <code>对接/审查.md</code>。"
            if ok
            else (
                "审核角色已完成复核，结论与证据保存在 <code>对接/审查.md</code>；"
                "可在「继续修正」面板重做受影响子任务并自动重新审核。"
            )
        )
        tone = "notice--ok" if ok else "notice--error"
        if issues:
            issue_html = (
                '<h3 class="review-subhead">问题清单摘要</h3>'
                '<ul class="issue-list">'
                + "".join(f"<li>{_esc(str(item))}</li>" for item in issues)
                + "</ul>"
            )
        else:
            issue_html = (
                '<p class="review-no-issues">审查档案中没有解析到问题清单条目。</p>'
            )
        done_html = (
            '<span class="run-pill run-pill--passed">审核完成信号已落盘</span>'
            if review_done
            else '<span class="run-pill run-pill--failed">缺少 审核完成.md</span>'
        )
        failure_note = ""
        if error:
            failure_note = (
                f'<div class="notice notice--warn review-failure" role="status">'
                f"<strong>执行器备注：</strong>{_esc(error)}</div>"
            )
        live_html = f"""
        {dispatch_html}
        <div class="notice {tone} review-conclusion" role="status">
          <strong>审核结论：{_esc(conclusion_label or ("通过" if ok else "须修正"))}</strong>
          <span>{_esc(message or "审查档案已落盘。")}</span>
        </div>
        {failure_note}
        {issue_html}
        <dl class="review-artifacts">
          <div><dt>审查档案</dt><dd><code>对接/审查.md</code> 已落盘</dd></div>
          <div><dt>完成信号</dt><dd>{done_html}</dd></div>
          <div><dt>审核日志</dt><dd>{log_link}</dd></div>
          <div><dt>结束时间</dt><dd>{_esc(finished_at or "—")}</dd></div>
        </dl>
        """
        action_html = ""
    else:
        title = "下一步：开始审核"
        if total:
            description = (
                "全部子任务已交付。审核角色会读取任务单、subtasks.yaml、每个子任务的"
                " DONE.md 与工作目录产物，按验收标准逐条核对并写入审查结论。"
            )
            ready_line = (
                f"<strong>子任务 {done}/{total} 已交付</strong>，"
                "没有待开始或失败的子任务。"
            )
        else:
            description = (
                "本任务没有拆分子任务。审核角色会直接读取任务单、任务级 DONE.md"
                " 与工作区产物，按验收标准逐条核对并写入审查结论。"
            )
            ready_line = (
                "<strong>任务级交付物已就绪</strong>，没有待分发子任务。"
            )
        live_html = f"""
        {dispatch_html}
        <div class="review-ready-note">
          <p>{ready_line}</p>
          <p><strong>交付判据：</strong>{_esc(delivery_evidence or "交付物已落盘")}。</p>
          <p>审核会启动 <code>{_esc(role_id)}</code> 角色绑定的 Agent，
            复用执行器单实例锁串行运行；不需要在本页选择 Agent。</p>
        </div>
        {reviewer_html}
        {failure_html}
        """
        button_label = "重新开始审核" if state_name == "review_failed" else "开始审核"
        disabled_attr = "" if can_start else " disabled"
        confirm = (
            f"确认开始审核 {task.task_id}？"
            f"系统会启动 {role_id} 角色绑定的 Agent，读取全部交付并写 对接/审查.md"
            " 与 对接/审核完成.md；同一时刻只允许一个 Agent 运行。"
        )
        hint = reason or ready_reason or "全部子任务已交付，将由审核角色复核验收标准与证据。"
        action_html = f"""
        <div class="dispatch-action-row">
          <form class="dispatch-action" method="post" action="/review/{_esc(task.task_id)}"
            data-confirm="{_esc(confirm)}">
            <button class="button button--signal" id="review-start" type="submit"{disabled_attr}>
              {_esc(button_label)}<span class="button-mark" aria-hidden="true">→</span>
            </button>
          </form>
          <p class="dispatch-hint">{_esc(hint)}</p>
        </div>
        """

    return f"""
      <section class="dispatch-panel review-panel" id="review-panel"
        aria-labelledby="review-panel-title"
        data-status-url="/api/task/{_esc(task.task_id)}/review-status"
        data-state="{'running' if running else 'idle'}"
        data-phase="{_esc(phase)}"
        data-poll-ms="{REVIEW_POLL_INTERVAL_MS}">
        <div class="dispatch-panel-head">
          <div>
            <p class="aside-kicker">NEXT STEP / 审核</p>
            <h2 id="review-panel-title">{_esc(title)}</h2>
            <p>{description}</p>
          </div>
          <div class="dispatch-panel-state">
            {_phase_pill(phase)}
            {state_badge}
            <a class="text-link" href="/runs">运行记录</a>
          </div>
        </div>
        {live_html}
        {action_html}
        <p class="dispatch-panel-foot">
          审核只写入 <code>对接/审查.md</code> 与 <code>对接/审核完成.md</code>；
          cwd 为任务项目目录 <code>{_esc(str(task.project_dir))}</code>（可同时访问
          <code>对接/</code> 与 <code>工作区/</code>），日志写
          <code>工作区/日志/review-&lt;时间戳&gt;.log</code>，运行状态写
          <code>协作/调度台/runner.status</code>（kind=review）。
          分发路径仍然禁止写 <code>审查.md</code>；只有审核角色可以写。
        </p>
      </section>
    """


def _render_advance_panel(task: Task, phase: str) -> str:
    """全自动推进入口与状态；到审核终态后由结论面板接管。"""
    if task.archived or phase in {PHASE_PASSED, PHASE_REVISE}:
        return ""
    payload = _advance_status_payload(task)
    state_name = str(payload.get("state") or "idle")
    running = bool(payload.get("running"))
    can_start = bool(payload.get("can_start"))
    reason = str(payload.get("disabled_reason") or "")
    ready_reason = str(payload.get("ready_reason") or "")
    message = str(payload.get("message") or "")
    error = str(payload.get("error") or "")
    step_label = str(payload.get("step_label") or "")
    started_at = str(payload.get("started_at") or "")
    finished_at = str(payload.get("finished_at") or "")
    auto = bool(payload.get("auto"))
    budget = payload.get("budget")
    if not isinstance(budget, dict):
        budget = {}
    budget_limits = budget.get("limits")
    if not isinstance(budget_limits, dict):
        budget_limits = {}
    budget_used = budget.get("used")
    if not isinstance(budget_used, dict):
        budget_used = {}
    hours = float(budget_limits.get("max_runtime_seconds") or 0) / 3600.0
    budget_line = (
        "预算：最长 "
        f"{hours:g} 小时 · 子任务 ≤{int(float(budget_limits.get('max_subtasks') or 0))} 个"
        f" · 修订 ≤{int(float(budget_limits.get('max_revise_rounds') or 0))} 轮"
        f"；已用 {float(budget_used.get('runtime_seconds') or 0):.0f} 秒"
        f"、{int(float(budget_used.get('subtasks') or 0))} 个子任务"
        f"、第 {int(float(budget_used.get('revise_round') or 0))} 轮"
    )
    events = payload.get("events")
    if not isinstance(events, list):
        events = []
    state_badge = (
        f'<span class="run-pill run-pill--{_esc(str(payload.get("state_tone") or "idle"))}">'
        f"{_esc(str(payload.get('state_label') or state_name))}</span>"
        if state_name != "idle"
        else ""
    )
    auto_badge = (
        '<span class="run-pill run-pill--running">发布后自动</span>' if auto else ""
    )
    recent_events = events[-4:]
    events_html = ""
    if recent_events:
        items = "".join(
            f"<li><span>{_esc(str(item.get('at') or ''))}</span>"
            f"{_esc(str(item.get('message') or ''))}</li>"
            for item in recent_events
            if isinstance(item, dict)
        )
        if items:
            events_html = f'<ul class="advance-events">{items}</ul>'

    if running:
        live_html = f"""
        <div class="dispatch-live" id="advance-live" role="status" aria-live="polite">
          <p class="dispatch-live-title"><span class="dispatch-pulse" aria-hidden="true"></span>
            <span id="advance-live-label">全自动推进中 · {_esc(step_label or "准备")}</span></p>
          <p class="dispatch-live-note" id="advance-live-message">{_esc(message or "正在推进…")}</p>
          {events_html}
          <p class="advance-time">开始 {_esc(started_at or "—")} · {_esc(budget_line)}</p>
        </div>
        """
        action_html = f"""
        <div class="dispatch-action-row">
          <form class="dispatch-action" method="post"
            action="/advance/{_esc(task.task_id)}/stop"
            data-confirm="确认停止全自动推进 {_esc(task.task_id)}？系统会终止当前 Agent 并释放执行器锁；停止后不会自动重新拉起。">
            <button class="button button--ghost" id="advance-stop" type="submit">
              停止推进<span class="button-mark" aria-hidden="true">■</span>
            </button>
          </form>
          <p class="dispatch-hint">停止由你显式触发；当前 Agent 会被终止，
            执行器锁会释放，之后不会自动重新拉起。页面会自动轮询，结束后刷新。</p>
        </div>
        """
    else:
        tone = "notice--ok" if state_name == "completed" else (
            "notice--warn"
            if state_name in {"stopped", "stopped_with_budget"}
            else ("notice--error" if state_name == "failed" else "")
        )
        summary = ""
        if state_name in {"completed", "stopped", "stopped_with_budget", "failed"}:
            heading = {
                "completed": "全自动推进完成",
                "stopped": "全自动推进已停止",
                "stopped_with_budget": "全自动推进已停止（预算用尽）",
                "failed": "全自动推进失败",
            }.get(state_name, "全自动推进")
            summary = f"""
            <div class="notice {tone} advance-summary" role="status">
              <strong>{heading}。</strong>
              <span>{_esc(error or message)}</span>
            </div>
            """
        disabled_attr = "" if can_start else " disabled"
        button_label = (
            "重新全自动推进"
            if state_name in {"failed", "stopped", "stopped_with_budget"}
            else "全自动推进"
        )
        confirm = (
            f"确认全自动推进 {task.task_id}？"
            "系统会继续分发 pending/failed 子任务，全部交付后自动启动审核；"
            "审核通过或须修正后停止。中途失败会停止并记录原因。"
        )
        live_html = f"""
        {summary}
        {events_html}
        <p class="advance-time">{_esc(budget_line)}</p>
        <div class="advance-copy">
          <p><strong>一条链跑到底：</strong>分发剩余 → 自动审核 → 停在「已通过 / 须修正」。</p>
          <p>不会重跑 delivered/passed 子任务；失败项会重置为 pending 后重试一次。
            任何 Agent 失败或审核未产出审查文件都会立即停止。</p>
          {f'<p class="advance-reason">{_esc(reason)}</p>' if reason else ''}
        </div>
        """
        action_html = f"""
        <div class="dispatch-action-row">
          <form class="dispatch-action" method="post" action="/advance/{_esc(task.task_id)}"
            data-confirm="{_esc(confirm)}">
            <button class="button button--signal" id="advance-start" type="submit"{disabled_attr}>
              {_esc(button_label)}<span class="button-mark" aria-hidden="true">↠</span>
            </button>
          </form>
          <p class="dispatch-hint">{_esc(reason or "全自动推进由系统串行调度，无需逐步点击。")}</p>
        </div>
        """

    return f"""
      <section class="dispatch-panel advance-panel" id="advance-panel"
        aria-labelledby="advance-panel-title"
        data-status-url="/api/task/{_esc(task.task_id)}/advance-status"
        data-state="{'running' if running else 'idle'}"
        data-poll-ms="{ADVANCE_POLL_INTERVAL_MS}">
        <div class="dispatch-panel-head">
          <div>
            <p class="aside-kicker">AUTOPILOT / 全自动推进</p>
            <h2 id="advance-panel-title">全自动推进</h2>
            <p>一次启动，自动把待开始/失败子任务分发完，再进入审核；到结论或失败即停。</p>
          </div>
          <div class="dispatch-panel-state">
            {_phase_pill(phase)}
            {state_badge}
            {auto_badge}
            <a class="text-link" href="/runs">运行记录</a>
          </div>
        </div>
        {live_html}
        {action_html}
        <p class="dispatch-panel-foot">
          推进器本身不启动额外进程；每个分发子任务和审核 Agent 仍由
          <code>console/runner.py</code> 抢同一把<strong>单实例锁</strong>串行执行。
          状态持久化在 <code>协作/调度台/advance-&lt;编号&gt;.status.json</code>，
          可轮询 <code>/api/task/&lt;编号&gt;/advance-status</code>。
          {f'结束时间 {_esc(finished_at)}' if finished_at else ''}
        </p>
      </section>
    """


def _render_revise_panel(task: Task, phase: str) -> str:
    """须修正阶段的继续修正面板：结论、问题清单、重做范围与轮次。"""
    payload = _revise_status_payload(task)
    running = bool(payload.get("running"))
    state_name = str(payload.get("state") or "idle")
    can_start = bool(payload.get("can_start"))
    reason = str(payload.get("disabled_reason") or "")
    ready_reason = str(payload.get("ready_reason") or "")
    message = str(payload.get("message") or "")
    error = str(payload.get("error") or "")
    log_url = str(payload.get("log_url") or "")
    issues = payload.get("issues")
    if not isinstance(issues, list):
        issues = []
    target_ids = payload.get("target_ids")
    if not isinstance(target_ids, list):
        target_ids = []
    passed_target_ids = payload.get("passed_target_ids")
    if not isinstance(passed_target_ids, list):
        passed_target_ids = []
    all_ids = payload.get("all_ids")
    if not isinstance(all_ids, list):
        all_ids = []
    has_subtasks = bool(payload.get("has_subtasks"))
    whole_rework = bool(payload.get("whole_rework"))
    owner_role = str(payload.get("owner_role") or DEFAULT_OWNER_ROLE)
    at_limit = bool(payload.get("at_limit"))
    round_no = int(payload.get("revise_round") or 0)
    next_round = int(payload.get("next_round") or 1)
    max_rounds = int(payload.get("max_rounds") or MAX_REVISE_ROUNDS)
    current_subtask = str(payload.get("current_subtask") or "")
    current_title = str(payload.get("current_title") or "")
    live_phase = str(payload.get("phase") or phase)
    log_link = (
        f'<a class="text-link" href="{_esc(log_url)}" target="_blank" rel="noopener">'
        "查看修正日志</a>"
        if log_url
        else ""
    )
    round_badge = (
        f'<span class="run-pill run-pill--revise">修正轮次 {round_no}/{max_rounds}</span>'
    )
    state_badge = (
        f'<span class="run-pill run-pill--{_esc(str(payload.get("state_tone") or "idle"))}">'
        f"{_esc(str(payload.get('state_label') or state_name))}</span>"
        if state_name != "idle" and not running
        else ""
    )

    if issues:
        issue_html = (
            '<h3 class="review-subhead">问题清单摘要</h3>'
            '<ul class="issue-list">'
            + "".join(f"<li>{_esc(str(item))}</li>" for item in issues)
            + "</ul>"
        )
    else:
        issue_html = (
            '<p class="review-no-issues">审查档案中没有解析到问题清单条目。</p>'
        )

    target_chips = "".join(
        f'<span class="run-pill run-pill--revise">{_esc(item)}</span>'
        for item in target_ids
    )
    if target_ids:
        passed_note = (
            f"其中 {'、'.join(passed_target_ids)} 已是「已通过」，默认不重跑；"
            "如需连同已通过项一起重做，可点「全部重做（含已通过）」。"
            if passed_target_ids
            else ""
        )
        target_html = f"""
        <h3 class="review-subhead">本轮将重做的子任务</h3>
        <p class="revise-targets">{target_chips}</p>
        <p class="revise-note">已从问题清单解析到子任务编号；未列出的子任务保持原状态，
          不会重跑。{_esc(passed_note)}旧审查会先复制到
          <code>对接/{_esc(REVIEW_HISTORY_DIRNAME)}/round-{next_round}.md</code>，
          原件不删除。</p>
        """
    else:
        choice_hint = (
            "请选择「全部子任务重做」或「只重新审核（不改产物）。"
            if has_subtasks
            else (
                f"本任务没有子任务，将回叫 owner_role={owner_role} 绑定的 Agent "
                "整单重修；旧审查会移入 对接/审查历史/。"
            )
        )
        target_html = f"""
        <div class="notice notice--warn revise-choice-note" role="status">
          <strong>未识别到具体子任务。</strong>
          <span>问题清单里没有出现可识别的子任务编号（Txx-n）。
            {_esc(choice_hint)}</span>
        </div>
        """

    if running:
        title = f"修正中…（第 {round_no} 轮）"
        description = (
            "正在回叫 owner_role 重做整单，写完新 DONE.md 后自动重新审核；"
            "本页会自动刷新最新结论。"
            if whole_rework
            else (
                "正在按修正计划重新分发受影响子任务，全部交付后自动重新审核；"
                "本页会自动刷新最新结论。"
            )
        )
        live_html = f"""
        <div class="dispatch-live" id="revise-live" role="status" aria-live="polite">
          <p class="dispatch-live-title"><span class="dispatch-pulse" aria-hidden="true"></span>
            <span id="revise-live-label">修正中…</span></p>
          <dl class="dispatch-live-meta">
            <div><dt>当前子任务</dt>
              <dd><code id="revise-current">{_esc(current_subtask or '准备中')}</code>
              <span id="revise-current-title">{_esc(current_title)}</span></dd></div>
            <div><dt>修正轮次</dt>
              <dd><strong>第 {round_no}/{max_rounds} 轮</strong></dd></div>
          </dl>
          <p class="dispatch-live-note" id="revise-live-note">{_esc(message or '正在重新分发并自动审核。')}</p>
          <p class="review-live-log">{log_link}</p>
        </div>
        """
        action_html = f"""
        <div class="dispatch-action-row">
          <button class="button button--signal" id="revise-start" type="button" disabled>
            修正中…<span class="button-mark" aria-hidden="true">→</span>
          </button>
          <p class="dispatch-hint">继续修正复用执行器<strong>单实例锁</strong>；
            同一时刻只有一个 Agent 在运行。完成后页面自动刷新结论。</p>
        </div>
        """
        issue_html = ""
        target_html = ""
    else:
        title = (
            f"下一步：继续修正（第 {next_round} 轮）"
            if not at_limit
            else "已达修正轮次上限"
        )
        description = (
            f"审核结论为「须修正」。本任务没有子任务：将把旧审查移入审查历史，"
            f"回叫 owner_role={owner_role} 绑定的 Agent 整单重修，"
            "完成后自动重新审核。"
            if whole_rework
            else (
                "审核结论为「须修正」。系统会只重置问题清单涉及的子任务，"
                "然后自动分发并重新审核；未受影响的子任务不会重跑。"
            )
        )
        last_result_html = ""
        if state_name == "failed":
            last_result_html = f"""
            <div class="notice notice--error revise-last-result" role="alert">
              <strong>上次继续修正失败。</strong>
              <span>{_esc(error or message)}</span>
              <span class="review-failure-links">{log_link}</span>
            </div>
            """
        elif state_name == "stopped":
            last_result_html = f"""
            <div class="notice notice--warn revise-last-result" role="status">
              <strong>上一轮修正后，审核结论仍为「须修正」。</strong>
              <span>{_esc(message)}</span>
              <span class="review-failure-links">{log_link}</span>
            </div>
            """
        limit_html = ""
        if at_limit:
            limit_html = f"""
            <div class="notice notice--error revise-limit" role="alert">
              <strong>已达 {max_rounds} 轮修正上限，请人工介入。</strong>
              <span>按钮已禁用；请人工检查产物与审查意见后再决定下一步。</span>
            </div>
            """
        disabled_attr = "" if can_start else " disabled"
        round_archive = f"round-{next_round}.md"
        if target_ids:
            target_list = "、".join(target_ids)
            confirm_affected = (
                f"确认继续修正 {task.task_id}（第 {next_round} 轮）？"
                f"将把 {target_list} 中仍为已交付的子任务重置为待开始"
                + (
                    f"（已通过项 {('、'.join(passed_target_ids))} 不会重跑）"
                    if passed_target_ids
                    else ""
                )
                + f"，复制旧审查到 对接/{REVIEW_HISTORY_DIRNAME}/{round_archive}，"
                "然后自动分发并重新审核；未受影响的子任务不会重跑。"
                "同一时刻只允许一个 Agent 运行。"
            )
            confirm_all = (
                f"确认全部重做 {task.task_id}（第 {next_round} 轮）？"
                f"将把全部 {len(all_ids)} 个子任务（含已通过项）重置为待开始，"
                f"复制旧审查到 对接/{REVIEW_HISTORY_DIRNAME}/{round_archive}，"
                "然后自动分发并重新审核。同一时刻只允许一个 Agent 运行。"
            )
            all_override = (
                f"""
              <form class="dispatch-action" method="post"
                action="/revise/{_esc(task.task_id)}"
                data-confirm="{_esc(confirm_all)}">
                <input type="hidden" name="mode" value="{REVISE_MODE_ALL}">
                <button class="button button--ghost" id="revise-all" type="submit"{disabled_attr}>
                  全部重做（含已通过）
                </button>
              </form>
                """
                if passed_target_ids
                else ""
            )
            action_html = f"""
            <div class="dispatch-action-row">
              <form class="dispatch-action" method="post"
                action="/revise/{_esc(task.task_id)}"
                data-confirm="{_esc(confirm_affected)}">
                <input type="hidden" name="mode" value="{REVISE_MODE_AFFECTED}">
                <button class="button button--signal" id="revise-start" type="submit"{disabled_attr}>
                  {_esc(f'继续修正（第 {next_round} 轮）' if not at_limit else '已达修正上限')}<span class="button-mark" aria-hidden="true">→</span>
                </button>
              </form>
              {all_override}
              <p class="dispatch-hint">{_esc(reason or ready_reason or '只重做问题清单涉及的子任务。')}</p>
            </div>
            """
        else:
            confirm_all = (
                f"确认全部重做 {task.task_id}（第 {next_round} 轮）？"
                f"将把全部 {len(all_ids)} 个子任务重置为待开始，"
                f"复制旧审查到 对接/{REVIEW_HISTORY_DIRNAME}/{round_archive}，"
                "然后自动分发并重新审核。同一时刻只允许一个 Agent 运行。"
            )
            confirm_whole = (
                f"确认整单重修 {task.task_id}（第 {next_round} 轮）？"
                "该任务没有子任务：将把旧审查移入 "
                f"对接/{REVIEW_HISTORY_DIRNAME}/{round_archive}，"
                f"回叫 owner_role={owner_role} 绑定的 Agent 重做整单，"
                "写新 DONE.md 后自动重新审核。同一时刻只允许一个 Agent 运行。"
            )
            confirm_review = (
                f"确认只重新审核 {task.task_id}（第 {next_round} 轮）？"
                "不会修改任何子任务产物；只复制旧审查并重新启动审核。"
            )
            all_disabled = disabled_attr if has_subtasks else " disabled"
            primary_form = (
                f'''<form class="dispatch-action" method="post"
                action="/revise/{_esc(task.task_id)}"
                data-confirm="{_esc(confirm_whole)}">
                <input type="hidden" name="mode" value="{REVISE_MODE_WHOLE}">
                <button class="button button--signal" id="revise-start" type="submit"{disabled_attr}>
                  {_esc(f'整单重修（第 {next_round} 轮）' if not at_limit else '已达修正上限')}<span class="button-mark" aria-hidden="true">→</span>
                </button>
              </form>'''
                if whole_rework
                else f'''<form class="dispatch-action" method="post"
                action="/revise/{_esc(task.task_id)}"
                data-confirm="{_esc(confirm_all)}">
                <input type="hidden" name="mode" value="{REVISE_MODE_ALL}">
                <button class="button button--signal" id="revise-start" type="submit"{all_disabled}>
                  全部子任务重做<span class="button-mark" aria-hidden="true">→</span>
                </button>
              </form>'''
            )
            action_html = f"""
            <div class="revise-choice-row">
              {primary_form}
              <form class="dispatch-action" method="post"
                action="/revise/{_esc(task.task_id)}"
                data-confirm="{_esc(confirm_review)}">
                <input type="hidden" name="mode" value="{REVISE_MODE_REVIEW}">
                <button class="button button--ghost" id="revise-review-only" type="submit"{disabled_attr}>
                  只重新审核（不改产物）
                </button>
              </form>
            </div>
            <p class="dispatch-hint">{_esc(reason or ready_reason or '不会修改子任务产物。')}</p>
            """
        live_html = f"""
        {last_result_html}
        {limit_html}
        <div class="notice notice--error review-conclusion" role="status">
          <strong>审核结论：须修正</strong>
          <span>结论与证据保存在 <code>对接/{REVIEW_NAME}</code>；修正完成后由审核角色重新复核。</span>
        </div>
        {issue_html}
        {target_html}
        {action_html}
        """

    return f"""
      <section class="dispatch-panel revise-panel" id="revise-panel"
        aria-labelledby="revise-panel-title"
        data-status-url="/api/task/{_esc(task.task_id)}/revise-status"
        data-state="{'running' if running else 'idle'}"
        data-phase="{_esc(live_phase)}"
        data-poll-ms="{REVISE_POLL_INTERVAL_MS}">
        <div class="dispatch-panel-head">
          <div>
            <p class="aside-kicker">NEXT STEP / 继续修正</p>
            <h2 id="revise-panel-title">{_esc(title)}</h2>
            <p>{_esc(description)}</p>
          </div>
          <div class="dispatch-panel-state">
            {_phase_pill(live_phase)}
            {round_badge}
            {state_badge}
            <a class="text-link" href="/runs">运行记录</a>
          </div>
        </div>
        {live_html}
        <p class="dispatch-panel-foot">
          继续修正复用执行器<strong>单实例锁</strong>；
          {'整单重修会把旧审查移入' if whole_rework else '旧审查只复制到'}
          <code>对接/{_esc(REVIEW_HISTORY_DIRNAME)}/</code>，不删除文件。
          {'owner_role 绑定的 Agent 重做整单后必须写新 DONE.md，再看 G2/G3。' if whole_rework else '重置只作用于问题清单涉及（或你选择全部）的子任务；未受影响的已交付子任务不重跑。'}
          控制台不写 <code>对接/{REVIEW_NAME}</code>，新结论仍只由审核角色写入。
        </p>
      </section>
    """


def _handoff_probe_stage(task: Task, phase: str) -> Tuple[str, int]:
    if phase in {PHASE_REVIEW_READY, PHASE_REVIEWING}:
        return "review", 0
    terminal, _verdict = _review_terminal_state(task)
    if terminal:
        return "review_complete", task.revise_round
    return "dispatch", 0


def _render_handoff_panel(task: Task, phase: str) -> str:
    """详情页交接闸门清单：✓/✗、责任方、缺件与重试入口。"""
    state = _handoff_state_snapshot(task.task_id)
    state_name = str(state.get("state") or "")
    running = state_name in {"checking", "supplementing"}
    blocked = state_name == "blocked"
    stored_checks = state.get("checks")
    if blocked or (running and isinstance(stored_checks, list)):
        checks = list(stored_checks) if isinstance(stored_checks, list) else []
        stage = str(state.get("stage") or "")
        heading_note = "最近一次交接体检"
    else:
        stage, round_no = _handoff_probe_stage(task, phase)
        try:
            checks = _handoff_checks_for(task, stage, round_no)
        except Exception:
            checks = []
        heading_note = "当前下一步的实时预检"
    if not checks:
        return ""

    rows: List[str] = []
    for check in checks:
        passed = bool(check.get("passed"))
        missing = check.get("missing")
        if not isinstance(missing, list):
            missing = []
        subtask = str(check.get("subtask") or "")
        title = (
            f"{check.get('gate')} · {check.get('name')}"
            + (f" · {subtask}" if subtask else "")
        )
        missing_html = ""
        if missing:
            missing_html = (
                '<ul class="handoff-missing">'
                + "".join(f"<li>{_esc(item)}</li>" for item in missing)
                + "</ul>"
            )
        rows.append(
            f"""
            <li class="handoff-check {'is-pass' if passed else 'is-fail'}">
              <span class="handoff-mark" aria-hidden="true">{'✓' if passed else '✗'}</span>
              <div class="handoff-check-body">
                <div class="handoff-check-head">
                  <strong>{_esc(title)}</strong>
                  <span class="handoff-role">责任方 · {_esc(check.get('owner_role') or '—')}</span>
                </div>
                <p>{_esc(check.get('detail') or ('通过' if passed else '缺少必要文件'))}</p>
                {missing_html}
              </div>
            </li>
            """
        )

    attempts = state.get("attempts")
    attempt_values = []
    if isinstance(attempts, dict):
        attempt_values = [int(value) for value in attempts.values()]
    attempt_total = int(state.get("attempt_total") or 0)
    runs = state.get("agent_runs")
    if not isinstance(runs, list):
        runs = []
    agent_names = sorted(
        {
            str(run.get("agent") or "")
            for run in runs
            if isinstance(run, dict) and run.get("agent")
        }
    )
    log = str(state.get("log") or "")
    log_link = (
        f'<a class="text-link" href="/log?path={quote(log, safe="")}">查看交接日志</a>'
        if log
        else ""
    )
    state_label = HANDOFF_STATE_NAMES.get(state_name, "实时预检")
    state_tone = HANDOFF_STATE_TONES.get(state_name, "idle")
    meta_bits = [f"阶段：{HANDOFF_STAGE_LABELS.get(stage, stage or '—')}"]
    if attempt_total:
        meta_bits.append(
            f"补交 {attempt_total} 次"
            + (f"（单闸门最多 {max(attempt_values)} 次）" if attempt_values else "")
        )
    if agent_names:
        meta_bits.append("回叫 Agent：" + "、".join(agent_names))
    if log_link:
        meta_bits.append(log_link)
    meta_html = " · ".join(meta_bits)

    action_html = ""
    if blocked:
        action_html = f"""
        <div class="notice notice--error handoff-blocked" role="alert">
          <strong>需要人工处理：交接体检未通过。</strong>
          <span>已停止推进；缺件见上方清单。补齐后可重试体检（每次仍最多自动补交 2 次）。</span>
          <span class="handoff-log">{log_link}</span>
        </div>
        """
        if not task.archived:
            action_html += f"""
            <form class="handoff-retry-form" method="post"
              action="/handoff/retry/{_esc(task.task_id)}">
              <button class="button button--signal" id="handoff-retry" type="submit">
                重试体检<span class="button-mark" aria-hidden="true">↻</span>
              </button>
            </form>
            """
    elif running:
        action_html = f"""
        <div class="notice notice--warn" role="status">
          <strong>{_esc(state_label)}。</strong>
          <span>{_esc(state.get('message') or '正在检查或缺件，自动补交与推进串行执行。')}</span>
        </div>
        """

    return f"""
      <section class="dispatch-panel handoff-panel" id="handoff-panel"
        data-handoff-state="{_esc(state_name or 'probe')}"
        data-status-url="/api/task/{_esc(task.task_id)}/handoff-status">
        <div class="dispatch-panel-head">
          <div>
            <p class="aside-kicker">HANDOFF GATES / 交接闸门</p>
            <h2>必要文件体检</h2>
            <p>{_esc(heading_note)}；全部通过才放行下一步。缺件回叫责任 Agent，
              每道闸门最多自动补交 {HANDOFF_MAX_SUPPLEMENTS} 次。</p>
          </div>
          <div class="dispatch-panel-state">
            <span class="run-pill run-pill--{_esc(state_tone)}">
              {_esc(state_label)}
            </span>
            <span class="task-subtasks">G1–G4</span>
          </div>
        </div>
        <ul class="handoff-checks">
          {''.join(rows)}
        </ul>
        <p class="handoff-meta">{meta_html}</p>
        {action_html}
        <p class="dispatch-panel-foot">
          补交与分发/审核共用 <code>runner.lock</code>，不会并发写；日志在
          <code>工作区/日志/handoff-*.log</code>。控制台只检查文件，不代替
          非 reviewer 角色写 <code>对接/{_esc(REVIEW_NAME)}</code>。
        </p>
      </section>
    """


def _render_workflow_panel(task: Task, phase: str) -> str:
    if task.archived:
        return ""
    if _revise_active_for(task.task_id) or phase == PHASE_REVISE:
        return _render_revise_panel(task, phase)
    advance_panel = _render_advance_panel(task, phase)
    if phase in {PHASE_REVIEW_READY, PHASE_REVIEWING, PHASE_PASSED}:
        return advance_panel + _render_review_panel(task, phase)
    return advance_panel + _render_dispatch_panel(task, phase)


def _render_page(title: str, active: str, content: str) -> str:
    nav_items = (
        ("board", "/", "BOARD", "任务看板"),
        ("new", "/new", "NEW", "发布任务"),
        ("archive", "/archive", "ARCHIVE", "归档库"),
        ("roles", "/roles", "ROLES", "角色"),
        ("agents", "/agents", "AGENTS", "Agent 应用"),
        ("runs", "/runs", "RUNS", "运行记录"),
        ("dispatch", "/dispatch", "DISPATCH", "调度台"),
    )
    nav_html = []
    for key, href, code, label in nav_items:
        current = ' aria-current="page"' if key == active else ""
        active_class = " is-active" if key == active else ""
        nav_html.append(
            f'<a class="nav-link{active_class}" href="{href}"{current}>'
            f'<span class="nav-code">{code}</span>'
            f"<span>{label}</span>"
            "</a>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="theme-color" content="#16211b">
  <title>{_esc(title)} · 协作值班台</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <a class="skip-link" href="#main">跳到主要内容</a>
  <div class="shell">
    <aside class="rail">
      <a class="brand" href="/" aria-label="协作值班台首页">
        <span class="brand-rings" aria-hidden="true"><i></i><i></i><i></i></span>
        <span class="brand-words">
          <strong>协作值班台</strong>
          <small>LOCAL AGENT DESK</small>
        </span>
      </a>
      <p class="rail-label">工作区 / WORKSPACE</p>
      <nav class="rail-nav" aria-label="主导航">
        {''.join(nav_html)}
      </nav>
      <div class="rail-foot">
        <p class="rail-local"><span class="local-dot" aria-hidden="true"></span>本机运行</p>
        <p class="rail-address">{HOST}:{PORT}</p>
        <p class="rail-note">执行器单实例 · 运行记录<br>任务单 / 归档 / 角色与 Agent</p>
        <p class="rail-roles">AI-A 审查 <span>/</span> AI-B 执行 <span>/</span> HUMAN 决策</p>
      </div>
    </aside>
    <main id="main" class="main">
      <div class="page">
        {content}
        <footer class="page-foot">
          <span>协作控制系统 / 本地控制台</span>
          <span>文件每次刷新实时读取</span>
        </footer>
      </div>
    </main>
  </div>
  <script src="/static/confirm.js" defer></script>
  <script src="/static/new-task.js" defer></script>
  <script src="/static/registering.js" defer></script>
  <script src="/static/dispatch.js" defer></script>
  <script src="/static/review.js" defer></script>
  <script src="/static/advance.js" defer></script>
  <script src="/static/revise.js" defer></script>
</body>
</html>
"""


def _render_error(code: int, title: str, message: str, hint: str = "") -> str:
    hint_html = f'<p class="error-hint">{_esc(hint)}</p>' if hint else ""
    content = f"""
      <section class="error-page">
        <p class="error-code">{code} / INTERRUPTED</p>
        <h1>{_esc(title)}</h1>
        <p class="error-message">{_esc(message)}</p>
        {hint_html}
        <div class="error-actions">
          <a class="button button--primary" href="/">返回任务看板</a>
          <a class="text-link" href="/dispatch">查看调度台</a>
        </div>
      </section>
    """
    return _render_page(title, "board", content)


def _summary_counts(tasks: List[Task]) -> Dict[str, int]:
    counts = {status: 0 for status in STATUS_TONES}
    for task in tasks:
        counts[task.status] += 1
    return counts


def _render_dashboard(tasks: List[Task]) -> str:
    counts = _summary_counts(tasks)
    total = len(tasks)

    tally = []
    for status, tone in STATUS_DISPLAY_ORDER:
        tally.append(
            f"""
            <div class="status-index-item">
              <dt><span class="status-dot dot--{tone}" aria-hidden="true"></span>{_esc(status)}</dt>
              <dd>{counts[status]:02d}</dd>
            </div>
            """
        )

    if tasks:
        rows = []
        for task in tasks:
            tone = _tone(task.status)
            phase = _task_phase(task)
            subtask_html = ""
            if task.subtasks:
                subtask_html = (
                    '<span class="task-subtasks">子任务 '
                    f"{task.subtasks_done}/{len(task.subtasks)}</span>"
                )
            phase_html = (
                f'<span class="run-pill run-pill--{_esc(_phase_tone(phase))}">'
                f"{_esc(_phase_label(phase))}</span>"
            )
            rows.append(
                f"""
                <li class="task-item" data-status="{tone}" data-phase="{_esc(phase)}">
                  <a class="task-row" href="/task/{_esc(task.task_id)}">
                    <span class="task-id">
                      <span class="task-id-code">{_esc(task.task_id)}</span>
                      <span class="task-id-label">TASK</span>
                    </span>
                    <span class="task-main">
                      <span class="task-title">{_esc(task.title)}</span>
                      <span class="task-meta">修改于
                        <time datetime="{_esc(_iso_time(task.mtime))}">{_esc(_fmt_time(task.mtime))}</time>
                      </span>
                    </span>
                    <span class="task-state">
                      {_stamp(task.status)}
                      {phase_html}
                      <span class="task-rounds">轮次 {task.rounds:02d}</span>
                      {subtask_html}
                    </span>
                    <span class="task-enter" aria-hidden="true">↗</span>
                  </a>
                  <div class="task-tools">
                    {_archive_form(task, button_class="task-move")}
                  </div>
                </li>
                """
            )
        tasks_html = f"""
        <section class="task-section" aria-labelledby="register-title">
          <div class="section-heading">
            <h2 class="section-title" id="register-title">任务登记簿</h2>
            <span class="section-meta">按编号倒序 · {total:02d} 张</span>
          </div>
          <ol class="task-register">
            {''.join(rows)}
          </ol>
        </section>
        """
    else:
        tasks_html = """
        <section class="empty-state" aria-labelledby="empty-title">
          <p class="empty-kicker">REGISTER EMPTY</p>
          <h2 id="empty-title">登记簿还是空的</h2>
          <p>写下第一张任务单，协作流程就从这里开始。</p>
          <a class="button button--primary" href="/new">新建第一张任务单</a>
        </section>
        """

    content = f"""
      <header class="page-head">
        <div class="page-title-group">
          <p class="kicker">LOCAL CONTROL <span aria-hidden="true">/</span> TASK REGISTER</p>
          <h1>任务交接台</h1>
          <p class="lede">每张任务单都在独立项目目录内，沿 <code>对接/</code> 查看交付与审查，沿 <code>工作区/</code> 查看实际产物。归档只会整体移动到 <code>归档/</code>，不删除任何文件。</p>
        </div>
        <div class="head-actions">
          <a class="button button--ghost" href="/roles">角色</a>
          <a class="button button--ghost" href="/archive">归档库</a>
          <a class="button button--primary" href="/new">
            新建任务单<span class="button-mark" aria-hidden="true">＋</span>
          </a>
        </div>
      </header>

      <section class="summary-panel" aria-label="任务概况">
        <div class="summary-total">
          <span class="summary-kicker">REGISTERED / 登记总数</span>
          <strong class="summary-number">{total:02d}</strong>
          <span class="summary-unit">张任务单</span>
        </div>
        <dl class="status-index">
          {''.join(tally)}
        </dl>
      </section>

      {tasks_html}
    """
    return _render_page("任务看板", "board", content)


def _render_archive_page() -> str:
    tasks = _load_archived_tasks()
    total = len(tasks)

    if tasks:
        rows = []
        for task in tasks:
            tone = _tone(task.status)
            rows.append(
                f"""
                <li class="task-item archive-item" data-status="{tone}">
                  <a class="task-row" href="/task/{_esc(task.task_id)}">
                    <span class="task-id">
                      <span class="task-id-code">{_esc(task.task_id)}</span>
                      <span class="task-id-label">ARCHIVED</span>
                    </span>
                    <span class="task-main">
                      <span class="task-title">{_esc(task.title)}</span>
                      <span class="task-meta task-meta--archive">归档位置 {_esc(_display_path(task.project_dir))} · 任务单修改于
                        <time datetime="{_esc(_iso_time(task.mtime))}">{_esc(_fmt_time(task.mtime))}</time>
                      </span>
                    </span>
                    <span class="task-state">
                      {_stamp(task.status)}
                      <span class="task-rounds">轮次 {task.rounds:02d}</span>
                    </span>
                    <span class="task-enter" aria-hidden="true">↗</span>
                  </a>
                  <div class="task-tools">
                    {_restore_form(task, button_class="task-move task-move--restore")}
                  </div>
                </li>
                """
            )
        archive_html = f"""
        <section class="task-section" aria-labelledby="archive-register-title">
          <div class="section-heading">
            <h2 class="section-title" id="archive-register-title">归档登记簿</h2>
            <span class="section-meta">按编号倒序 · {total:02d} 个</span>
          </div>
          <ol class="task-register">
            {''.join(rows)}
          </ol>
        </section>
        """
    else:
        archive_html = """
        <section class="empty-state" aria-labelledby="archive-empty-title">
          <p class="empty-kicker">ARCHIVE EMPTY</p>
          <h2 id="archive-empty-title">归档库还是空的</h2>
          <p>在看板上归档任务后，整个项目目录会出现在这里；归档项目仍可只读打开。</p>
          <a class="button button--ghost" href="/">返回任务看板</a>
        </section>
        """

    content = f"""
      <header class="page-head">
        <div class="page-title-group">
          <p class="kicker">ARCHIVE <span aria-hidden="true">/</span> READ-ONLY REGISTER</p>
          <h1>归档库</h1>
          <p class="lede">归档只把 <code>项目/…</code> 整体移动到 <code>归档/…</code>，不删除、不改写任何文件。归档项目仍可只读打开详情；恢复会把整个目录移回 <code>项目/</code>。</p>
        </div>
        <div class="head-actions">
          <a class="button button--ghost" href="/">返回看板</a>
        </div>
      </header>

      <section class="summary-panel summary-panel--archive" aria-label="归档概况">
        <div class="summary-total">
          <span class="summary-kicker">ARCHIVED / 已归档</span>
          <strong class="summary-number">{total:02d}</strong>
          <span class="summary-unit">个项目目录</span>
        </div>
        <div class="archive-scope">
          <p class="aside-kicker">MOVE ONLY / 只移动</p>
          <p class="archive-scope-title">旧版永不删除</p>
          <p>归档与恢复都只整体移动项目目录；不删除文件，也不改写 <code>对接/交付/</code>、<code>对接/审查.md</code> 或 <code>工作区/</code>。</p>
        </div>
      </section>

      {archive_html}
    """
    return _render_page("归档库", "archive", content)


def _render_source(text: str) -> str:
    return (
        '<details class="doc-source">'
        "<summary>查看 Markdown 原文</summary>"
        f"<pre>{_esc(text)}</pre>"
        "</details>"
    )


def _render_document(
    anchor: str,
    index: str,
    title: str,
    path_label: str,
    text: str,
    mtime: float,
) -> str:
    rendered = render_markdown(text) or '<p class="empty-file">（文件为空）</p>'
    return f"""
      <section class="doc-section" id="{anchor}" aria-labelledby="{anchor}-title">
        <header class="doc-head">
          <span class="doc-index" aria-hidden="true">{index}</span>
          <div class="doc-heading">
            <h2 id="{anchor}-title">{_esc(title)}</h2>
            <code class="doc-path">{_esc(path_label)}</code>
          </div>
          <time class="doc-time" datetime="{_esc(_iso_time(mtime))}">{_esc(_fmt_time(mtime))}</time>
        </header>
        <div class="doc-paper">
          <div class="prose">{rendered}</div>
          {_render_source(text)}
        </div>
      </section>
    """


def _render_missing_document(anchor: str, index: str, title: str, message: str) -> str:
    return f"""
      <section class="doc-section" id="{anchor}" aria-labelledby="{anchor}-title">
        <header class="doc-head">
          <span class="doc-index" aria-hidden="true">{index}</span>
          <div class="doc-heading">
            <h2 id="{anchor}-title">{_esc(title)}</h2>
          </div>
          <span class="doc-absent">未出现</span>
        </header>
        <div class="doc-empty">
          <span class="doc-empty-mark" aria-hidden="true">—</span>
          <p>{_esc(message)}</p>
        </div>
      </section>
    """


def _docking_file_kind(task: Task, entry: FileEntry) -> str:
    if entry.path == task.path:
        return "任务单"
    if entry.path == task.meta_path:
        return "任务元数据"
    if entry.path == task.idea_path:
        return "人类想法"
    if entry.path == task.subtasks_path:
        return "子任务"
    if entry.path.name == REGISTER_DONE_NAME:
        return "登记完成"
    if task.review_path is not None and entry.path == task.review_path:
        return "审查意见"
    if task.delivery is not None and entry.path == task.delivery.done_path:
        return "最新交付"
    if entry.relative.startswith(f"{DELIVERY_DIRNAME}/"):
        return "交付档案"
    return "对接附件"


def _render_file_preview(entry: FileEntry) -> str:
    if entry.size > MAX_FILE_PREVIEW_BYTES or entry.path.suffix.lower() not in TEXT_FILE_SUFFIXES:
        return ""
    try:
        data = entry.path.read_bytes()
    except OSError:
        return ""
    if len(data) > MAX_FILE_PREVIEW_BYTES:
        return ""
    text = data.decode("utf-8", errors="replace")
    if entry.path.suffix.lower() in {".md", ".markdown"}:
        preview = render_markdown(text) or '<p class="empty-file">（文件为空）</p>'
        return (
            '<details class="file-preview">'
            "<summary>展开预览</summary>"
            f'<div class="file-preview-body prose">{preview}</div>'
            "</details>"
        )
    body = html.escape(text, quote=False) or "（文件为空）"
    return (
        '<details class="file-preview">'
        "<summary>展开原文</summary>"
        f'<pre class="file-preview-source">{body}</pre>'
        "</details>"
    )


def _render_file_inventory(
    entries: List[FileEntry],
    *,
    empty_message: str,
    kind_for: Optional[Callable[[FileEntry], str]] = None,
    preview: bool = False,
) -> str:
    if not entries:
        return (
            '<div class="doc-empty">'
            '<span class="doc-empty-mark" aria-hidden="true">—</span>'
            f"<p>{_esc(empty_message)}</p>"
            "</div>"
        )

    rows: List[str] = []
    for entry in entries:
        suffix = entry.path.suffix.lstrip(".").upper() or "FILE"
        kind_html = ""
        if callable(kind_for):
            kind_html = f'<span class="file-kind">{_esc(kind_for(entry))}</span>'
        preview_html = _render_file_preview(entry) if preview else ""
        rows.append(
            """
            <li class="file-item">
              <div class="file-row">
                <span class="file-ext" aria-hidden="true">{suffix}</span>
                <div class="file-main">
                  <code class="file-path">{path}</code>
                  {kind}
                </div>
                <span class="file-size">{size}</span>
                <time class="file-time" datetime="{iso}">{shown}</time>
              </div>
              {preview}
            </li>
            """.format(
                suffix=_esc(suffix[:6]),
                path=_esc(entry.relative),
                kind=kind_html,
                size=_esc(_fmt_size(entry.size)),
                iso=_esc(_iso_time(entry.mtime)),
                shown=_esc(_fmt_time(entry.mtime)),
                preview=preview_html,
            )
        )
    return f'<ol class="file-list">{"".join(rows)}</ol>'


def _render_directory_section(
    anchor: str,
    index: str,
    title: str,
    path_label: str,
    entries: List[FileEntry],
    *,
    empty_message: str,
    kind_for: Optional[Callable[[FileEntry], str]] = None,
    preview: bool = False,
) -> str:
    return f"""
      <section class="doc-section" id="{anchor}" aria-labelledby="{anchor}-title">
        <header class="doc-head">
          <span class="doc-index" aria-hidden="true">{index}</span>
          <div class="doc-heading">
            <h2 id="{anchor}-title">{_esc(title)}</h2>
            <code class="doc-path">{_esc(path_label)}</code>
          </div>
          <span class="doc-absent">{len(entries):02d} 个文件</span>
        </header>
        {_render_file_inventory(entries, empty_message=empty_message, kind_for=kind_for, preview=preview)}
      </section>
    """


def _render_subtasks_section(task: Task, index: str = "03") -> str:
    if not task.subtasks_path.is_file():
        return _render_missing_document(
            "doc-subtasks",
            index,
            "子任务",
            "对接/subtasks.yaml 还没有出现。本期只展示总控拆出的子任务，不执行子任务。",
        )
    if task.subtasks_error:
        return f"""
      <section class="doc-section" id="doc-subtasks" aria-labelledby="doc-subtasks-title">
        <header class="doc-head">
          <span class="doc-index" aria-hidden="true">{index}</span>
          <div class="doc-heading">
            <h2 id="doc-subtasks-title">子任务</h2>
            <code class="doc-path">{_esc(_display_path(task.subtasks_path))}</code>
          </div>
          <span class="doc-absent">无法解析</span>
        </header>
        <div class="doc-empty">
          <span class="doc-empty-mark" aria-hidden="true">!</span>
          <p>{_esc(task.subtasks_error)}</p>
        </div>
      </section>
    """

    if not task.subtasks:
        return _render_missing_document(
            "doc-subtasks",
            index,
            "子任务",
            "subtasks.yaml 还没有列出子任务。总控登记完成前可稍后刷新。",
        )

    rows: List[str] = []
    for item in task.subtasks:
        tone = SUBTASK_STATUS_TONES.get(item.status, "pending")
        label = SUBTASK_STATUS_LABELS.get(item.status, item.status)
        role_html = (
            f'<span class="subtask-role">角色 · {_esc(item.agent_role)}</span>'
            if item.agent_role
            else '<span class="subtask-muted">未指定</span>'
        )
        goal_html = (
            f'<p class="subtask-goal">{_esc(item.goal)}</p>'
            if item.goal
            else '<p class="subtask-goal subtask-muted">目标未填写。</p>'
        )
        acceptance_html = (
            '<p class="subtask-acceptance"><span>验收</span>'
            f"{_esc(item.acceptance)}</p>"
            if item.acceptance
            else ""
        )
        error_html = ""
        if item.status == "failed" and item.error:
            shown_error = item.error if len(item.error) <= 400 else item.error[:400] + "…"
            error_html = (
                f'<p class="subtask-error" title="{_esc(item.error)}">'
                f"{_esc(shown_error)}</p>"
            )
        log_cell = '<span class="subtask-muted">—</span>'
        if item.log:
            try:
                log_path = _runner().resolve_log(item.log)
            except Exception:
                log_path = None
            if log_path is not None:
                log_cell = (
                    f'<a class="text-link subtask-log-link" target="_blank" rel="noopener" '
                    f'href="/log?path={_esc(quote(item.log, safe=""))}" '
                    f'title="{_esc(item.log)}">查看日志</a>'
                )
            else:
                log_cell = (
                    f'<span class="subtask-muted" title="{_esc(item.log)}">日志暂不可读</span>'
                )
        time_html = ""
        if item.finished_at:
            time_html = (
                f'<p class="subtask-time">完成于 {_esc(item.finished_at)}</p>'
            )
        rows.append(
            f"""
            <tr class="subtask-row" data-subtask-id="{_esc(item.id)}" data-status="{_esc(item.status)}">
              <td class="subtask-cell-id"><code class="subtask-id">{_esc(item.id)}</code></td>
              <td class="subtask-cell-title">
                <h3>{_esc(item.title)}</h3>
                {goal_html}
                {acceptance_html}
                {error_html}
                {time_html}
              </td>
              <td class="subtask-cell-role">{role_html}</td>
              <td class="subtask-cell-status">
                <span class="subtask-status subtask-status--{tone}">{_esc(label)}</span>
              </td>
              <td class="subtask-cell-log">{log_cell}</td>
            </tr>
            """
        )

    progress = task.subtasks_done
    total = len(task.subtasks)
    return f"""
      <section class="doc-section" id="doc-subtasks" aria-labelledby="doc-subtasks-title">
        <header class="doc-head">
          <span class="doc-index" aria-hidden="true">{index}</span>
          <div class="doc-heading">
            <h2 id="doc-subtasks-title">子任务</h2>
            <code class="doc-path">{_esc(_display_path(task.subtasks_path))}</code>
          </div>
          <span class="doc-absent">进度 {progress}/{total}</span>
        </header>
        <div class="subtask-panel">
          <p class="subtask-note">由总控拆分；分发时按每条 <code>agent_role</code> 找到角色绑定的
            Agent，串行执行。完成判定：已交付 / 已通过。</p>
          <div class="subtask-table-wrap">
            <table class="subtask-table">
              <thead>
                <tr>
                  <th scope="col">ID</th>
                  <th scope="col">标题</th>
                  <th scope="col">角色</th>
                  <th scope="col">状态</th>
                  <th scope="col">日志</th>
                </tr>
              </thead>
              <tbody>
                {"".join(rows)}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    """


def _render_task_detail(
    task: Task,
    created: bool = False,
    workdir_saved: bool = False,
    review_started: bool = False,
    advance_started: bool = False,
    revise_started: bool = False,
    handoff_retried: str = "",
) -> str:
    docking_files = _collect_files(task.docking_dir)
    workspace_files = _collect_files(task.workspace_dir)
    timestamps = [task.mtime]
    timestamps.extend(
        entry.mtime for entry in [*docking_files, *workspace_files]
    )
    last_modified = max(timestamps)
    tone = _tone(task.status)
    delivery_folder = task.delivery.folder if task.delivery else "尚未出现"
    review_state = "已记录" if task.review_path else "尚未出现"
    registration_label = REGISTER_STATUS_LABELS.get(
        task.registration_status, task.registration_status or "未登记"
    )
    subtask_progress = (
        f"{task.subtasks_done}/{len(task.subtasks)}"
        if task.subtasks
        else "—"
    )
    phase = _task_phase(task)
    phase_html = (
        f'<span class="run-pill run-pill--{_esc(_phase_tone(phase))}">'
        f"当前阶段 · {_esc(_phase_label(phase))}</span>"
        f'<span class="phase-progress">子任务 {_esc(subtask_progress)}</span>'
    )
    controller_row = (
        '<div class="meta-row"><dt>登记总控</dt>'
        f'<dd class="meta-code">{_esc(task.controller_agent)}</dd></div>'
        if task.controller_agent
        else ""
    )

    notice = (
        '<div class="notice notice--ok" role="status">'
        f"<strong>任务单已写入。</strong> {task.task_id} 已加入登记簿。"
        "</div>"
        if created
        else ""
    )
    workdir_notice = (
        '<div class="notice notice--ok" role="status">'
        "<strong>工作目录已更新。</strong> "
        f"<code>{_esc(str(task.workdir))}</code>"
        "</div>"
        if workdir_saved
        else ""
    )
    review_notice = (
        '<div class="notice notice--ok" role="status">'
        "<strong>已开始审核。</strong> 审核角色会串行复核全部交付；"
        "本页轮询结论，完成后自动显示 已通过 / 须修正。"
        "</div>"
        if review_started
        else ""
    )
    advance_notice = (
        '<div class="notice notice--ok" role="status">'
        "<strong>已启动全自动推进。</strong> 系统会继续分发剩余子任务，"
        "全部交付后自动审核；须修正或失败时会停止并等待人工处理。"
        "</div>"
        if advance_started
        else ""
    )
    revise_notice = (
        '<div class="notice notice--ok" role="status">'
        "<strong>已启动继续修正。</strong> 旧审查已复制到 "
        "<code>对接/审查历史/</code>；受影响的子任务会重新分发，"
        "全部交付后自动重新审核。本页会轮询修正进度，完成后刷新结论。"
        "</div>"
        if revise_started
        else ""
    )
    archived_notice = (
        '<div class="notice notice--archive" role="status">'
        "<strong>已归档。</strong> 项目目录当前位于 <code>归档/</code>；"
        "本页只读，恢复会把整个目录移回 <code>项目/</code>。"
        "</div>"
        if task.archived
        else ""
    )
    handoff_notice = ""
    if handoff_retried == "passed":
        handoff_notice = (
            '<div class="notice notice--ok" role="status">'
            "<strong>重试体检通过。</strong> 交接阻塞已解除，可继续下一步。"
            "</div>"
        )
    elif handoff_retried == "blocked":
        handoff_notice = (
            '<div class="notice notice--error" role="alert">'
            "<strong>重试体检仍未通过。</strong> 缺件仍在，请人工检查后再次重试。"
            "</div>"
        )
    back_href = "/archive" if task.archived else "/"
    back_label = "返回归档库" if task.archived else "返回看板"
    back_trail = "归档库" if task.archived else "任务登记簿"
    if task.archived:
        head_actions = (
            f'<a class="button button--ghost" href="/archive">返回归档库</a>'
            f"{_restore_form(task)}"
        )
        side_note = (
            "本页从 归档/ 只读打开。恢复只整体移动项目目录，不删除、不改写任何档案文件；"
            "控制台不写对接/交付/ 与审查.md。值班台与工作目录都不能在归档页修改。"
        )
    else:
        head_actions = (
            f'<a class="button button--ghost" href="/">返回看板</a>'
            f'<a class="button button--ghost" href="/runs">运行记录</a>'
            f"{_archive_form(task)}"
        )
        side_note = (
            "档案内容只读。分发只启动执行器与 Agent；控制台本身不写对接/交付/ 与审查.md。"
            "执行器以工作目录为 cwd；值班台只读，运行日志仍写在 项目/工作区/日志/。"
            "归档只把整个项目目录移入 归档/，不改写任何档案文件。"
        )

    task_doc = _render_document(
        "doc-task",
        "01",
        "任务单",
        _display_path(task.path),
        _read_text(task.path),
        task.mtime,
    )
    idea_mtime: Optional[float] = None
    if task.idea_path.is_file():
        try:
            idea_mtime = task.idea_path.stat().st_mtime
        except OSError:
            idea_mtime = None
    if idea_mtime is not None:
        idea_doc = _render_document(
            "doc-idea",
            "02",
            "我的想法",
            _display_path(task.idea_path),
            _read_text(task.idea_path),
            idea_mtime,
        )
    else:
        idea_doc = _render_missing_document(
            "doc-idea",
            "02",
            "我的想法",
            "本任务没有 对接/idea.md（由 T08 之前的旧版表单创建）。",
        )
    subtasks_doc = _render_subtasks_section(task, "03")
    if task.delivery:
        delivery_doc = _render_document(
            "doc-delivery",
            "04",
            "最新交付",
            _display_path(task.delivery.done_path),
            _read_text(task.delivery.done_path),
            task.delivery.mtime,
        )
    else:
        delivery_doc = _render_missing_document(
            "doc-delivery",
            "04",
            "最新交付",
            "对接/交付/ 中还没有 DONE.md。任务会停留在「待处理」，直到执行方交付。",
        )

    if task.review_path and task.review_mtime is not None:
        review_doc = _render_document(
            "doc-review",
            "05",
            "审查意见",
            _display_path(task.review_path),
            task.review_text,
            task.review_mtime,
        )
    else:
        review_doc = _render_missing_document(
            "doc-review",
            "05",
            "审查意见",
            "对接/审查.md 还没有出现。状态暂由交付情况决定。",
        )

    workdir_tag = "已归档 · 只读" if task.archived else "可编辑"
    workdir_tag_class = "" if task.archived else " meta-tag--edit"
    workdir_editor = (
        ""
        if task.archived
        else f"""
            <form class="workdir-form" method="post" action="/task/{_esc(task.task_id)}/workdir">
              <label class="aside-kicker" for="workdir-input">工作目录（可编辑）</label>
              <input class="field-input" id="workdir-input" name="workdir" type="text"
                maxlength="{MAX_WORKDIR_CHARS}" value="{_esc(str(task.workdir))}"
                autocomplete="off" spellcheck="false">
              <p class="workdir-hint">绝对路径；留空 = 本任务默认工作区。值班台内只允许
                <code>项目/&lt;编号&gt;/工作区/</code>。</p>
              <button class="button button--ghost button--small" type="submit">保存工作目录</button>
            </form>
            <form class="workdir-form" method="post" action="/task/{_esc(task.task_id)}/owner-role">
              <label class="aside-kicker" for="owner-role-input">owner_role（整单重修）</label>
              <input class="field-input" id="owner-role-input" name="owner_role" type="text"
                maxlength="48" value="{_esc(task.owner_role)}"
                autocomplete="off" spellcheck="false">
              <p class="workdir-hint">无子任务任务在「须修正」时回叫该角色绑定的 Agent；
                默认 <code>designer</code>，必须是已存在角色。</p>
              <button class="button button--ghost button--small" type="submit">保存 owner_role</button>
            </form>
        """
    )

    docking_section = _render_directory_section(
        "doc-docking",
        "06",
        "对接文件",
        f"{_display_path(task.docking_dir)}/",
        docking_files,
        empty_message="对接目录中还没有文件。",
        kind_for=lambda entry: _docking_file_kind(task, entry),
        preview=True,
    )
    workspace_title = "工作区文件（值班台侧）" if task.uses_custom_workdir else "工作区文件"
    workspace_empty = (
        "值班台工作区还是空的；本任务的产物在外部工作目录里，不在此列出。"
        if task.uses_custom_workdir
        else "工作区还是空的；这里会列出执行方产出的代码、文档或数据文件。"
    )
    workspace_section = _render_directory_section(
        "doc-workspace",
        "07",
        workspace_title,
        f"{_display_path(task.workspace_dir)}/",
        workspace_files,
        empty_message=workspace_empty,
    )
    workflow_panel = _render_workflow_panel(task, phase)
    handoff_panel = _render_handoff_panel(task, phase)

    content = f"""
      <header class="page-head detail-head">
        <div class="page-title-group">
          <p class="kicker"><a href="{back_href}">{back_trail}</a> <span aria-hidden="true">/</span> {_esc(task.task_id)}</p>
          <h1>{_esc(task.title)}</h1>
          <div class="detail-meta">
            {_stamp(task.status, "stamp--large")}
            {('<span class="archive-badge">已归档</span>' if task.archived else '')}
            {phase_html}
            <span>最近修改 {_esc(_fmt_time(last_modified))}</span>
          </div>
        </div>
        <div class="head-actions">
          {head_actions}
        </div>
      </header>

      {notice}
      {workdir_notice}
      {review_notice}
      {advance_notice}
      {revise_notice}
      {handoff_notice}
      {archived_notice}
      {handoff_panel}
      {workflow_panel}

      <div class="detail-layout">
        <article class="detail-stack" aria-label="任务对接档案与工作区文件">
          {task_doc}
          {idea_doc}
          {subtasks_doc}
          {delivery_doc}
          {review_doc}
          {docking_section}
          {workspace_section}
          <a class="back-link" href="{back_href}"><span aria-hidden="true">←</span> {back_label}</a>
        </article>

        <aside class="file-aside" aria-label="任务档案侧栏">
          <div class="file-aside-inner">
            <p class="aside-kicker">DOSSIER / 档案</p>
            <dl class="meta-list">
              <div class="meta-row">
                <dt>任务编号</dt>
                <dd class="meta-code">{_esc(task.task_id)}</dd>
              </div>
              <div class="meta-row">
                <dt>项目目录</dt>
                <dd class="meta-code">{_esc(task.project_name)}</dd>
              </div>
              <div class="meta-row meta-row--path">
                <dt>值班台</dt>
                <dd class="meta-code path-value">
                  <span>{_esc(str(PROJECT_DIR))}</span>
                  <span class="meta-tag">只读</span>
                </dd>
              </div>
              <div class="meta-row meta-row--path">
                <dt>工作目录</dt>
                <dd class="meta-code path-value">
                  <span>{_esc(str(task.workdir))}</span>
                  <span class="meta-tag{workdir_tag_class}">{workdir_tag}</span>
                </dd>
              </div>
              <div class="meta-row">
                <dt>档案位置</dt>
                <dd class="meta-code">{_esc("归档/" if task.archived else "项目/")}</dd>
              </div>
              <div class="meta-row">
                <dt>登记状态</dt>
                <dd class="meta-code">{_esc(registration_label)}</dd>
              </div>
              {controller_row}
              <div class="meta-row">
                <dt>当前阶段</dt>
                <dd class="meta-code">{_esc(_phase_label(phase))}</dd>
              </div>
              <div class="meta-row">
                <dt>子任务进度</dt>
                <dd class="meta-code">{_esc(subtask_progress)}</dd>
              </div>
              <div class="meta-row">
                <dt>修正轮次</dt>
                <dd class="meta-code">{task.rounds} 轮</dd>
              </div>
              <div class="meta-row">
                <dt>继续修正</dt>
                <dd class="meta-code">{task.revise_round}/{MAX_REVISE_ROUNDS} 轮</dd>
              </div>
              <div class="meta-row">
                <dt>最近修改</dt>
                <dd>{_esc(_fmt_time(last_modified))}</dd>
              </div>
              <div class="meta-row">
                <dt>最新交付</dt>
                <dd>{_esc(delivery_folder)}</dd>
              </div>
              <div class="meta-row">
                <dt>审查档案</dt>
                <dd>{_esc(review_state)}</dd>
              </div>
              <div class="meta-row">
                <dt>对接文件</dt>
                <dd class="meta-code">{len(docking_files)} 个</dd>
              </div>
              <div class="meta-row">
                <dt>工作区文件</dt>
                <dd class="meta-code">{len(workspace_files)} 个</dd>
              </div>
              <div class="meta-row">
                <dt>当前状态</dt>
                <dd><span class="status-dot dot--{tone}" aria-hidden="true"></span>{_esc(task.status)}</dd>
              </div>
            </dl>

            {workdir_editor}

            <div class="aside-note">
              {side_note} 状态由当前工作流阶段派生；交付判据统一为任务级 DONE.md、
              全部子任务已交付或无子任务时的工作区产物。
            </div>

            <nav class="aside-nav" aria-label="档案目录">
              <a href="#doc-task"><span>任务单</span><span class="aside-nav-code">01</span></a>
              <a href="#doc-idea"><span>我的想法</span><span class="aside-nav-code">02</span></a>
              <a href="#doc-subtasks"><span>子任务</span><span class="aside-nav-code">03</span></a>
              <a href="#doc-delivery"><span>最新交付</span><span class="aside-nav-code">04</span></a>
              <a href="#doc-review"><span>审查意见</span><span class="aside-nav-code">05</span></a>
              <a href="#doc-docking"><span>对接文件</span><span class="aside-nav-code">06</span></a>
              <a href="#doc-workspace"><span>{workspace_title}</span><span class="aside-nav-code">07</span></a>
            </nav>
          </div>
        </aside>
      </div>
    """
    active_nav = "archive" if task.archived else "board"
    return _render_page(f"{task.task_id} · 任务详情", active_nav, content)


def _render_new_page(values: Optional[Dict[str, str]] = None, error: str = "") -> str:
    fields = {"title": "", "idea": "", "workdir": "", "auto_advance": "1"}
    if values:
        for name in fields:
            fields[name] = str(values.get(name) or "")
    auto_advance_checked = str(fields.get("auto_advance") or "") in {
        "1",
        "on",
        "true",
        "yes",
    }
    auto_advance_attr = " checked" if auto_advance_checked else ""

    resolution = _resolve_controller()
    if resolution.agent is not None:
        controller_panel = f"""
            <div class="controller-panel is-ready">
              <p class="aside-kicker">CONTROLLER / 登记总控</p>
              <p class="controller-name">{_esc(resolution.agent.name)}</p>
              <p class="controller-id"><code>{_esc(resolution.agent.id)}</code></p>
              <p class="controller-source">{_esc(resolution.source)}</p>
              <p class="controller-note">{_esc(resolution.note)}</p>
            </div>
        """
        publish_disabled = ""
        publish_hint = "发布后自动调用总控登记并拆分 2–6 个子任务。"
    else:
        controller_panel = f"""
            <div class="controller-panel is-missing" role="alert">
              <p class="aside-kicker">CONTROLLER / 登记总控</p>
              <p class="controller-name">没有可用总控</p>
              <p class="controller-note">{_esc(resolution.note)}</p>
              <p class="controller-note"><a href="/agents">检查 Agent 登记</a> ·
                <a href="/roles/controller">检查 controller 角色</a></p>
            </div>
        """
        publish_disabled = " disabled"
        publish_hint = "先登记可执行总控 Agent，再发布任务。"

    notice = (
        '<div class="notice notice--error" role="alert">'
        f"<strong>没有发布。</strong> {_esc(error)}</div>"
        if error
        else ""
    )
    initial_status = (
        "已载入路径；发布前会再次校验。" if fields["workdir"] else
        "尚未选择。可以点左侧按钮，或直接在输入框粘贴绝对路径。"
    )

    content = f"""
      <header class="page-head intake-head">
        <div class="page-title-group">
          <p class="kicker">TASK INTAKE <span aria-hidden="true">/</span> PUBLISH</p>
          <h1>发布一个新任务</h1>
          <p class="lede">选项目文件夹，写下想法，发布。系统会分配编号、保存人类原文，
            并自动调用总控登记任务、拆出可独立运行的子任务。</p>
        </div>
        <div class="head-actions">
          <a class="button button--ghost" href="/">返回看板</a>
        </div>
      </header>

      {notice}

      <div class="publish-layout">
        <form class="publish-form" id="publish-form" method="post" action="/publish"
          data-validate-url="/api/validate-folder">
          <section class="intake-block" aria-labelledby="folder-step-title">
            <div class="intake-index" aria-hidden="true">01</div>
            <div class="intake-body">
              <div class="intake-heading">
                <div>
                  <h2 id="folder-step-title">项目文件夹</h2>
                  <p>Agent 未来在这个文件夹里工作；发布时只登记，不写入它。</p>
                </div>
                <span class="required-mark">必填</span>
              </div>
              <div class="folder-picker">
                <button class="button button--ghost folder-button" id="folder-pick" type="button">
                  选择项目文件夹…<span class="button-mark" aria-hidden="true">⌘</span>
                </button>
                <input class="field-input field-input--mono folder-input" id="field-workdir"
                  name="workdir" type="text" maxlength="{MAX_WORKDIR_CHARS}"
                  value="{_esc(fields['workdir'])}"
                  placeholder="/Users/你/Documents/项目文件夹" required
                  aria-label="项目文件夹绝对路径"
                  aria-describedby="folder-status" autocomplete="off" spellcheck="false">
              </div>
              <p class="folder-status is-idle" id="folder-status" role="status"
                aria-live="polite">{_esc(initial_status)}</p>
              <p class="intake-hint">必须是已存在的绝对路径；不能是系统根，也不能位于
                <code>协作/</code> 下。原生选择框不可用时，直接手动输入同样生效。</p>
            </div>
          </section>

          <section class="intake-block intake-block--idea" aria-labelledby="idea-step-title">
            <div class="intake-index" aria-hidden="true">02</div>
            <div class="intake-body">
              <div class="intake-heading">
                <div>
                  <h2 id="idea-step-title">我的想法 / 要实现的任务</h2>
                  <p>用自然语言写清楚即可；总控会据此起标题、登记并拆子任务。</p>
                </div>
                <span class="required-mark">必填</span>
              </div>
              <textarea class="field-input idea-input" id="field-idea" name="idea" rows="16"
                maxlength="{MAX_IDEA_CHARS}" required aria-label="我的想法或要实现的任务"
                placeholder="例如：&#10;我想做一个能自动整理每日收货单据的小工具……&#10;&#10;现在的问题、想要的结果、必须遵守的限制，都可以直接写在这里。">{_esc(fields['idea'])}</textarea>
              <div class="idea-foot">
                <span>原文会保存为 <code>对接/idea.md</code>，总控只读，不改写。</span>
                <span>支持长文粘贴</span>
              </div>
            </div>
          </section>

          <section class="intake-block intake-block--title" aria-labelledby="title-step-title">
            <div class="intake-index" aria-hidden="true">03</div>
            <div class="intake-body">
              <div class="intake-heading">
                <div>
                  <h2 id="title-step-title">任务标题</h2>
                  <p>可选。留空则由总控阅读想法后自动起名。</p>
                </div>
                <span class="optional-mark">可选</span>
              </div>
              <input class="field-input title-input" id="field-title" name="title" type="text"
                maxlength="{MAX_TITLE_CHARS}" value="{_esc(fields['title'])}"
                aria-label="任务标题（可选）"
                placeholder="留空 = 总控自动起标题" autocomplete="off">
            </div>
          </section>

          <div class="publish-bar">
            {controller_panel}
            <div class="publish-action">
              <label class="auto-advance-option" for="field-auto-advance">
                <input id="field-auto-advance" name="auto_advance" type="checkbox"
                  value="1"{auto_advance_attr}>
                <span>
                  <strong>发布后自动推进</strong>
                  <small>登记完成后自动继续分发、审核；须修正或失败时停下等人处理。</small>
                </span>
              </label>
              <p class="publish-hint">{_esc(publish_hint)}</p>
              <button class="button button--signal publish-button" type="submit"{publish_disabled}>
                发布并登记<span class="button-mark" aria-hidden="true">→</span>
              </button>
            </div>
          </div>
        </form>

        <aside class="write-note intake-note" aria-label="发布说明">
          <div class="write-note-inner">
            <p class="aside-kicker">PUBLISH FLOW / 发布之后</p>
            <h2>登记，但不执行</h2>
            <ol class="flow-list">
              <li><span>01</span>分配最小空号，创建 <code>项目/TNN_slug/对接/</code></li>
              <li><span>02</span>保存 <code>idea.md</code> 与 <code>task.yaml</code></li>
              <li><span>03</span>总控写任务单、拆 2–6 个子任务</li>
              <li><span>04</span>完成信号出现后进入任务详情</li>
            </ol>
            <dl class="scope-list">
              <div><dt>总控写入</dt><dd>任务单.md · subtasks.yaml · 登记完成.md</dd></div>
              <div><dt>本期</dt><dd>只登记和拆解；子任务执行后续再接 runner</dd></div>
              <div><dt>只读</dt><dd>协作/ · 对接/审查.md</dd></div>
              <div><dt>项目文件夹</dt><dd>必须是已存在的绝对目录</dd></div>
            </dl>
            <div class="aside-note">
              发布和登记共用执行器单实例锁；同一时刻只会有一个写手。
              如果登记失败，等待页会显示日志路径和「重试登记」。
            </div>
          </div>
        </aside>
      </div>
    """
    return _render_page("发布新任务", "new", content)


def _render_registering_page(task_id: str) -> str:
    project_dir = _find_registration_project(task_id)
    if project_dir is None:
        return _render_error(
            404,
            "没有找到这次发布",
            f"{task_id} 不在活动项目目录中。",
            "返回看板或重新发布任务。",
        )
    meta = _read_task_meta(project_dir, task_id)
    payload = _registration_payload(task_id)
    status = str(payload.get("status") or REGISTERING)
    status_label = REGISTER_STATUS_LABELS.get(status, status)
    title = str(payload.get("title") or "")
    controller = str(payload.get("controller") or "") or "待定"
    log = str(payload.get("log") or "")
    error = str(payload.get("error") or "")
    project_name = project_dir.name
    try:
        workdir = str(Path(str(meta.get("workdir") or _default_workdir(project_dir))))
    except (OSError, ValueError):
        workdir = str(meta.get("workdir") or "—")
    idea_path = project_dir / DOCKING_DIRNAME / IDEA_NAME
    idea_excerpt = ""
    if idea_path.is_file():
        try:
            text = _read_text(idea_path)
            idea_excerpt = text[:1600] + ("\n…" if len(text) > 1600 else "")
        except OSError:
            idea_excerpt = ""
    task_url = str(payload.get("task_url") or f"/task/{task_id}")
    idea_details = (
        '<details class="idea-excerpt"><summary>查看已保存的想法</summary>'
        f"<pre>{_esc(idea_excerpt)}</pre></details>"
        if idea_excerpt
        else ""
    )

    if status == REGISTERED:
        status_panel = f"""
          <div class="register-visual is-success" aria-hidden="true"><span>✓</span></div>
          <h1>登记完成</h1>
          <p class="register-lede">{_esc(title or '任务已由总控登记。')}</p>
          <p><a class="button button--primary" href="{_esc(task_url)}">进入任务详情</a></p>
        """
    elif status == REGISTER_FAILED:
        status_panel = f"""
          <div class="register-visual is-failed" aria-hidden="true"><span>!</span></div>
          <h1>登记没有完成</h1>
          <p class="register-lede">{_esc(title or '任务项目已经创建，idea.md 原文仍然保留。')}</p>
          <div class="notice notice--error" role="alert">
            <strong>总控登记失败。</strong>
            <span id="register-error-message">{_esc(error or '请查看登记日志后重试。')}</span>
          </div>
          <div class="register-meta">
            <div><span>任务编号</span><code>{_esc(task_id)}</code></div>
            <div><span>日志路径</span><code id="register-log-code">{_esc(log or '尚未生成')}</code></div>
          </div>
          <form method="post" action="/register/{_esc(task_id)}/retry">
            <button class="button button--signal" type="submit">重试登记<span class="button-mark" aria-hidden="true">↻</span></button>
          </form>
          <p class="register-secondary"><a class="text-link" href="/new">返回发布页</a>
            <a class="text-link" href="/">任务看板</a></p>
        """
    else:
        status_panel = f"""
          <div class="register-visual is-waiting" aria-hidden="true"><span></span></div>
          <h1>总控登记中…</h1>
          <p class="register-lede">{_esc(title or '总控正在阅读 idea.md，写任务单并拆分子任务。')}</p>
          <p class="register-live" id="register-live-status" role="status" aria-live="polite">
            <span class="register-pulse" aria-hidden="true"></span>正在登记 · 请稍候</p>
          <div class="register-failure-fallback" id="register-failure" hidden>
            <div class="notice notice--error" role="alert">
              <strong>总控登记失败。</strong>
              <span id="register-error-message">请查看登记日志后重试。</span>
            </div>
            <div class="register-meta">
              <div><span>任务编号</span><code>{_esc(task_id)}</code></div>
              <div><span>日志路径</span><code id="register-log-code">{_esc(log or '尚未生成')}</code></div>
            </div>
            <form method="post" action="/register/{_esc(task_id)}/retry">
              <button class="button button--signal" type="submit">重试登记<span class="button-mark" aria-hidden="true">↻</span></button>
            </form>
          </div>
          <p class="register-secondary"><a class="text-link" href="/">返回任务看板</a></p>
        """

    content = f"""
      <header class="page-head intake-head">
        <div class="page-title-group">
          <p class="kicker">REGISTRATION <span aria-hidden="true">/</span> {_esc(task_id)}</p>
          <p class="lede">发布项目：<code>{_esc(project_name)}</code></p>
        </div>
      </header>

      <section class="register-stage" id="register-poll"
        data-status-url="/api/task/{_esc(task_id)}/register-status"
        data-task-url="{_esc(task_url)}"
        data-initial-status="{_esc(status)}"
        data-poll-ms="{REGISTER_POLL_INTERVAL_MS}">
        <div class="register-card">
          <p class="register-badge">{_esc(status_label)}</p>
          {status_panel}
        </div>
        <aside class="register-aside">
          <div class="write-note-inner">
            <p class="aside-kicker">REGISTER FACTS / 登记信息</p>
            <dl class="register-facts">
              <div><dt>任务编号</dt><dd><code>{_esc(task_id)}</code></dd></div>
              <div><dt>项目文件夹</dt><dd><code>{_esc(workdir)}</code></dd></div>
              <div><dt>登记总控</dt><dd><code>{_esc(controller)}</code></dd></div>
              <div><dt>idea.md</dt><dd>{'已保存人类原文' if idea_excerpt else '尚未保存'}</dd></div>
            </dl>
            {idea_details}
            <div class="aside-note">
              登记完成后会自动跳到任务详情。总控调用与其他 Agent 共用执行器单实例锁；
              失败时 task.yaml 会标记 register_failed，并保留日志路径供重试。
            </div>
          </div>
        </aside>
      </section>
    """
    return _render_page(f"{task_id} · 登记中", "new", content)


def _render_dispatch_page() -> str:
    guard_status = _load_guard_status()
    guard_state = guard_status["state"]
    running = guard_state == "running"
    state_lines = _state_line_count()
    journal_exists = JOURNAL_LOG.is_file()
    tail = _tail_lines(JOURNAL_LOG, JOURNAL_TAIL_LINES)
    run_snapshot = _runner().snapshot()
    run_running = bool(run_snapshot.get("running"))
    run_state = str(run_snapshot.get("state") or "idle")
    run_state_label = _run_state_label(run_state)
    run_tone = _run_state_tone(run_state)
    run_task = str(run_snapshot.get("task") or "")
    run_agent = str(run_snapshot.get("agent") or "")
    if run_running:
        run_headline = f"执行器 ● 运行中（任务 {_esc(run_task or '未知')}）"
        run_detail = f"Agent {_esc(run_agent or '—')} · 开始于 {_esc(str(run_snapshot.get('started') or '—'))}"
    else:
        run_headline = "执行器 ● 空闲"
        if run_state in {"success", "failed", "timeout", "stale"}:
            run_detail = f"最近一次：{_esc(run_state_label)} · {_esc(run_task or '—')}"
        else:
            run_detail = "还没有执行器运行记录"

    if journal_exists:
        if tail:
            journal_html = f'<pre class="journal-log">{_esc(chr(10).join(tail))}</pre>'
        else:
            journal_html = '<p class="journal-empty">journal.log 为空。菜单栏应用的守卫输出会显示在这里。</p>'
    else:
        journal_html = (
            '<p class="journal-empty">journal.log 还没有创建。启动菜单栏应用后，守卫输出会从这里开始累计。</p>'
        )

    guard_labels = {
        "running": "常驻中",
        "stopped": "已停止",
        "unknown": "未上报",
    }
    guard_label = guard_labels[guard_state]
    guard_class = "is-running" if running else "is-idle"
    updated = guard_status["updated"] or "—"
    guard_note = guard_status["note"] or "状态由菜单栏应用写入"
    status_source = (
        f"菜单栏应用 · 更新于 {updated}"
        if guard_state != "unknown"
        else "菜单栏应用尚未写入 guard.status"
    )

    baseline_warning = ""
    if state_lines == 0:
        baseline_warning = """
          <div class="dispatch-warning">
            <span class="dispatch-warning-label">基线提示</span>
            <p>watch.state 当前为空。下一轮守卫会以空基线比较，现有档案可能在日志中被标为「消失」；菜单栏应用会在检测到变化后自动刷新基线。</p>
          </div>
        """

    content = f"""
      <header class="page-head">
        <div class="page-title-group">
          <p class="kicker">WATCH DESK <span aria-hidden="true">/</span> GUARD + EXECUTOR</p>
          <h1>调度台</h1>
          <p class="lede">同一个调度台显示两件事：看守 <code>项目/*/对接/</code> 的常驻守卫，以及全局单实例的 Agent 执行器。守卫启停由 macOS 菜单栏应用负责；执行器只从任务详情页派发。</p>
        </div>
        <div class="head-actions">
          <a class="button button--ghost" href="/runs">运行记录</a>
          <span class="head-stamp">MENU BAR / 菜单栏托管</span>
        </div>
      </header>

      <section class="executor-strip" data-state="{_esc(run_tone)}" aria-label="执行器状态">
        <div class="executor-strip-main">
          <p class="stat-label">EXECUTOR / 执行器</p>
          <p class="executor-indicator {'is-running' if run_running else 'is-idle'}">
            <span class="guard-dot" aria-hidden="true"></span>{run_headline}
          </p>
          <p class="stat-note">{run_detail}</p>
        </div>
        <div class="executor-strip-meta">
          <span>锁 <code>协作/调度台/runner.lock</code></span>
          <span>超时 <strong>{_esc(f'{RUNNER_TIMEOUT_SECONDS:g} 秒')}</strong></span>
          <a class="text-link" href="/runs">查看当前运行与最近 {RUN_HISTORY_LIMIT} 次 →</a>
        </div>
      </section>

      <div class="dispatch-layout">
        <div class="dispatch-main">
          <section class="dispatch-summary" id="guard-summary" data-guard-state="{_esc(guard_state)}" aria-label="调度台状态">
            <div class="stat-block">
              <p class="stat-label">watch.state 行数</p>
              <p class="stat-value">{state_lines}<span>行</span></p>
            </div>
            <div class="stat-block">
              <p class="stat-label">守卫状态</p>
              <p class="guard-indicator {guard_class}">
                <span class="guard-dot" aria-hidden="true"></span>{guard_label}
              </p>
              <p class="stat-note">{_esc(status_source)}</p>
            </div>
            <div class="stat-block stat-block--wide">
              <p class="stat-label">当前状态</p>
              <code class="command">{_esc(guard_note)}</code>
            </div>
          </section>

          {baseline_warning}

          <section class="journal-section" aria-labelledby="journal-title">
            <div class="section-heading">
              <h2 class="section-title" id="journal-title">journal.log</h2>
              <span class="section-meta">尾部 {JOURNAL_TAIL_LINES} 行</span>
            </div>
            <div class="journal-console">
              <div class="journal-chrome" aria-hidden="true">
                <span>协作/调度台/journal.log</span>
                <span>tail -n {JOURNAL_TAIL_LINES}</span>
              </div>
              {journal_html}
            </div>
          </section>
        </div>

        <aside class="write-note dispatch-note" aria-label="守卫说明">
          <div class="write-note-inner">
            <p class="aside-kicker">RUNBOOK / 守卫与执行器</p>
            <h2>两套进程模型</h2>
            <p>守卫由右上角菜单栏应用运行 <code>watch.sh</code>，检测到变化后写入 journal.log、刷新基线并重挂；执行器只在任务详情页收到派发时启动一个 Agent 子进程。</p>
            <dl class="scope-list">
              <div><dt>守卫状态</dt><dd>guard.status（只读）</dd></div>
              <div><dt>观察</dt><dd>项目/*/对接/（递归）</dd></div>
              <div><dt>执行状态</dt><dd>runner.status</dd></div>
              <div><dt>并发</dt><dd>全局仅一个写手</dd></div>
              <div><dt>超时</dt><dd>{_esc(f'{RUNNER_TIMEOUT_SECONDS:g} 秒')}</dd></div>
            </dl>
            <div class="aside-note">守卫没有网页启停按钮。执行器不能从这里直接启动；入口只在任务详情页「开始分发实现」，按子任务角色绑定串行执行。执行器不会代写审查或交付。</div>
          </div>
        </aside>
      </div>
    """
    return _render_page("调度台", "dispatch", content)


def _render_run_task_link(task_id: str) -> str:
    if TASK_ID_RE.fullmatch(task_id):
        return f'<a class="run-task-link" href="/task/{_esc(task_id)}">{_esc(task_id)}</a>'
    return '<span class="run-task-link">—</span>'


def _render_run_log_tail(record: Dict[str, object], *, open_by_default: bool = False) -> str:
    lines = _run_log_tail(record)
    log_display = str(record.get("log") or "") or "（未记录日志路径）"
    body = (
        f'<pre class="run-log-pre">{_esc(chr(10).join(lines))}</pre>'
        if lines
        else '<p class="run-log-empty">日志为空，或对应项目已移动后无法读取尾行。</p>'
    )
    open_attr = " open" if open_by_default else ""
    return f"""
      <details class="run-log"{open_attr}>
        <summary>
          <span>日志尾部 {RUN_LOG_TAIL_LINES} 行</span>
          <code>{_esc(log_display)}</code>
        </summary>
        {body}
      </details>
    """


def _render_run_card(
    record: Dict[str, object], *, current: bool = False, open_log: bool = False
) -> str:
    state = str(record.get("state") or "idle")
    tone = _run_state_tone(state)
    task_id = str(record.get("task") or "")
    status_label = _run_state_label(state)
    agent_name = str(record.get("agent_name") or "")
    agent = str(record.get("agent") or "")
    agent_label = f"{agent_name} · {agent}" if agent_name else agent or "—"
    round_no = record.get("round")
    kind = str(record.get("kind") or "run")
    subtask = str(record.get("subtask") or "")
    if kind == "dispatch":
        round_html = (
            f"分发 · {_esc(subtask)}" if subtask else "分发"
        )
    elif kind == "review":
        round_html = "审核"
    elif kind == "handoff":
        round_html = "交接补交"
    elif kind == "whole_rework":
        round_html = "整单重修"
    else:
        round_html = f"r{_esc(round_no)}" if round_no not in (None, "") else "—"
    run_id = str(record.get("run_id") or "")
    pid = record.get("pid")
    pid_display = str(pid) if pid not in (None, "") else "—"
    error = str(record.get("error") or "")
    error_html = (
        f'<p class="run-error">{_esc(error)}</p>' if error else ""
    )
    current_badge = '<span class="run-current-badge">当前运行</span>' if current else ""
    return f"""
      <article class="run-card" data-state="{_esc(tone)}">
        <header class="run-card-head">
          <div class="run-card-title">
            <span class="run-state run-state--{_esc(tone)}">{_esc(status_label)}</span>
            {current_badge}
            {_render_run_task_link(task_id)}
            <span class="run-agent">{_esc(agent_label)}</span>
          </div>
          <span class="run-id">{_esc(run_id or "—")}</span>
        </header>
        <dl class="run-meta">
          <div><dt>开始</dt><dd>{_esc(str(record.get("started") or "—"))}</dd></div>
          <div><dt>耗时</dt><dd>{_esc(_format_duration(record.get("duration_seconds")))}</dd></div>
          <div><dt>轮次</dt><dd>{round_html}</dd></div>
          <div><dt>PID</dt><dd>{_esc(pid_display)}</dd></div>
          <div><dt>退出</dt><dd>{_esc(_format_exit(record.get("exit")))}</dd></div>
        </dl>
        {error_html}
        {_render_run_log_tail(record, open_by_default=open_log)}
      </article>
    """


def _render_runs_page(*, started: str = "") -> str:
    engine = _runner()
    snapshot = engine.snapshot()
    history = engine.history(RUN_HISTORY_LIMIT)
    running = bool(snapshot.get("running"))
    state = str(snapshot.get("state") or "idle")
    state_label = _run_state_label(state)
    tone = _run_state_tone(state)
    current_task = str(snapshot.get("task") or "")
    current_agent = str(snapshot.get("agent_name") or snapshot.get("agent") or "")

    notices: List[str] = []
    if started:
        notices.append(
            _render_notice(
                f"<strong>已派发。</strong> 运行 <code>{_esc(started)}</code> 已启动；"
                "执行器会在后台写入日志与状态，刷新本页查看结果。"
            )
        )

    if running:
        current_body = _render_run_card(
            snapshot, current=True, open_log=True
        )
        current_hint = (
            f"任务 {_esc(current_task or '未知')} 正在执行。"
            "执行器全局锁生效，刷新本页不会中断任务。"
        )
    else:
        current_body = f"""
        <div class="run-idle">
          <p class="run-idle-title">当前没有运行中的任务</p>
          <p>执行器空闲。到任务详情页点「开始分发实现」，系统会按
             <code>subtasks.yaml</code> 的 <code>agent_role</code> 绑定串行执行；
             同一时刻只允许一个写手，超时、失败或退出都会释放锁。</p>
          <div class="run-idle-actions">
            <a class="button button--ghost button--small" href="/">返回任务看板</a>
            <a class="button button--ghost button--small" href="/dispatch">查看调度台</a>
          </div>
        </div>
        """
        if state in {"success", "failed", "timeout", "stale"}:
            current_hint = f"执行器空闲。最近一次运行：{state_label} · {current_task or '—'}。"
        else:
            current_hint = "还没有运行记录。"

    if history:
        history_html = "".join(
            _render_run_card(record, open_log=(index == 0))
            for index, record in enumerate(history)
        )
        history_body = f'<div class="run-list">{history_html}</div>'
    else:
        history_body = """
        <div class="empty-state empty-state--compact">
          <p class="empty-kicker">NO RUNS YET</p>
          <h2>还没有运行记录</h2>
          <p>第一次在任务详情页开始分发实现后，状态、耗时和日志尾部会显示在这里。</p>
        </div>
        """

    content = f"""
      <header class="page-head">
        <div class="page-title-group">
          <p class="kicker">EXECUTOR <span aria-hidden="true">/</span> RUN LEDGER</p>
          <h1>运行记录</h1>
          <p class="lede">当前运行与最近 {RUN_HISTORY_LIMIT} 次执行。执行器用固定提示词调用已登记 Agent，
             只追加日志、状态与锁；同一时刻最多一个写手。</p>
        </div>
        <div class="head-actions">
          <a class="button button--ghost" href="/dispatch">调度台</a>
          <a class="button button--ghost" href="/">任务看板</a>
        </div>
      </header>

      {''.join(notices)}

      <section class="run-executor-banner" data-state="{_esc(tone)}" aria-label="执行器状态">
        <div>
          <p class="stat-label">EXECUTOR / 执行器</p>
          <p class="executor-indicator {'is-running' if running else 'is-idle'}">
            <span class="guard-dot" aria-hidden="true"></span>{f'执行器 ● 运行中（任务 {_esc(current_task)}）' if running else '执行器 ● 空闲'}
          </p>
          <p class="stat-note">{_esc(current_hint)}</p>
        </div>
        <dl class="run-global-meta">
          <div><dt>全局锁</dt><dd>{_esc('占用中' if running else '未占用')}</dd></div>
          <div><dt>超时</dt><dd>{_esc(f'{RUNNER_TIMEOUT_SECONDS:g} 秒')}</dd></div>
          <div><dt>记录</dt><dd>{len(history):02d} / {RUN_HISTORY_LIMIT:02d}</dd></div>
        </dl>
      </section>

      <section class="run-section" aria-labelledby="current-run-title">
        <div class="section-heading">
          <h2 class="section-title" id="current-run-title">当前运行</h2>
          <span class="section-meta">{_esc('RUNNING' if running else 'IDLE')}</span>
        </div>
        {current_body}
      </section>

      <section class="run-section" aria-labelledby="recent-run-title">
        <div class="section-heading">
          <h2 class="section-title" id="recent-run-title">最近运行</h2>
          <span class="section-meta">最多 {RUN_HISTORY_LIMIT} 次 · 日志尾部 {RUN_LOG_TAIL_LINES} 行</span>
        </div>
        {history_body}
      </section>

      <p class="run-page-note">
        状态文件 <code>协作/调度台/runner.status</code> · 只追加历史
        <code>runner.history.jsonl</code> · 锁 <code>runner.lock</code>。
        运行中的日志位于任务项目 <code>工作区/日志/</code>；归档后仍从 <code>归档/</code> 读取尾部。
        当前 Agent：{_esc(current_agent or '—')}。
      </p>
    """
    return _render_page("运行记录", "runs", content)


def _render_notice(message: str, *, tone: str = "ok") -> str:
    role = "alert" if tone == "error" else "status"
    return (
        f'<div class="notice notice--{_esc(tone)}" role="{role}">'
        f"{message}</div>"
    )


def _render_config_issues(issues: List[ConfigIssue]) -> str:
    if not issues:
        return ""
    rows = "".join(
        f"<li><code>{_esc(_display_path(issue.path))}</code>：{_esc(issue.message)}</li>"
        for issue in issues
    )
    return (
        '<div class="notice notice--warn" role="status">'
        "<strong>有配置未能读取。</strong> "
        f"{len(issues)} 个文件被跳过，原文件未改写。"
        f'<ul class="issue-list">{rows}</ul>'
        "</div>"
    )


def _role_form_values(role: Optional[Role] = None) -> Dict[str, str]:
    if role is None:
        return {
            "id": "",
            "name": "",
            "description": "",
            "agent_id": "",
            "write_scope": "",
            "enabled": "1",
            "notes": "",
        }
    return {
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "agent_id": role.agent_id,
        "write_scope": "\n".join(role.write_scope),
        "enabled": "1" if role.enabled else "",
        "notes": role.notes,
    }


def _render_roles_page(
    *, notice: str = "", error: str = "", archived_id: str = ""
) -> str:
    roles, issues = _load_roles()
    agents, _agent_issues = _load_agents()
    agent_names = {agent.id: agent.name for agent in agents}
    enabled_count = sum(1 for role in roles if role.enabled)
    bound_count = sum(1 for role in roles if role.agent_id)
    issue_html = _render_config_issues(issues)
    if archived_id:
        notice = (
            f"角色「{_esc(archived_id)}」已移出列表；原 YAML 保留在 "
            "<code>协作/roles/.archive/</code>，没有删除。"
        )

    notice_html = _render_notice(notice) if notice else ""
    error_html = _render_notice(f"<strong>操作未完成。</strong> {_esc(error)}", tone="error") if error else ""

    if roles:
        rows = []
        for index, role in enumerate(roles, start=1):
            agent_label = "未绑定 Agent"
            if role.agent_id:
                agent_label = agent_names.get(role.agent_id, role.agent_id)
            scopes = "".join(
                f'<span class="scope-chip">{_esc(scope)}</span>' for scope in role.write_scope
            ) or '<span class="scope-chip scope-chip--empty">未限定写入范围</span>'
            state_class = "is-enabled" if role.enabled else "is-disabled"
            state_label = "已启用" if role.enabled else "已停用"
            description = role.description or "尚未填写角色说明。"
            rows.append(
                f"""
                <li class="role-item {state_class}">
                  <a class="role-row" href="/roles/{_esc(role.id)}">
                    <span class="role-index">{index:02d}</span>
                    <span class="role-main">
                      <span class="role-heading">
                        <strong class="role-name">{_esc(role.name)}</strong>
                        <code class="role-id">{_esc(role.id)}</code>
                      </span>
                      <span class="role-description">{_esc(description)}</span>
                      <span class="role-scope">{scopes}</span>
                    </span>
                    <span class="role-side">
                      <span class="role-state role-state--{'on' if role.enabled else 'off'}">{state_label}</span>
                      <span class="role-agent">{_esc(agent_label)}</span>
                      <span class="role-file">协作/roles/{_esc(role.id)}.yaml</span>
                    </span>
                    <span class="role-enter" aria-hidden="true">↗</span>
                  </a>
                </li>
                """
            )
        roles_html = f"""
        <section class="config-section" aria-labelledby="roles-list-title">
          <div class="section-heading">
            <h2 class="section-title" id="roles-list-title">角色登记簿</h2>
            <span class="section-meta">{len(roles):02d} 个角色 · 点击编辑</span>
          </div>
          <ol class="role-register">{''.join(rows)}</ol>
        </section>
        """
    else:
        roles_html = """
        <section class="empty-state" aria-labelledby="roles-empty-title">
          <p class="empty-kicker">ROLES EMPTY</p>
          <h2 id="roles-empty-title">还没有角色</h2>
          <p>新增一个角色，再为它绑定已登记的 Agent 应用。</p>
          <a class="button button--primary" href="/roles/new">新增角色</a>
        </section>
        """

    content = f"""
      <header class="page-head">
        <div class="page-title-group">
          <p class="kicker">ROLE REGISTRY <span aria-hidden="true">/</span> ROUTING</p>
          <h1>角色</h1>
          <p class="lede">每个角色绑定一个已登记的 Agent CLI。本页只维护路由配置：<strong>派发入口在任务详情页</strong>，执行器会按绑定解析 Agent 并启动一次运行。配置以可手改的 YAML 保存在 <code>协作/roles/</code>。</p>
        </div>
        <div class="head-actions">
          <a class="button button--ghost" href="/agents">Agent 应用</a>
          <a class="button button--primary" href="/roles/new">新增角色<span class="button-mark" aria-hidden="true">＋</span></a>
        </div>
      </header>

      {notice_html}
      {error_html}
      {issue_html}

      <section class="config-summary" aria-label="角色概况">
        <div class="summary-total">
          <span class="summary-kicker">ROLES / 角色</span>
          <strong class="summary-number">{len(roles):02d}</strong>
          <span class="summary-unit">个定义</span>
        </div>
        <dl class="config-index">
          <div><dt>已启用</dt><dd>{enabled_count:02d}</dd></div>
          <div><dt>已绑定 Agent</dt><dd>{bound_count:02d}</dd></div>
          <div><dt>Agent 登记</dt><dd>{len(agents):02d}</dd></div>
        </dl>
      </section>

      {roles_html}
    """
    return _render_page("角色", "roles", content)


def _render_text_field(
    name: str,
    label: str,
    hint: str,
    value: str,
    *,
    wide: bool = True,
    required: bool = False,
    readonly: bool = False,
    placeholder: str = "",
    mono: bool = False,
    maxlength: int = MAX_CONFIG_TEXT_CHARS,
) -> str:
    classes = "field field--wide" if wide else "field field--half"
    required_html = ' <span class="required-mark">必填</span>' if required else ""
    readonly_html = " readonly" if readonly else ""
    mono_class = " field-input--mono" if mono else ""
    required_attr = " required" if required else ""
    return f"""
      <div class="{classes}">
        <div class="field-head">
          <label class="field-label" for="field-{name}">{_esc(label)}{required_html}</label>
          <span class="field-hint">{_esc(hint)}</span>
        </div>
        <input class="field-input{mono_class}" id="field-{name}" name="{name}" type="text"
          value="{_esc(value)}" maxlength="{maxlength}" placeholder="{_esc(placeholder)}"
          autocomplete="off"{readonly_html}{required_attr}>
      </div>
    """


def _render_textarea_field(
    name: str,
    label: str,
    hint: str,
    value: str,
    *,
    rows: int = 4,
    wide: bool = True,
    mono: bool = False,
    placeholder: str = "",
) -> str:
    classes = "field field--wide" if wide else "field field--half"
    mono_class = " field-input--mono" if mono else ""
    return f"""
      <div class="{classes}">
        <div class="field-head">
          <label class="field-label" for="field-{name}">{_esc(label)}</label>
          <span class="field-hint">{_esc(hint)}</span>
        </div>
        <textarea class="field-input{mono_class}" id="field-{name}" name="{name}" rows="{rows}"
          placeholder="{_esc(placeholder)}">{_esc(value)}</textarea>
      </div>
    """


def _render_agent_select(value: str, agents: List[AgentApp]) -> str:
    options = ['<option value="">— 暂不绑定 —</option>']
    for agent in agents:
        selected = " selected" if agent.id == value else ""
        current = "" if agent.installed else "（登记时不可执行）"
        options.append(
            f'<option value="{_esc(agent.id)}"{selected}>{_esc(agent.name)} · {_esc(agent.id)}{_esc(current)}</option>'
        )
    if value and not any(agent.id == value for agent in agents):
        options.append(f'<option value="{_esc(value)}" selected>{_esc(value)}（文件缺失）</option>')
    agent_hint = (
        f'<a class="text-link" href="/agents">先登记 Agent</a>'
        if not agents
        else "从已登记的 Agent 应用中选择"
    )
    return f"""
      <div class="field field--half">
        <div class="field-head">
          <label class="field-label" for="field-agent_id">绑定 Agent</label>
          <span class="field-hint">{agent_hint}</span>
        </div>
        <select class="field-input field-input--select" id="field-agent_id" name="agent_id">
          {''.join(options)}
        </select>
      </div>
    """


def _render_role_form(
    values: Dict[str, str],
    *,
    role_id: Optional[str],
    agents: List[AgentApp],
    error: str = "",
    notice: str = "",
) -> str:
    is_new = role_id is None
    title = "新增角色" if is_new else "编辑角色"
    action = "/roles/new" if is_new else f"/roles/{role_id}"
    notice_html = notice
    if error:
        notice_html = _render_notice(
            f"<strong>没有保存角色。</strong> {_esc(error)}", tone="error"
        )
    enabled_checked = " checked" if values.get("enabled") else ""
    id_control = _render_text_field(
        "id",
        "角色 ID",
        "小写字母、数字、- 或 _；创建后不可改名",
        values.get("id", ""),
        wide=False,
        required=True,
        readonly=not is_new,
        placeholder="例如 designer-cn",
        mono=True,
        maxlength=48,
    )
    name_control = _render_text_field(
        "name",
        "显示名称",
        "例如 设计师",
        values.get("name", ""),
        wide=False,
        required=True,
        placeholder="角色名称",
        maxlength=MAX_CONFIG_NAME_CHARS,
    )
    description_control = _render_textarea_field(
        "description",
        "角色说明",
        "职责与协作边界",
        values.get("description", ""),
        rows=3,
    )
    scope_control = _render_textarea_field(
        "write_scope",
        "写入范围",
        "每行一项；作为配置展示，执行器按固定任务边界运行",
        values.get("write_scope", ""),
        rows=5,
        mono=True,
        placeholder="项目/*/工作区/**\n项目/*/对接/交付/**",
    )
    notes_control = _render_textarea_field(
        "notes",
        "备注",
        "留给人类的补充说明",
        values.get("notes", ""),
        rows=3,
    )
    danger_html = ""
    if not is_new:
        danger_html = f"""
        <section class="config-danger" aria-labelledby="role-danger-title">
          <div>
            <p class="aside-kicker">ARCHIVE ROLE / 移出列表</p>
            <h2 id="role-danger-title">删除这个角色</h2>
            <p>为遵守“零删除”，YAML 不会消失：它会移动到 <code>协作/roles/.archive/</code>，需要时可由人类手动移回。</p>
          </div>
          <form method="post" action="/roles/{_esc(role_id)}/delete"
            data-confirm="确认将角色 {_esc(values.get('name') or role_id)} 移出列表？YAML 会保留在 协作/roles/.archive/，不会删除文件。">
            <button class="button button--danger" type="submit">删除（保留文件）</button>
          </form>
        </section>
        """
    content = f"""
      <header class="page-head">
        <div class="page-title-group">
          <p class="kicker"><a href="/roles">角色</a> <span aria-hidden="true">/</span> {'NEW ROLE' if is_new else _esc(role_id or '')}</p>
          <h1>{title}</h1>
          <p class="lede">只定义角色并绑定 Agent；保存本页不会执行 CLI。派发在任务详情页发生，由执行器按固定提示词启动。配置可在 <code>协作/roles/&lt;role-id&gt;.yaml</code> 直接阅读与手改。</p>
        </div>
        <div class="head-actions">
          <a class="button button--ghost" href="/roles">返回角色</a>
        </div>
      </header>

      {notice_html}

      <div class="form-layout">
        <form class="config-form" method="post" action="{_esc(action)}">
          <div class="form-ledger" aria-hidden="true">
            <span>{'新建定义' if is_new else '编辑定义'}</span>
            <span>仅写 协作/roles/</span>
            <span>不在此页触发 Agent</span>
          </div>
          {id_control}
          {name_control}
          {description_control}
          {_render_agent_select(values.get('agent_id', ''), agents)}
          <div class="field field--half">
            <div class="field-head">
              <label class="field-label" for="field-enabled">启用状态</label>
              <span class="field-hint">停用后该角色不可派发</span>
            </div>
            <label class="check-control" for="field-enabled">
              <input id="field-enabled" name="enabled" type="checkbox" value="1"{enabled_checked}>
              <span>在角色列表中显示为已启用</span>
            </label>
          </div>
          {scope_control}
          {notes_control}
          <div class="form-actions">
            <button class="button button--signal" type="submit">保存角色<span class="button-mark" aria-hidden="true">→</span></button>
            <a class="text-link" href="/roles">取消，返回角色列表</a>
          </div>
        </form>

        <aside class="write-note" aria-label="角色配置说明">
          <div class="write-note-inner">
            <p class="aside-kicker">WRITE SCOPE / 边界</p>
            <h2>只写配置</h2>
            <p>本页只写入 <code>协作/roles/</code>；不会写任务单、交付、审查或工作区，也不会调用绑定的 Agent。</p>
            <dl class="scope-list">
              <div><dt>角色文件</dt><dd>协作/roles/*.yaml</dd></div>
              <div><dt>Agent 来源</dt><dd>协作/agents/*.yaml</dd></div>
              <div><dt>删除方式</dt><dd>移动到 .archive</dd></div>
              <div><dt>执行</dt><dd>任务详情页派发</dd></div>
            </dl>
            <div class="aside-note">字段固定为 id、name、description、agent_id、write_scope、enabled、notes；YAML 为兼容手改的小型子集。</div>
          </div>
        </aside>
      </div>

      {danger_html}
    """
    return _render_page(title, "roles", content)


def _render_agents_page(
    *, values: Optional[Dict[str, str]] = None, error: str = "", notice: str = ""
) -> str:
    agents, issues = _load_agents()
    roles, _role_issues = _load_roles()
    detected = _detect_agents()
    form_values = values or {
        "id": "",
        "name": "",
        "command": "",
        "args_template": "",
        "cwd": str(PROJECT_DIR),
        "notes": "",
    }
    issue_html = _render_config_issues(issues)
    notice_html = _render_notice(notice) if notice else ""
    error_html = (
        _render_notice(f"<strong>没有登记 Agent。</strong> {_esc(error)}", tone="error")
        if error
        else ""
    )
    bindings: Dict[str, List[str]] = {}
    for role in roles:
        if role.agent_id:
            bindings.setdefault(role.agent_id, []).append(role.name)

    if agents:
        agent_cards = []
        for agent in agents:
            current = (
                '<span class="status-chip status-chip--ok">当前可执行</span>'
                if agent.currently_executable
                else '<span class="status-chip status-chip--warn">当前不可执行</span>'
            )
            installed = "已安装" if agent.installed else "登记时不可执行"
            bound_roles = "、".join(bindings.get(agent.id, [])) or "尚未被角色绑定"
            args = agent.args_template or "（未设置）"
            cwd = agent.cwd or "（未设置）"
            agent_cards.append(
                f"""
                <li class="agent-card">
                  <header class="agent-head">
                    <div>
                      <p class="agent-role-id">{_esc(agent.id)}</p>
                      <h3>{_esc(agent.name)}</h3>
                    </div>
                    <div class="agent-head-state">
                      {current}
                      <span class="status-chip">{_esc(installed)}</span>
                    </div>
                  </header>
                  <dl class="agent-spec">
                    <div><dt>命令</dt><dd><code>{_esc(agent.command)}</code></dd></div>
                    <div><dt>参数模板</dt><dd><code>{_esc(args)}</code></dd></div>
                    <div><dt>登记 cwd</dt><dd><code>{_esc(cwd)}</code><br><span class="agent-foot">执行任务时固定使用任务项目目录</span></dd></div>
                    <div><dt>版本（前 {MAX_STORED_VERSION_CHARS} 字符）</dt><dd><code>{_esc(agent.version or '（未探测）')}</code></dd></div>
                    <div><dt>被角色绑定</dt><dd>{_esc(bound_roles)}</dd></div>
                  </dl>
                  <p class="agent-foot">登记文件 <code>协作/agents/{_esc(agent.id)}.yaml</code> · 最后修改 {_esc(_fmt_time(agent.mtime))}</p>
                  {f'<p class="agent-note">{_esc(agent.notes)}</p>' if agent.notes else ''}
                </li>
                """
            )
        registered_html = f"""
        <section class="config-section" aria-labelledby="registered-agents-title">
          <div class="section-heading">
            <h2 class="section-title" id="registered-agents-title">已登记 Agent</h2>
            <span class="section-meta">{len(agents):02d} 个应用 · 在任务详情页派发</span>
          </div>
          <ol class="agent-grid">{''.join(agent_cards)}</ol>
        </section>
        """
    else:
        registered_html = """
        <section class="empty-state" aria-labelledby="agents-empty-title">
          <p class="empty-kicker">NO REGISTERED AGENT</p>
          <h2 id="agents-empty-title">还没有登记 Agent</h2>
          <p>从下方检测结果一键登记，或手动填写 CLI 绝对路径与参数模板。</p>
        </section>
        """

    if detected:
        registered_paths = [agent.command for agent in agents]
        rows = []
        for item in detected:
            already = next(
                (agent for agent in agents if _same_agent_command(agent.command, item.path)),
                None,
            )
            exec_label = (
                '<span class="status-chip status-chip--ok">可执行</span>'
                if item.executable
                else '<span class="status-chip status-chip--warn">不可执行</span>'
            )
            if already:
                action = f'<span class="detected-registered">已登记为 <code>{_esc(already.id)}</code></span>'
            else:
                suggested_id = _suggest_agent_id(item.path, [agent.id for agent in agents])
                action = f"""
                <form method="post" action="/agents/new" class="detect-form">
                  <input type="hidden" name="id" value="{_esc(suggested_id)}">
                  <input type="hidden" name="name" value="{_esc(Path(item.path).name)}">
                  <input type="hidden" name="command" value="{_esc(item.path)}">
                  <input type="hidden" name="cwd" value="{_esc(str(PROJECT_DIR))}">
                  <input type="hidden" name="args_template" value="">
                  <input type="hidden" name="notes" value="由本机自动检测后登记">
                  <button class="button button--ghost button--small" type="submit">登记</button>
                </form>
                """
            rows.append(
                f"""
                <li class="detected-item">
                  <div class="detected-main">
                    <div class="detected-path-line">
                      <code class="detected-path">{_esc(item.path)}</code>
                      <span class="detected-source">{_esc(item.source)}</span>
                    </div>
                    <span class="detected-version">{_esc(item.version)}</span>
                  </div>
                  <div class="detected-state">{exec_label}{action}</div>
                </li>
                """
            )
        detected_html = f"""
        <section class="config-section" aria-labelledby="detected-agents-title">
          <div class="section-heading">
            <h2 class="section-title" id="detected-agents-title">本机检测</h2>
            <span class="section-meta">{len(detected):02d} 个候选 · --version 超时 {VERSION_TIMEOUT_SECONDS}s</span>
          </div>
          <p class="section-note">扫描 <code>~/claude-model/bin/*</code> 与 <code>command -v claude claude-ds claude-doubao codex openclaw</code>。版本只读取前 {MAX_STORED_VERSION_CHARS} 字符，每个探测最多等待 {VERSION_TIMEOUT_SECONDS} 秒。</p>
          <ol class="detected-list">{''.join(rows)}</ol>
        </section>
        """
    else:
        detected_html = """
        <section class="config-section" aria-labelledby="detected-agents-title">
          <div class="section-heading">
            <h2 class="section-title" id="detected-agents-title">本机检测</h2>
            <span class="section-meta">0 个候选</span>
          </div>
          <div class="empty-state empty-state--compact">
            <p>没有在已知位置找到 Agent CLI；可在下方手动登记绝对路径。</p>
          </div>
        </section>
        """

    content = f"""
      <header class="page-head">
        <div class="page-title-group">
          <p class="kicker">AGENT APPLICATIONS <span aria-hidden="true">/</span> CLI REGISTRY</p>
          <h1>Agent 应用</h1>
          <p class="lede">查看本机已安装的 Agent CLI，登记命令与参数模板。本页与登记动作只做检测（只读 <code>--version</code>），<strong>不在此页启动任务</strong>；真正的派发在任务详情页，由执行器用固定提示词调用。</p>
        </div>
        <div class="head-actions">
          <a class="button button--ghost" href="/roles">角色配置</a>
          <a class="button button--primary" href="#manual-agent">手动登记<span class="button-mark" aria-hidden="true">＋</span></a>
        </div>
      </header>

      {notice_html}
      {error_html}
      {issue_html}

      <section class="config-summary" aria-label="Agent 概况">
        <div class="summary-total">
          <span class="summary-kicker">AGENTS / 应用</span>
          <strong class="summary-number">{len(agents):02d}</strong>
          <span class="summary-unit">个已登记</span>
        </div>
        <dl class="config-index">
          <div><dt>本机检测</dt><dd>{len(detected):02d}</dd></div>
          <div><dt>当前可执行</dt><dd>{sum(1 for agent in agents if agent.currently_executable):02d}</dd></div>
          <div><dt>被角色绑定</dt><dd>{sum(1 for agent in agents if agent.id in bindings):02d}</dd></div>
        </dl>
      </section>

      {registered_html}
      {detected_html}

      <section class="config-section" id="manual-agent" aria-labelledby="manual-agent-title">
        <div class="section-heading">
          <h2 class="section-title" id="manual-agent-title">手动登记</h2>
          <span class="section-meta">写 协作/agents/&lt;id&gt;.yaml</span>
        </div>
        <div class="form-layout config-form-layout">
          <form class="config-form" method="post" action="/agents/new">
            <div class="form-ledger" aria-hidden="true">
              <span>手动路径</span>
              <span>本页不执行 Agent</span>
              <span>只探测 --version</span>
            </div>
            {_render_text_field('id', 'Agent ID', '小写字母、数字、- 或 _', form_values.get('id', ''), wide=False, required=True, placeholder='claude-ds', mono=True, maxlength=48)}
            {_render_text_field('name', '显示名称', '用于角色绑定下拉框', form_values.get('name', ''), wide=False, required=True, placeholder='Claude DS', maxlength=MAX_CONFIG_NAME_CHARS)}
            {_render_text_field('command', 'CLI 绝对路径', '登记时只执行 --version（最多 3 秒）', form_values.get('command', ''), wide=True, required=True, placeholder='/Users/me/claude-model/bin/claude-ds', mono=True)}
            {_render_textarea_field('args_template', '参数模板', '执行时把 {prompt} 代入，必须包含一个占位符', form_values.get('args_template', ''), rows=3, mono=True, placeholder='--permission-mode acceptEdits …')}
            {_render_text_field('cwd', '登记 cwd（仅展示）', '执行器固定以任务项目为 cwd；此字段不改执行目录', form_values.get('cwd', ''), wide=True, placeholder=str(PROJECT_DIR), mono=True)}
            {_render_textarea_field('notes', '备注', '登记用途或限制说明', form_values.get('notes', ''), rows=2)}
            <div class="form-actions">
              <button class="button button--signal" type="submit">登记 Agent<span class="button-mark" aria-hidden="true">→</span></button>
            </div>
          </form>
          <aside class="write-note" aria-label="Agent 登记说明">
            <div class="write-note-inner">
              <p class="aside-kicker">PROBE / 探测约定</p>
              <h2>只读版本</h2>
              <p>自动检测与手动登记最多运行 <code>&lt;command&gt; --version</code>，超时 <strong>{VERSION_TIMEOUT_SECONDS} 秒</strong>后立即放弃，不会挂起页面。</p>
              <dl class="scope-list">
                <div><dt>检测位置</dt><dd>~/claude-model/bin/*</dd></div>
                <div><dt>另查</dt><dd>command -v 五个 CLI</dd></div>
                <div><dt>版本截断</dt><dd>{MAX_STORED_VERSION_CHARS} 字符</dd></div>
                <div><dt>派发入口</dt><dd>任务详情页（本页不提供）</dd></div>
              </dl>
            </div>
          </aside>
        </div>
      </section>
    """
    return _render_page("Agent 应用", "agents", content)


def _roles_api_payload() -> dict:
    roles, issues = _load_roles()
    agents, agent_issues = _load_agents()
    agent_map = {agent.id: agent for agent in agents}
    return {
        "generated_at": _now_iso(),
        "count": len(roles),
        "roles_path": _display_path(ROLES_DIR),
        "roles": [
            {
                "id": role.id,
                "name": role.name,
                "description": role.description,
                "agent_id": role.agent_id,
                "agent_name": agent_map[role.agent_id].name if role.agent_id in agent_map else None,
                "write_scope": list(role.write_scope),
                "enabled": role.enabled,
                "notes": role.notes,
                "file": _display_path(role.path),
                "mtime": _iso_time(role.mtime),
                "edit_url": f"/roles/{role.id}",
            }
            for role in roles
        ],
        "issues": [
            {"file": _display_path(issue.path), "message": issue.message} for issue in issues
        ],
        "agent_issues": [
            {"file": _display_path(issue.path), "message": issue.message}
            for issue in agent_issues
        ],
    }


def _agents_api_payload() -> dict:
    agents, issues = _load_agents()
    detected = _detect_agents()
    return {
        "generated_at": _now_iso(),
        "count": len(agents),
        "agents_path": _display_path(AGENTS_DIR),
        "version_timeout_seconds": VERSION_TIMEOUT_SECONDS,
        "version_max_chars": MAX_STORED_VERSION_CHARS,
        "registered": [
            {
                "id": agent.id,
                "name": agent.name,
                "command": agent.command,
                "args_template": agent.args_template,
                "cwd": agent.cwd,
                "installed": agent.installed,
                "currently_executable": agent.currently_executable,
                "version": agent.version,
                "notes": agent.notes,
                "file": _display_path(agent.path),
                "mtime": _iso_time(agent.mtime),
            }
            for agent in agents
        ],
        "detected": [
            {
                "path": item.path,
                "executable": item.executable,
                "version": item.version,
                "source": item.source,
            }
            for item in detected
        ],
        "issues": [
            {"file": _display_path(issue.path), "message": issue.message} for issue in issues
        ],
    }


def _task_revise_api(task: Task) -> Dict[str, object]:
    phase = _task_phase(task)
    can_start = False
    reason = "当前阶段不是「须修正」，无需继续修正。"
    if phase == PHASE_REVISE:
        can_start, reason = _revise_availability(task)
    terminal, _verdict = _review_terminal_state(task)
    target_ids = _review_target_ids(task) if terminal else []
    return {
        "revise_round": task.revise_round,
        "revise_max_rounds": MAX_REVISE_ROUNDS,
        "revise_next_round": _revise_next_round(task),
        "revise_can_start": can_start,
        "revise_disabled_reason": "" if can_start else reason,
        "revise_targets": target_ids,
        "revise_needs_choice": bool(
            phase == PHASE_REVISE
            and terminal
            and task.subtasks
            and not target_ids
        ),
        "revise_whole_rework": bool(task and not task.subtasks),
        "revise_owner_role": task.owner_role,
        "revise_at_limit": task.revise_round >= MAX_REVISE_ROUNDS,
    }


def _tasks_api_payload(tasks: List[Task]) -> dict:
    counts = _summary_counts(tasks)
    return {
        "generated_at": _now_iso(),
        "count": len(tasks),
        "counts": counts,
        "tasks": [
            {
                "id": task.task_id,
                "title": task.title,
                "status": _task_status_from_phase(task, _task_phase(task)),
                "phase": _task_phase(task),
                "phase_label": _phase_label(_task_phase(task)),
                "phase_tone": _phase_tone(_task_phase(task)),
                "mtime": _iso_time(task.mtime),
                "mtime_display": _fmt_time(task.mtime),
                "mtime_epoch": task.mtime,
                "rounds": task.rounds,
                "has_delivery": _delivery_evidence(task)[0],
                "delivery_evidence": _delivery_evidence(task)[1],
                "delivery_folder": task.delivery.folder if task.delivery else None,
                "handoff_blocked": _handoff_blocked_for(task.task_id),
                "handoff": _handoff_state_snapshot(task.task_id),
                "handoff_retry_url": f"/handoff/retry/{task.task_id}",
                "has_review": task.review_path is not None,
                "has_review_done": (task.docking_dir / REVIEW_DONE_NAME).is_file(),
                "review_verdict": _review_terminal_verdict(task),
                "project": task.project_name,
                "project_path": _display_path(task.project_dir),
                "desk_root": str(PROJECT_DIR),
                "workdir": str(task.workdir),
                "workdir_is_custom": task.uses_custom_workdir,
                "owner_role": task.owner_role,
                "meta_path": _display_path(task.meta_path),
                "task_file": task.path.name,
                "task_path": _display_path(task.path),
                "docking_path": _display_path(task.docking_dir),
                "workspace_path": _display_path(task.workspace_dir),
                "docking_file_count": len(_collect_files(task.docking_dir)),
                "workspace_file_count": len(_collect_files(task.workspace_dir)),
                "registration_status": task.registration_status,
                "registered_at": task.registered_at,
                "controller_agent": task.controller_agent,
                "subtask_total": len(task.subtasks),
                "subtask_done": task.subtasks_done,
                "subtasks_total": len(task.subtasks),
                "subtasks_done": task.subtasks_done,
                "subtasks_error": task.subtasks_error,
                **_task_revise_api(task),
                "detail_url": f"/task/{task.task_id}",
            }
            for task in tasks
        ],
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "CollabDesk/2.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._handle_get()
        except Exception:
            traceback.print_exc()
            self._send(
                500,
                _render_error(
                    500,
                    "读取档案时出错",
                    "服务在读取协作档案时遇到错误，本次请求没有修改任何文件。",
                    "刷新页面重试；若仍然失败，请查看启动终端里的错误信息。",
                ),
            )

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._handle_post()
        except Exception:
            traceback.print_exc()
            self._send(
                500,
                _render_error(
                    500,
                    "处理请求时出错",
                    "服务遇到未预期的错误。请查看启动终端里的错误信息。",
                ),
            )

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self._send(
            405,
            _render_error(405, "这个地址不接受该动作", "控制台只提供 GET 与 POST，不提供 HTTP DELETE/PUT/PATCH。"),
            extra_headers=(("Allow", "GET, POST"),),
        )

    def _handle_get(self) -> None:
        if not self._request_allowed():
            return
        path = urlsplit(self.path).path
        normalized = path.rstrip("/") or "/"

        if normalized == "/":
            self._send(200, _render_dashboard(_load_tasks()))
            return

        if normalized == "/new":
            self._send(200, _render_new_page())
            return

        registering_match = re.fullmatch(r"/registering/([^/]+)", normalized)
        if registering_match:
            raw_id = registering_match.group(1)
            if not TASK_ID_RE.fullmatch(raw_id):
                self._send(
                    404,
                    _render_error(
                        404, "任务编号格式不正确", "登记页只接受形如 T09 的任务编号。"
                    ),
                )
                return
            task_id = raw_id.upper()
            project_dir = _find_registration_project(task_id)
            if project_dir is None:
                self._send(
                    404,
                    _render_error(
                        404,
                        "没有找到这次发布",
                        f"{task_id} 不在活动项目目录中。",
                        "重新发布，或返回任务看板查看已登记任务。",
                    ),
                )
                return
            meta = _read_task_meta(project_dir, task_id)
            if (
                str(meta.get("status") or "") == REGISTERED
                and (project_dir / DOCKING_DIRNAME / TASK_SHEET_NAME).is_file()
            ):
                self._redirect(f"/task/{task_id}")
                return
            self._send(200, _render_registering_page(task_id))
            return

        register_status_match = re.fullmatch(
            r"/api/task/([^/]+)/register-status", normalized
        )
        if register_status_match:
            raw_id = register_status_match.group(1)
            if not TASK_ID_RE.fullmatch(raw_id):
                self._send_json(
                    404, {"ok": False, "error": "任务编号格式不正确。"}
                )
                return
            try:
                payload = _registration_payload(raw_id.upper())
            except ActionError as exc:
                self._send_json(
                    exc.status,
                    {
                        "ok": False,
                        "error": exc.title,
                        "message": exc.message,
                    },
                )
                return
            self._send_json(200, payload)
            return

        dispatch_status_match = re.fullmatch(
            r"/api/task/([^/]+)/dispatch-status", normalized
        )
        if dispatch_status_match:
            raw_id = dispatch_status_match.group(1)
            if not TASK_ID_RE.fullmatch(raw_id):
                self._send_json(
                    404, {"ok": False, "error": "任务编号格式不正确。"}
                )
                return
            task_id = raw_id.upper()
            task = _find_task(task_id)
            if task is None:
                self._send_json(
                    404,
                    {
                        "ok": False,
                        "error": "没有找到任务",
                        "message": f"{task_id} 不在 项目/ 或 归档/ 中。",
                    },
                )
                return
            self._send_json(200, _dispatch_status_payload(task))
            return

        review_status_match = re.fullmatch(
            r"/api/task/([^/]+)/review-status", normalized
        )
        if review_status_match:
            raw_id = review_status_match.group(1)
            if not TASK_ID_RE.fullmatch(raw_id):
                self._send_json(
                    404, {"ok": False, "error": "任务编号格式不正确。"}
                )
                return
            task_id = raw_id.upper()
            task = _find_task(task_id)
            if task is None:
                self._send_json(
                    404,
                    {
                        "ok": False,
                        "error": "没有找到任务",
                        "message": f"{task_id} 不在 项目/ 或 归档/ 中。",
                    },
                )
                return
            self._send_json(200, _review_status_payload(task))
            return

        advance_status_match = re.fullmatch(
            r"/api/task/([^/]+)/advance-status", normalized
        )
        if advance_status_match:
            raw_id = advance_status_match.group(1)
            if not TASK_ID_RE.fullmatch(raw_id):
                self._send_json(
                    404, {"ok": False, "error": "任务编号格式不正确。"}
                )
                return
            task_id = raw_id.upper()
            task = _find_task(task_id)
            if task is None:
                self._send_json(
                    404,
                    {
                        "ok": False,
                        "error": "没有找到任务",
                        "message": f"{task_id} 不在 项目/ 或 归档/ 中。",
                    },
                )
                return
            self._send_json(200, _advance_status_payload(task))
            return

        handoff_status_match = re.fullmatch(
            r"/api/task/([^/]+)/handoff-status", normalized
        )
        if handoff_status_match:
            raw_id = handoff_status_match.group(1)
            if not TASK_ID_RE.fullmatch(raw_id):
                self._send_json(
                    404, {"ok": False, "error": "任务编号格式不正确。"}
                )
                return
            task_id = raw_id.upper()
            task = _find_task(task_id)
            if task is None:
                self._send_json(
                    404,
                    {
                        "ok": False,
                        "error": "没有找到任务",
                        "message": f"{task_id} 不在 项目/ 或 归档/ 中。",
                    },
                )
                return
            self._send_json(200, _handoff_status_payload(task))
            return

        revise_status_match = re.fullmatch(
            r"/api/task/([^/]+)/revise-status", normalized
        )
        if revise_status_match:
            raw_id = revise_status_match.group(1)
            if not TASK_ID_RE.fullmatch(raw_id):
                self._send_json(
                    404, {"ok": False, "error": "任务编号格式不正确。"}
                )
                return
            task_id = raw_id.upper()
            task = _find_task(task_id)
            if task is None:
                self._send_json(
                    404,
                    {
                        "ok": False,
                        "error": "没有找到任务",
                        "message": f"{task_id} 不在 项目/ 或 归档/ 中。",
                    },
                )
                return
            self._send_json(200, _revise_status_payload(task))
            return

        if normalized == "/log":
            query = parse_qs(urlsplit(self.path).query)
            raw_path = (query.get("path") or [""])[0]
            try:
                log_path = _runner().resolve_log(raw_path) if raw_path else None
            except Exception:
                log_path = None
            if (
                log_path is None
                or log_path.parent.name != "日志"
                or not log_path.is_file()
            ):
                self._send(
                    404,
                    _render_error(
                        404,
                        "日志未找到",
                        "该路径不是可读取的日志文件，或文件已随项目归档/移动。",
                        "返回任务详情页查看子任务状态。",
                    ),
                )
                return
            try:
                size = log_path.stat().st_size
                with log_path.open("rb") as handle:
                    if size > MAX_LOG_VIEW_BYTES:
                        handle.seek(size - MAX_LOG_VIEW_BYTES)
                    data = handle.read(MAX_LOG_VIEW_BYTES)
            except OSError:
                self._send(
                    404,
                    _render_error(404, "日志未找到", "日志文件无法读取。"),
                )
                return
            text = data.decode("utf-8", errors="replace")
            if size > MAX_LOG_VIEW_BYTES:
                text = "…（仅显示最后 512 KB）\n" + text
            self._send(200, text, content_type="text/plain; charset=utf-8")
            return

        if normalized == "/archive":
            self._send(200, _render_archive_page())
            return

        if normalized == "/dispatch":
            self._send(200, _render_dispatch_page())
            return

        if normalized == "/roles":
            query = parse_qs(urlsplit(self.path).query)
            created = query.get("created", [""])[0]
            archived = query.get("archived", [""])[0]
            notice = ""
            if created:
                notice = f"角色「{_esc(created)}」已创建并写入磁盘。"
            self._send(200, _render_roles_page(notice=notice, archived_id=archived))
            return

        if normalized == "/roles/new":
            agents, _issues = _load_agents()
            self._send(200, _render_role_form(_role_form_values(), role_id=None, agents=agents))
            return

        if normalized == "/agents":
            query = parse_qs(urlsplit(self.path).query)
            created = query.get("created", [""])[0]
            notice = f"Agent「{_esc(created)}」已登记并写入磁盘。" if created else ""
            self._send(200, _render_agents_page(notice=notice))
            return

        if normalized == "/runs":
            query = parse_qs(urlsplit(self.path).query)
            started = query.get("started", [""])[0]
            self._send(200, _render_runs_page(started=started))
            return

        if normalized == "/api/runner":
            payload = json.dumps(_runner_api_payload(), ensure_ascii=False, indent=2)
            self._send(200, payload + "\n", content_type="application/json; charset=utf-8")
            return

        if normalized == "/api/tasks":
            payload = json.dumps(_tasks_api_payload(_load_tasks()), ensure_ascii=False, indent=2)
            self._send(200, payload + "\n", content_type="application/json; charset=utf-8")
            return

        if normalized == "/api/roles":
            payload = json.dumps(_roles_api_payload(), ensure_ascii=False, indent=2)
            self._send(200, payload + "\n", content_type="application/json; charset=utf-8")
            return

        if normalized == "/api/agents":
            payload = json.dumps(_agents_api_payload(), ensure_ascii=False, indent=2)
            self._send(200, payload + "\n", content_type="application/json; charset=utf-8")
            return

        if normalized == "/static/style.css":
            try:
                css = STYLE_FILE.read_text(encoding="utf-8")
            except OSError:
                self._send(404, _render_error(404, "样式表未找到", "static/style.css 不存在。"))
                return
            self._send(200, css, content_type="text/css; charset=utf-8")
            return

        if normalized in {
            "/static/confirm.js",
            "/static/new-task.js",
            "/static/registering.js",
            "/static/dispatch.js",
            "/static/review.js",
            "/static/advance.js",
            "/static/revise.js",
        }:
            script_file = {
                "/static/confirm.js": CONFIRM_SCRIPT_FILE,
                "/static/new-task.js": NEW_TASK_SCRIPT_FILE,
                "/static/registering.js": REGISTERING_SCRIPT_FILE,
                "/static/dispatch.js": DISPATCH_SCRIPT_FILE,
                "/static/review.js": REVIEW_SCRIPT_FILE,
                "/static/advance.js": ADVANCE_SCRIPT_FILE,
                "/static/revise.js": REVISE_SCRIPT_FILE,
            }[normalized]
            try:
                script = script_file.read_text(encoding="utf-8")
            except OSError:
                self._send(
                    404,
                    _render_error(
                        404,
                        "脚本未找到",
                        f"static/{script_file.name} 不存在。",
                    ),
                )
                return
            self._send(200, script, content_type="text/javascript; charset=utf-8")
            return

        role_match = re.fullmatch(r"/roles/([a-z0-9][a-z0-9_-]{0,47})", normalized)
        if role_match:
            role_id = role_match.group(1)
            role = _find_role(role_id)
            if role is None:
                self._send(
                    404,
                    _render_error(
                        404,
                        "没有找到这个角色",
                        f"协作/roles/{_esc(role_id)}.yaml 不存在或无法读取。",
                        "返回角色列表重新选择；无效 YAML 不会被自动改写。",
                    ),
                )
                return
            query = parse_qs(urlsplit(self.path).query)
            notice = ""
            if query.get("created", [""])[0] == "1":
                notice = _render_notice(f"<strong>已创建。</strong> 角色 {_esc(role.name)} 已写入磁盘。")
            elif query.get("saved", [""])[0] == "1":
                notice = _render_notice(f"<strong>已保存。</strong> 角色 {_esc(role.name)} 的配置已更新。")
            agents, _issues = _load_agents()
            self._send(
                200,
                _render_role_form(
                    _role_form_values(role),
                    role_id=role.id,
                    agents=agents,
                    notice=notice,
                ),
            )
            return

        prefix = "/task/"
        if normalized.startswith(prefix):
            raw_id = normalized[len(prefix):]
            if "/" in raw_id or not TASK_ID_RE.fullmatch(raw_id):
                self._send(
                    404,
                    _render_error(404, "没有找到这张任务单", "任务编号格式不正确。", "编号应形如 T01。"),
                )
                return
            task_id = raw_id.upper()
            task = _find_task(task_id)
            if task is None:
                self._send(
                    404,
                    _render_error(
                        404,
                        "没有找到这张任务单",
                        f"{task_id} 不在 项目/ 或 归档/ 的任务单中。",
                        "检查编号，或返回登记簿重新选择。",
                    ),
                )
                return
            query = parse_qs(urlsplit(self.path).query)
            created = query.get("created", [""])[0] == "1"
            workdir_saved = query.get("workdir", [""])[0] == "1"
            review_started = query.get("review", [""])[0] == "1"
            advance_started = query.get("advance", [""])[0] == "1"
            revise_started = query.get("revise", [""])[0] == "1"
            handoff_retried = query.get("handoff", [""])[0]
            self._send(
                200,
                _render_task_detail(
                    task,
                    created=created,
                    workdir_saved=workdir_saved,
                    review_started=review_started,
                    advance_started=advance_started,
                    revise_started=revise_started,
                    handoff_retried=handoff_retried,
                ),
            )
            return

        self._send(
            404,
            _render_error(404, "没有找到这页档案", "这个地址不在控制台的路由表里。", "返回任务看板继续浏览。"),
        )

    def _handle_post(self) -> None:
        if not self._request_allowed():
            return
        if not self._origin_allowed():
            self._send(
                403,
                _render_error(
                    403,
                    "请求来源不被允许",
                    f"写操作只接受来自 {_local_base_url()} 的表单。",
                ),
            )
            return

        path = urlsplit(self.path).path
        normalized = path.rstrip("/") or "/"

        if normalized == "/new":
            self._handle_new_post()
            return

        if normalized == "/publish":
            self._handle_publish_post()
            return

        if normalized == "/api/pick-folder":
            self._handle_pick_folder_post()
            return

        if normalized == "/api/validate-folder":
            self._handle_validate_folder_post()
            return

        retry_match = re.fullmatch(r"/register/([^/]+)/retry", normalized)
        if retry_match:
            raw_id = retry_match.group(1)
            if not TASK_ID_RE.fullmatch(raw_id):
                self._send(
                    404,
                    _render_error(
                        404,
                        "任务编号格式不正确",
                        "重试登记只接受形如 T09 的任务编号。",
                    ),
                )
                return
            self._handle_register_retry_post(raw_id.upper())
            return

        if normalized == "/roles/new":
            self._handle_role_create_post()
            return

        if normalized == "/agents/new":
            self._handle_agent_create_post()
            return

        workdir_match = re.fullmatch(r"/task/([^/]+)/workdir", normalized)
        if workdir_match:
            raw_id = workdir_match.group(1)
            if not TASK_ID_RE.fullmatch(raw_id):
                self._workdir_error_response(
                    404,
                    "任务编号格式不正确",
                    "修改工作目录只接受形如 T07 的任务编号。",
                    "返回任务详情页重新提交。",
                )
                return
            self._handle_workdir_post(raw_id.upper())
            return

        owner_role_match = re.fullmatch(
            r"/task/([^/]+)/owner-role", normalized
        )
        if owner_role_match:
            raw_id = owner_role_match.group(1)
            if not TASK_ID_RE.fullmatch(raw_id):
                self._workdir_error_response(
                    404,
                    "任务编号格式不正确",
                    "修改 owner_role 只接受形如 T07 的任务编号。",
                    "返回任务详情页重新提交。",
                )
                return
            self._handle_owner_role_post(raw_id.upper())
            return

        handoff_retry_match = re.fullmatch(
            r"/handoff/retry/([^/]+)", normalized
        )
        if handoff_retry_match:
            raw_id = handoff_retry_match.group(1)
            if not TASK_ID_RE.fullmatch(raw_id):
                self._send(
                    404,
                    _render_error(
                        404,
                        "任务编号格式不正确",
                        "重试体检只接受形如 T18 的任务编号。",
                        "返回任务详情页重新操作。",
                    ),
                )
                return
            self._handle_handoff_retry_post(raw_id.upper())
            return

        dispatch_match = re.fullmatch(r"/dispatch/([^/]+)", normalized)
        if dispatch_match:
            if not TASK_ID_RE.fullmatch(dispatch_match.group(1)):
                self._send(
                    404,
                    _render_error(
                        404,
                        "任务编号格式不正确",
                        "分发只接受形如 T06 的任务编号。",
                        "返回任务详情页重新操作。",
                    ),
                )
                return
            self._handle_dispatch_post(dispatch_match.group(1).upper())
            return

        review_match = re.fullmatch(r"/review/([^/]+)", normalized)
        if review_match:
            if not TASK_ID_RE.fullmatch(review_match.group(1)):
                self._send(
                    404,
                    _render_error(
                        404,
                        "任务编号格式不正确",
                        "审核只接受形如 T06 的任务编号。",
                        "返回任务详情页重新操作。",
                    ),
                )
                return
            self._handle_review_post(review_match.group(1).upper())
            return

        advance_stop_match = re.fullmatch(r"/advance/([^/]+)/stop", normalized)
        if advance_stop_match:
            if not TASK_ID_RE.fullmatch(advance_stop_match.group(1)):
                self._send(
                    404,
                    _render_error(
                        404,
                        "任务编号格式不正确",
                        "停止推进只接受形如 T06 的任务编号。",
                        "返回任务详情页重新操作。",
                    ),
                )
                return
            self._handle_advance_stop_post(advance_stop_match.group(1).upper())
            return

        advance_match = re.fullmatch(r"/advance/([^/]+)", normalized)
        if advance_match:
            if not TASK_ID_RE.fullmatch(advance_match.group(1)):
                self._send(
                    404,
                    _render_error(
                        404,
                        "任务编号格式不正确",
                        "全自动推进只接受形如 T06 的任务编号。",
                        "返回任务详情页重新操作。",
                    ),
                )
                return
            self._handle_advance_post(advance_match.group(1).upper())
            return

        revise_match = re.fullmatch(r"/revise/([^/]+)", normalized)
        if revise_match:
            if not TASK_ID_RE.fullmatch(revise_match.group(1)):
                self._send(
                    404,
                    _render_error(
                        404,
                        "任务编号格式不正确",
                        "继续修正只接受形如 T06 的任务编号。",
                        "返回任务详情页重新操作。",
                    ),
                )
                return
            self._handle_revise_post(revise_match.group(1).upper())
            return

        role_delete = re.fullmatch(
            r"/roles/([a-z0-9][a-z0-9_-]{0,47})/delete", normalized
        )
        if role_delete:
            self._handle_role_delete_post(role_delete.group(1))
            return

        role_edit = re.fullmatch(
            r"/roles/([a-z0-9][a-z0-9_-]{0,47})", normalized
        )
        if role_edit:
            self._handle_role_edit_post(role_edit.group(1))
            return

        for prefix, mover, redirect_to in (
            ("/archive/", _archive_project, "/"),
            ("/restore/", _restore_project, "/archive"),
        ):
            if not normalized.startswith(prefix):
                continue
            raw_id = normalized[len(prefix):]
            if not raw_id or "/" in raw_id or not TASK_ID_RE.fullmatch(raw_id):
                self._send(
                    404,
                    _render_error(
                        404,
                        "任务编号格式不正确",
                        "归档与恢复只接受形如 T03 的任务编号。",
                        "返回看板或归档库重新选择任务。",
                    ),
                )
                return
            try:
                fields = self._read_action_form()
                mover(raw_id.upper(), fields.get("project", ""))
            except FormError as exc:
                self._send(
                    400,
                    _render_error(
                        400,
                        "归档请求格式不正确",
                        str(exc),
                        "请从看板或归档库页面重新提交。",
                    ),
                )
                return
            except ActionError as exc:
                self._send(
                    exc.status,
                    _render_error(exc.status, exc.title, exc.message, exc.hint),
                )
                return
            self._redirect(redirect_to)
            return

        self._send(
            404,
            _render_error(
                404,
                "没有可写入的地址",
                "控制台只接受任务单、分发执行、归档/恢复、角色与 Agent 登记的 POST；"
                "守卫启停由菜单栏应用管理。",
            ),
        )

    def _handle_role_create_post(self) -> None:
        agents, _issues = _load_agents()
        fields: Dict[str, str] = {}
        try:
            fields = self._read_config_form()
            values = _build_role_values(fields)
            role_id = _save_new_role(fields)
        except (FormError, ConfigError) as exc:
            values = _build_role_values(fields)
            self._send(
                400,
                _render_role_form(values, role_id=None, agents=agents, error=str(exc)),
            )
            return
        self._redirect(f"/roles/{role_id}?created=1")

    def _handle_role_edit_post(self, role_id: str) -> None:
        agents, _issues = _load_agents()
        fields: Dict[str, str] = {}
        try:
            fields = self._read_config_form()
            values = _build_role_values(fields, role_id)
            if _find_role(role_id) is None:
                raise FormError("角色文件不存在，未写入任何内容。")
            _save_existing_role(role_id, fields)
        except (FormError, ConfigError) as exc:
            values = _build_role_values(fields, role_id)
            self._send(
                400,
                _render_role_form(
                    values, role_id=role_id, agents=agents, error=str(exc)
                ),
            )
            return
        self._redirect(f"/roles/{role_id}?saved=1")

    def _handle_role_delete_post(self, role_id: str) -> None:
        try:
            _move_role_to_archive(role_id)
        except FormError as exc:
            self._send(
                400,
                _render_error(
                    400,
                    "角色没有移出",
                    str(exc),
                    "返回角色列表；原配置未被改写或删除。",
                ),
            )
            return
        self._redirect(f"/roles?archived={role_id}")

    def _handle_agent_create_post(self) -> None:
        fields: Dict[str, str] = {}
        try:
            fields = self._read_config_form()
            values = _build_agent_values(fields)
            agent_id = _save_new_agent(fields)
        except (FormError, ConfigError) as exc:
            values = _build_agent_values(fields)
            self._send(400, _render_agents_page(values=values, error=str(exc)))
            return
        self._redirect(f"/agents?created={agent_id}")

    def _handle_workdir_post(self, task_id: str) -> None:
        task = _find_task(task_id)
        try:
            if task is None:
                raise ActionError(
                    404,
                    "没有找到这张任务单",
                    f"{task_id} 不在 项目/ 或 归档/ 的任务单中。",
                    "检查编号，或返回看板重新选择。",
                )
            if task.archived:
                raise ActionError(
                    409,
                    "归档任务只读",
                    f"{task_id} 位于 归档/，不能修改工作目录。",
                    "先从归档库恢复项目，再修改工作目录。",
                )
            if _active_dispatch_task() == task_id:
                raise ActionError(
                    409,
                    "任务正在分发",
                    f"{task_id} 正在按子任务串行分发，本次不修改工作目录。",
                    "等待分发结束后再修改。",
                )
            if _review_active_for(task_id):
                raise ActionError(
                    409,
                    "任务正在审核",
                    f"{task_id} 正在由审核角色复核，本次不修改工作目录。",
                    "等待审核结束后再修改。",
                )
            if _active_handoff_task() == task_id.upper():
                raise ActionError(
                    409,
                    "任务正在交接体检",
                    f"{task_id} 正在检查/补交交接文件，本次不修改工作目录。",
                    "等待交接体检结束后再修改。",
                )
            if _active_advance_task() == task_id.upper():
                raise ActionError(
                    409,
                    "任务正在全自动推进",
                    f"{task_id} 正在自动分发/审核，本次不修改工作目录。",
                    "等待推进结束后再修改。",
                )
            fields = self._read_urlencoded_form()
            new_workdir = _update_task_workdir(task, fields.get("workdir", ""))
        except ActionError as exc:
            self._workdir_error_response(
                exc.status, exc.title, exc.message, exc.hint
            )
            return
        except FormError as exc:
            self._workdir_error_response(
                400,
                "工作目录没有更新",
                str(exc),
                "填写绝对路径；系统根内只允许 `项目/<编号>_<短名>/工作区/`。",
            )
            return

        if self._wants_json():
            self._send_json(
                200,
                {
                    "ok": True,
                    "task": task_id,
                    "desk_root": str(PROJECT_DIR),
                    "workdir": str(new_workdir),
                    "meta": _display_path(task.meta_path),
                },
            )
            return
        self._redirect(f"/task/{task_id}?workdir=1")

    def _handle_owner_role_post(self, task_id: str) -> None:
        task = _find_task(task_id)
        try:
            if task is None:
                raise ActionError(
                    404,
                    "没有找到这张任务单",
                    f"{task_id} 不在 项目/ 或 归档/ 的任务单中。",
                    "检查编号，或返回看板重新选择。",
                )
            fields = self._read_urlencoded_form()
            owner_role = _update_task_owner_role(
                task, fields.get("owner_role", "")
            )
        except ActionError as exc:
            self._workdir_error_response(
                exc.status, exc.title, exc.message, exc.hint
            )
            return
        except FormError as exc:
            self._workdir_error_response(
                400,
                "owner_role 没有更新",
                str(exc),
                "填写 协作/roles/ 中已存在的角色 id；默认 designer。",
            )
            return

        if self._wants_json():
            self._send_json(
                200,
                {
                    "ok": True,
                    "task": task_id,
                    "owner_role": owner_role,
                    "meta": _display_path(task.meta_path),
                },
            )
            return
        self._redirect(f"/task/{task_id}")

    def _workdir_error_response(
        self, status: int, title: str, message: str, hint: str = ""
    ) -> None:
        if self._wants_json():
            self._send_json(
                status, {"ok": False, "error": title, "message": message}
            )
            return
        self._send(status, _render_error(status, title, message, hint))

    def _handle_handoff_retry_post(self, task_id: str) -> None:
        try:
            task = _find_task(task_id)
            if task is None:
                raise ActionError(
                    404,
                    "没有找到任务",
                    f"{task_id} 不在 项目/ 或 归档/ 中。",
                    "返回任务看板确认编号与项目状态。",
                )
            result = _retry_handoff(task)
        except ActionError as exc:
            self._dispatch_error_response(exc)
            return
        except Exception as exc:
            traceback.print_exc()
            self._dispatch_error_response(
                ActionError(
                    500,
                    "重试体检失败",
                    f"服务遇到未预期的错误：{exc}",
                    "查看控制台终端错误信息后重试。",
                )
            )
            return

        passed = bool(result.get("passed"))
        if self._wants_json():
            self._send_json(
                200 if passed else 409,
                {
                    **result,
                    "message": (
                        "交接体检通过，交接阻塞已解除。"
                        if passed
                        else "交接体检仍有缺件，已保持阻塞。"
                    ),
                    "status_url": f"/api/task/{task_id}/handoff-status",
                    "retry_url": f"/handoff/retry/{task_id}",
                },
            )
            return
        self._redirect(
            f"/task/{task_id}?handoff={'passed' if passed else 'blocked'}"
        )

    def _handle_dispatch_post(self, task_id: str) -> None:
        try:
            task = _find_task(task_id)
            if task is None:
                raise ActionError(
                    404,
                    "没有找到可分发的任务",
                    f"{task_id} 不在 项目/*/对接/任务单.md 中。",
                    "返回任务看板确认编号与项目状态。",
                )
            state = _start_dispatch(task)
        except ActionError as exc:
            self._dispatch_error_response(exc)
            return
        except Exception as exc:
            traceback.print_exc()
            self._dispatch_error_response(
                ActionError(
                    500,
                    "分发未能启动",
                    f"服务遇到未预期的错误：{exc}",
                    "查看控制台终端错误信息后重试。",
                )
            )
            return

        if self._wants_json():
            self._send_json(
                202,
                {
                    "ok": True,
                    "message": "已开始分发实现；系统会按角色绑定串行执行 pending 子任务。",
                    "dispatch": state,
                    "status_url": f"/api/task/{task_id}/dispatch-status",
                },
            )
            return
        self._redirect(f"/task/{task_id}?dispatch=1")

    def _dispatch_error_response(self, exc: ActionError) -> None:
        if self._wants_json():
            payload: Dict[str, object] = {
                "ok": False,
                "error": exc.title,
                "message": exc.message,
            }
            if exc.violations:
                payload["violations"] = exc.violations
            payload.update(exc.extra)
            self._send_json(exc.status, payload)
            return
        self._send(
            exc.status,
            _render_error(exc.status, exc.title, exc.message, exc.hint),
        )

    def _handle_review_post(self, task_id: str) -> None:
        try:
            task = _find_task(task_id)
            if task is None:
                raise ActionError(
                    404,
                    "没有找到可审核的任务",
                    f"{task_id} 不在 项目/*/对接/任务单.md 中。",
                    "返回任务看板确认编号与项目状态。",
                )
            state = _start_review(task)
        except ActionError as exc:
            self._dispatch_error_response(exc)
            return
        except Exception as exc:
            traceback.print_exc()
            self._dispatch_error_response(
                ActionError(
                    500,
                    "审核未能启动",
                    f"服务遇到未预期的错误：{exc}",
                    "查看控制台终端错误信息后重试。",
                )
            )
            return

        if self._wants_json():
            self._send_json(
                202,
                {
                    "ok": True,
                    "message": "已开始审核；审核角色会复核全部子任务并写审查结论。",
                    "review": state,
                    "status_url": f"/api/task/{task_id}/review-status",
                },
            )
            return
        self._redirect(f"/task/{task_id}?review=1")

    def _handle_advance_post(self, task_id: str) -> None:
        try:
            task = _find_task(task_id)
            if task is None:
                raise ActionError(
                    404,
                    "没有找到可推进的任务",
                    f"{task_id} 不在 项目/*/对接/任务单.md 中。",
                    "返回任务看板确认编号与项目状态。",
                )
            state = _start_advance(task)
        except ActionError as exc:
            self._dispatch_error_response(exc)
            return
        except Exception as exc:
            traceback.print_exc()
            self._dispatch_error_response(
                ActionError(
                    500,
                    "全自动推进未能启动",
                    f"服务遇到未预期的错误：{exc}",
                    "查看控制台终端错误信息后重试。",
                )
            )
            return

        if self._wants_json():
            self._send_json(
                202,
                {
                    "ok": True,
                    "message": "已启动全自动推进；将分发剩余子任务，全部交付后自动审核。",
                    "advance": state,
                    "status_url": f"/api/task/{task_id}/advance-status",
                },
            )
            return
        self._redirect(f"/task/{task_id}?advance=1")

    def _handle_advance_stop_post(self, task_id: str) -> None:
        try:
            task = _find_task(task_id)
            if task is None:
                raise ActionError(
                    404,
                    "没有找到可停止的任务",
                    f"{task_id} 不在 项目/*/对接/任务单.md 中。",
                    "返回任务看板确认编号与项目状态。",
                )
            final = _stop_advance(task.task_id)
        except ActionError as exc:
            self._dispatch_error_response(exc)
            return
        except Exception as exc:
            traceback.print_exc()
            self._dispatch_error_response(
                ActionError(
                    500,
                    "停止推进时出错",
                    f"服务遇到未预期的错误：{exc}",
                    "刷新详情页查看状态；如仍有锁，请检查 /runs。",
                )
            )
            return

        if self._wants_json():
            self._send_json(
                200,
                {
                    "ok": True,
                    "message": "已停止全自动推进；当前 Agent 已终止，执行器锁已检查。",
                    "advance": final,
                    "status_url": f"/api/task/{task_id}/advance-status",
                },
            )
            return
        self._redirect(f"/task/{task_id}")

    def _handle_revise_post(self, task_id: str) -> None:
        mode = ""
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length > 0:
            try:
                mode = self._read_urlencoded_form().get("mode", "")
            except FormError as exc:
                if self._wants_json():
                    self._send_json(400, {"ok": False, "error": "表单无效", "message": str(exc)})
                else:
                    self._send(400, _render_error(400, "表单无效", str(exc)))
                return
        try:
            task = _find_task(task_id)
            if task is None:
                raise ActionError(
                    404,
                    "没有找到可继续修正的任务",
                    f"{task_id} 不在 项目/*/对接/任务单.md 中。",
                    "返回任务看板确认编号与项目状态。",
                )
            result = _start_revise(task, mode)
        except ActionError as exc:
            self._dispatch_error_response(exc)
            return
        except Exception as exc:
            traceback.print_exc()
            self._dispatch_error_response(
                ActionError(
                    500,
                    "继续修正未能启动",
                    f"服务遇到未预期的错误：{exc}",
                    "查看控制台终端错误信息后重试。",
                )
            )
            return

        if self._wants_json():
            payload = dict(result)
            payload["message"] = (
                f"已启动继续修正（第 {result.get('revise_round')} 轮）；"
                "将重新分发受影响子任务并自动审核。"
            )
            self._send_json(202, payload)
            return
        self._redirect(f"/task/{task_id}?revise=1")

    def _wants_json(self) -> bool:
        accept = (self.headers.get("Accept") or "").lower()
        return "application/json" in accept and "text/html" not in accept

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self._send(status, body, content_type="application/json; charset=utf-8")

    def _handle_publish_post(self) -> None:
        values: Dict[str, str] = {name: "" for name in PUBLISH_FIELDS}
        try:
            incoming = self._read_urlencoded_form()
            for name in PUBLISH_FIELDS:
                values[name] = incoming.get(name, "")
            created = _create_published_task(values)
        except FormError as exc:
            self._publish_error_response(values, str(exc))
            return

        task_id = str(created["task_id"])
        project_dir = created["project_dir"]
        assert isinstance(project_dir, Path)
        controller = created["controller"]
        assert isinstance(controller, AgentApp)
        title = str(created.get("title") or "")
        try:
            _start_registration(
                task_id=task_id,
                project_dir=project_dir,
                title_hint=title,
                controller=controller,
            )
        except Exception as exc:
            if not isinstance(exc, FormError):
                traceback.print_exc()
            meta = _read_task_meta(project_dir, task_id, title=title)
            if str(meta.get("status") or "") != REGISTER_FAILED:
                _mark_registration_failed(
                    project_dir,
                    task_id,
                    str(exc),
                    "",
                    controller=controller.id,
                )
            if self._wants_json():
                self._send_json(
                    502,
                    {
                        "ok": False,
                        "error": "总控登记未能启动",
                        "message": str(exc),
                        "id": task_id,
                        "status": REGISTER_FAILED,
                        "status_url": f"/api/task/{task_id}/register-status",
                        "register_url": f"/registering/{task_id}",
                        "retry_url": f"/register/{task_id}/retry",
                    },
                )
                return
            self._redirect(f"/registering/{task_id}")
            return

        if self._wants_json():
            self._send_json(
                202,
                {
                    "ok": True,
                    "id": task_id,
                    "status": REGISTERING,
                    "title": title,
                    "project": str(created.get("project_name") or ""),
                    "project_path": _display_path(project_dir),
                    "workdir": str(created.get("workdir") or ""),
                    "controller": controller.id,
                    "idea_path": _display_path(
                        project_dir / DOCKING_DIRNAME / IDEA_NAME
                    ),
                    "status_url": f"/api/task/{task_id}/register-status",
                    "task_url": f"/task/{task_id}",
                    "register_url": f"/registering/{task_id}",
                },
            )
            return
        self._redirect(f"/registering/{task_id}")

    def _publish_error_response(self, values: Dict[str, str], message: str) -> None:
        if self._wants_json():
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": "没有发布",
                    "message": message,
                },
            )
            return
        self._send(400, _render_new_page(values, message))

    def _handle_register_retry_post(self, task_id: str) -> None:
        project_dir = _find_registration_project(task_id)
        if project_dir is None:
            message = f"{task_id} 不在活动项目目录中。"
            if self._wants_json():
                self._send_json(404, {"ok": False, "error": message})
            else:
                self._send(404, _render_error(404, "没有找到这次发布", message))
            return
        meta = _read_task_meta(project_dir, task_id)
        status = str(meta.get("status") or "")
        if status == REGISTERED:
            message = f"{task_id} 已经登记完成，不需要重试。"
            if self._wants_json():
                self._send_json(409, {"ok": False, "error": message})
            else:
                self._send(409, _render_error(409, "已经登记完成", message))
            return
        if status != REGISTER_FAILED:
            message = f"{task_id} 当前状态为 {status or '未知'}，暂时不能重试。"
            if self._wants_json():
                self._send_json(409, {"ok": False, "error": message})
            else:
                self._send(409, _render_error(409, "登记尚未结束", message))
            return
        title_hint = str(meta.get("title") or "")
        try:
            _start_registration(
                task_id=task_id,
                project_dir=project_dir,
                title_hint=title_hint,
            )
        except Exception as exc:
            if not isinstance(exc, FormError):
                traceback.print_exc()
                current = _read_task_meta(project_dir, task_id, title=title_hint)
                if str(current.get("status") or "") == REGISTERING:
                    _mark_registration_failed(
                        project_dir,
                        task_id,
                        f"重试登记未能启动：{exc}",
                        "",
                    )
            if self._wants_json():
                self._send_json(
                    409,
                    {
                        "ok": False,
                        "error": "不能重试登记",
                        "message": str(exc),
                        "register_url": f"/registering/{task_id}",
                    },
                )
            else:
                self._send(
                    409,
                    _render_error(
                        409,
                        "不能重试登记",
                        str(exc),
                        "检查执行器是否空闲、总控 Agent 是否可用，然后回到登记页重试。",
                    ),
                )
            return
        if self._wants_json():
            self._send_json(
                202,
                {
                    "ok": True,
                    "id": task_id,
                    "status": REGISTERING,
                    "status_url": f"/api/task/{task_id}/register-status",
                    "register_url": f"/registering/{task_id}",
                },
            )
            return
        self._redirect(f"/registering/{task_id}")

    def _handle_validate_folder_post(self) -> None:
        try:
            fields = self._read_urlencoded_form()
            folder = _validate_project_folder(fields.get("workdir", ""))
        except FormError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._send_json(200, {"ok": True, "path": str(folder), "exists": True})

    def _handle_pick_folder_post(self) -> None:
        if not shutil.which("osascript"):
            self._send_json(
                503,
                {
                    "ok": False,
                    "error": "本机没有 osascript，无法打开原生选择框。",
                    "hint": "请直接在文件夹输入框填写绝对路径。",
                },
            )
            return
        try:
            result = subprocess.run(
                ["osascript", "-e", PICK_FOLDER_SCRIPT],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=PICK_FOLDER_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            self._send_json(
                504,
                {
                    "ok": False,
                    "error": "选择文件夹等待超时。",
                    "hint": "请直接在输入框填写绝对路径。",
                },
            )
            return
        except OSError as exc:
            self._send_json(
                503,
                {
                    "ok": False,
                    "error": f"无法调用系统选择框：{exc.strerror or exc}",
                    "hint": "请直接在输入框填写绝对路径。",
                },
            )
            return

        output = (result.stdout or "").strip()
        if result.returncode != 0 or not output:
            detail = " ".join((result.stderr or "").split())[:300]
            cancelled = "cancel" in detail.lower() or "-128" in detail
            self._send_json(
                400,
                {
                    "ok": False,
                    "cancelled": cancelled,
                    "error": "已取消选择文件夹。" if cancelled else "原生选择框没有返回路径。",
                    "detail": detail,
                    "hint": "也可以直接在输入框手动填写绝对路径。",
                },
            )
            return

        try:
            selected = Path(output)
            if not selected.is_absolute():
                raise ValueError("返回的不是绝对路径")
            path = selected.resolve()
        except (OSError, ValueError) as exc:
            self._send_json(
                400,
                {
                    "ok": False,
                    "error": f"选择框返回的路径无法解析：{exc}",
                    "hint": "请直接在输入框手动填写绝对路径。",
                },
            )
            return

        valid = True
        error = ""
        try:
            path = _validate_project_folder(str(path))
        except FormError as exc:
            valid = False
            error = str(exc)
        self._send_json(
            200,
            {
                "ok": True,
                "path": str(path),
                "valid": valid,
                "error": error,
            },
        )

    def _handle_new_post(self) -> None:
        values = _blank_form()
        try:
            values.update(self._read_urlencoded_form())
            task_id = _create_task(values)
        except FormError as exc:
            self._send(400, _render_new_page(values, str(exc)))
            return
        self._redirect(f"/task/{task_id}?created=1")

    def _read_config_form(self) -> Dict[str, str]:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise FormError("表单格式无效，请从控制台页面提交。")
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or "0")
        except ValueError as exc:
            raise FormError("请求长度无效。") from exc
        if length < 0 or length > MAX_CONFIG_FORM_BYTES:
            raise FormError("表单内容过大，未写入配置。")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise FormError("表单传输不完整，请重试。")
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FormError("表单不是有效的 UTF-8 文本。") from exc
        parsed = parse_qs(decoded, keep_blank_values=True)
        if len(parsed) > 64:
            raise FormError("表单字段过多，未写入配置。")
        return {name: values[-1] for name, values in parsed.items()}

    def _read_urlencoded_form(self) -> Dict[str, str]:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise FormError("表单格式无效，请从控制台页面提交。")

        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or "0")
        except ValueError as exc:
            raise FormError("请求长度无效。") from exc
        if length < 0 or length > MAX_FORM_BYTES:
            raise FormError("表单内容过大，未写入任务单。")

        raw = self.rfile.read(length)
        if len(raw) != length:
            raise FormError("表单传输不完整，请重试。")
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FormError("表单不是有效的 UTF-8 文本。") from exc

        parsed = parse_qs(decoded, keep_blank_values=True)
        return {
            name: parsed.get(name, [""])[-1]
            for name in FORM_FIELDS
            if name in parsed
        }

    def _read_action_form(self) -> Dict[str, str]:
        """归档/恢复表单可选带 project 字段；空表单等价于按编号取最新项目。"""
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or "0")
        except ValueError as exc:
            raise FormError("请求长度无效。") from exc
        if length < 0 or length > MAX_FORM_BYTES:
            raise FormError("表单内容过大，未移动任何目录。")
        if length == 0:
            return {}

        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise FormError("表单格式无效，请从控制台页面提交。")

        raw = self.rfile.read(length)
        if len(raw) != length:
            raise FormError("表单传输不完整，未移动任何目录。")
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FormError("表单不是有效的 UTF-8 文本。") from exc

        parsed = parse_qs(decoded, keep_blank_values=True)
        return {"project": parsed.get("project", [""])[-1]}

    def _request_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in _allowed_hosts():
            self._send(
                403,
                _render_error(
                    403,
                    "只允许本机访问",
                    f"请通过 {_local_base_url()} 打开控制台。",
                ),
            )
            return False
        return True

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin.strip().lower() in _allowed_origins()

    def _redirect(self, location: str) -> None:
        body = (
            "<!DOCTYPE html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            f'<p>已处理。<a href="{_esc(location)}">继续</a></p></html>'
        )
        self._send(303, body, extra_headers=(("Location", location),))

    def _send(
        self,
        status: int,
        body: str,
        content_type: str = "text/html; charset=utf-8",
        extra_headers: Tuple[Tuple[str, str], ...] = (),
    ) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("Connection", "close")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.close_connection = True
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] {self.address_string()} {fmt % args}")


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    global PORT, RUNNER, RUNNER_TIMEOUT_SECONDS, REGISTRATION_TIMEOUT_SECONDS
    global ADVANCE_MAX_RUNTIME_SECONDS, ADVANCE_MAX_SUBTASKS
    global ADVANCE_MAX_REVISE_ROUNDS
    parser = argparse.ArgumentParser(description="协作值班台本地控制台")
    parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="监听端口，默认 8787；临时验收可用 8789",
    )
    parser.add_argument(
        "--run-timeout",
        type=int,
        default=DEFAULT_RUN_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"执行器单次运行超时，默认 {DEFAULT_RUN_TIMEOUT_SECONDS} 秒",
    )
    parser.add_argument(
        "--register-timeout",
        type=int,
        default=DEFAULT_REGISTRATION_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"总控登记调用超时，默认 {DEFAULT_REGISTRATION_TIMEOUT_SECONDS} 秒",
    )
    parser.add_argument(
        "--advance-max-runtime",
        type=int,
        default=int(ADVANCE_MAX_RUNTIME_SECONDS),
        metavar="SECONDS",
        help=(
            "全自动推进最长运行时长，默认 "
            f"{int(ADVANCE_MAX_RUNTIME_SECONDS)} 秒（2 小时）"
        ),
    )
    parser.add_argument(
        "--advance-max-subtasks",
        type=int,
        default=ADVANCE_MAX_SUBTASKS,
        metavar="N",
        help=f"全自动推进最多启动的子任务数，默认 {ADVANCE_MAX_SUBTASKS}",
    )
    parser.add_argument(
        "--advance-max-revise-rounds",
        type=int,
        default=ADVANCE_MAX_REVISE_ROUNDS,
        metavar="N",
        help=f"全自动推进最多修订轮次，默认 {ADVANCE_MAX_REVISE_ROUNDS}",
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("--port 必须在 1024–65535 之间")
    if not 1 <= args.run_timeout <= MAX_RUN_TIMEOUT_SECONDS:
        parser.error(
            f"--run-timeout 必须在 1–{MAX_RUN_TIMEOUT_SECONDS} 秒之间"
        )
    if not 1 <= args.register_timeout <= MAX_REGISTRATION_TIMEOUT_SECONDS:
        parser.error(
            f"--register-timeout 必须在 1–{MAX_REGISTRATION_TIMEOUT_SECONDS} 秒之间"
        )
    if not 1 <= args.advance_max_runtime:
        parser.error("--advance-max-runtime 必须是正整数秒")
    if not 1 <= args.advance_max_subtasks:
        parser.error("--advance-max-subtasks 必须是正整数")
    if not 1 <= args.advance_max_revise_rounds:
        parser.error("--advance-max-revise-rounds 必须是正整数")
    PORT = args.port
    RUNNER_TIMEOUT_SECONDS = float(args.run_timeout)
    REGISTRATION_TIMEOUT_SECONDS = float(args.register_timeout)
    ADVANCE_MAX_RUNTIME_SECONDS = float(args.advance_max_runtime)
    ADVANCE_MAX_SUBTASKS = int(args.advance_max_subtasks)
    ADVANCE_MAX_REVISE_ROUNDS = int(args.advance_max_revise_rounds)
    RUNNER = runner_engine.Runner(PROJECT_DIR, timeout_seconds=RUNNER_TIMEOUT_SECONDS)
    reconciled = RUNNER.reconcile()
    if reconciled is not None:
        print(
            "[执行器] 已把上次未收尾的运行标记为 failed；锁已检查并释放。"
        )
    stale_registrations = _reconcile_stale_registrations()
    if stale_registrations:
        print(
            f"[登记] 已把 {stale_registrations} 个上次未收尾的登记标记为 failed；可重试。"
        )
    interrupted_subtasks = _reconcile_interrupted_subtasks()
    if interrupted_subtasks:
        print(
            f"[分发] 已把 {interrupted_subtasks} 个上次未收尾的子任务标记为 "
            "failed（interrupted）；可继续分发重试。"
        )

    created = _ensure_default_roles()
    if created:
        print("已生成默认角色：" + "、".join(path.stem for path in created))

    def _request_stop(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _request_stop)
    except (ValueError, OSError):
        pass

    server = ConsoleServer((HOST, PORT), ConsoleHandler)
    print(f"协作值班台已启动：{_local_base_url()}")
    print(
        f"只监听本机回环地址；执行器单实例、超时 {RUNNER_TIMEOUT_SECONDS:g} 秒；"
        "按 Ctrl+C 停止。"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止...")
    finally:
        try:
            RUNNER.shutdown()
        except Exception:
            traceback.print_exc()
        server.server_close()
        print("已停止。")


if __name__ == "__main__":
    main()
