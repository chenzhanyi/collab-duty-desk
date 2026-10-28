"""Shared project-workspace policy; agents retain their own CLI permissions."""
from pathlib import Path
import json
import os

ISOLATED = "isolated"
PROJECT = "project"
SELF = "self"
MODES = {
    PROJECT: "修改项目源码",
    SELF: "维护工作台自身",
    ISOLATED: "生成独立产物",
}
DISPATCH_SOURCES = {"watch.sh", "ack.sh", "since.sh", "rounds.sh", "state.py"}
IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"}
MANIFEST = "changes.json"


def mode(value):
    result = str(value or ISOLATED).strip()
    if result not in MODES:
        raise ValueError("工作方式无效，请重新选择。")
    return result


def source_mode(value):
    return value in {PROJECT, SELF}


def within(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    while True:
        if path == root:
            return True
        try:
            if os.path.samefile(path, root):
                return True
        except OSError:
            pass
        parent = path.parent
        if parent == path:
            return False
        path = parent


def validate_target(path, desk_root, work_mode):
    work_mode = mode(work_mode)
    path, root = Path(path).resolve(), Path(desk_root).resolve()
    same = path == root
    try:
        same = same or os.path.samefile(path, root)
    except OSError:
        pass
    if work_mode == SELF:
        if not same:
            raise ValueError("维护工作台自身时，项目目录必须是当前工作台根目录。")
        return
    if same:
        raise ValueError("这是当前工作台；请先选择“维护工作台自身”，再使用此目录。")
    if work_mode == PROJECT and within(path, root):
        raise ValueError("项目源码应选择工作台外的项目根目录；修改本系统请选择“维护工作台自身”。")


def protected_file(path, desk_root):
    """Runtime data, personal routing and generated files are not source delivery."""
    path, root = Path(path).resolve(), Path(desk_root).resolve()
    if not within(path, root):
        return False
    current, names = path, []
    while current != root:
        try:
            if os.path.samefile(current, root):
                break
        except OSError:
            pass
        names.insert(0, current.name)
        current = current.parent
    parts = tuple(name.casefold() for name in names)
    if not parts:
        return False
    if any(part in IGNORED_DIRS for part in parts):
        return True
    if parts[0] in {"项目", "归档"} or parts[:2] in {("desktop", "build"), ("协作", "审计"), ("协作", "roles")}:
        return True
    if parts[:2] == ("协作", "agents"):
        return len(parts) < 3 or parts[2] != "bin"
    if parts[:2] == ("协作", "调度台"):
        return len(parts) != 3 or parts[2] not in DISPATCH_SOURCES
    return False


def source_file_problem(raw, workdir, desk_root):
    if not isinstance(raw, str) or not raw.strip() or any(ord(c) < 32 for c in raw):
        return "文件路径必须是非空的项目相对路径"
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return "文件路径不能越出项目目录"
    if any(part.casefold() in IGNORED_DIRS for part in relative.parts):
        return "缓存和 Git 元数据不能作为源码交付"
    root = Path(workdir).resolve()
    candidate = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return "交付路径不能经过符号链接"
    if not within(candidate, root):
        return "文件路径不能越出项目目录"
    if protected_file(candidate, desk_root):
        return "运行数据、任务档案和本机 Agent 配置不能作为源码交付"
    if not candidate.is_file():
        return "清单中的源码文件不存在"
    return ""


def manifest_problems(path, workdir, desk_root):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return ["缺少有效的 changes.json 交付清单"]
    try:
        if path.stat().st_size > 256 * 1024:
            return ["changes.json 超过 256 KiB"]
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return ["changes.json 不是有效 JSON"]
    if not isinstance(data, dict) or not isinstance(data.get("files"), list) or not isinstance(data.get("checks"), list):
        return ["changes.json 必须包含 files 路径列表和 checks 验证记录列表"]
    if len(data["files"]) > 2000 or len(data["checks"]) > 1000:
        return ["changes.json 清单过长"]
    problems = []
    for raw in data["files"]:
        problem = source_file_problem(raw, workdir, desk_root)
        if problem:
            problems.append(f"交付文件 {str(raw)[:180]}：{problem}")
    if not data["checks"] or any(not isinstance(item, str) or not item.strip() for item in data["checks"]):
        problems.append("changes.json 的 checks 需要写明实际运行的命令与结果")
    return problems
