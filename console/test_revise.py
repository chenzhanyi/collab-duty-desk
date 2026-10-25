#!/usr/bin/env python3
"""T17 继续修正（重修）：详情页按钮、受影响子任务重置、审查历史与轮次上限。

所有测试都在临时仓库运行，使用假 Agent，不触碰真实 项目/、协作/ 或 8787。
"""

from __future__ import annotations

import http.client
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import runner  # noqa: E402
import server  # noqa: E402

GLOBALS = (
    "PROJECT_DIR",
    "COLLAB_DIR",
    "PROJECTS_DIR",
    "ARCHIVE_DIR",
    "DISPATCH_DIR",
    "ROLES_DIR",
    "AGENTS_DIR",
    "ROLES_ARCHIVE_DIR",
    "WATCH_STATE",
    "ROUNDS_TSV",
    "JOURNAL_LOG",
    "GUARD_STATUS",
    "RUNNER",
    "REGISTRATION_TIMEOUT_SECONDS",
    "RUNNER_TIMEOUT_SECONDS",
    "PORT",
)

FAKE_AGENT = r'''#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
mode = os.environ.get("T17_MODE", "pass")
issue_mode = os.environ.get("T17_ISSUES", "first")
order = os.environ.get("T17_ORDER_FILE", "")
held = os.environ.get("T17_HELD_FILE", "")
sleep_seconds = float(os.environ.get("T17_AGENT_SLEEP", "0.2"))


def record(text):
    if order:
        with open(order, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
            handle.flush()


fd = None
if held:
    try:
        fd = os.open(held, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        record("overlap pid=%d" % os.getpid())
        print("OVERLAP")
        raise SystemExit(3)

try:
    if "你是审核角色" in prompt:
        task_match = re.search(r"审核任务 (T\d+)", prompt)
        task_id = task_match.group(1) if task_match else "unknown"
        record("review start %s pid=%d" % (task_id, os.getpid()))
        time.sleep(sleep_seconds)
        if mode == "fail_review":
            record("review fail %s" % task_id)
            print("fake review failure")
            raise SystemExit(2)
        if mode == "nofiles":
            record("review nofiles %s" % task_id)
            raise SystemExit(0)
        review_match = re.search(r"绝对路径：(\S+?/审查\.md)", prompt)
        done_match = re.search(r"绝对路径：(\S+?/审核完成\.md)", prompt)
        if not review_match or not done_match:
            print("MISSING REVIEW PATHS")
            raise SystemExit(4)
        verdict = "须修正" if mode == "revise" else "通过"
        if verdict == "须修正":
            if issue_mode == "first":
                issues = "| P1 | 阻塞 | 产物不完整 | 重做 %s-1 |" % task_id
                issues = (
                    "| 编号 | 级别 | 问题 | 建议修正 |\n"
                    "| --- | --- | --- | --- |\n" + issues
                )
            else:
                issues = "- 缺少运行证据：DONE.md 没有记录命令输出"
        else:
            issues = "- 无"
        body = (
            "# 审查\n\n## 结论\n\n结论：**%s**\n\n"
            "## 逐项证据\n\n- 已核对修正后的交付\n\n"
            "## 问题清单\n\n%s\n\n"
            "- 审核时刻：%.6f\n"
            % (verdict, issues, time.time())
        )
        review_path = Path(review_match.group(1))
        done_path = Path(done_match.group(1))
        review_path.write_text(body, encoding="utf-8")
        done_path.write_text("审核完成\n", encoding="utf-8")
        record("review end %s pid=%d" % (task_id, os.getpid()))
        print("fake review " + verdict)
        raise SystemExit(0)

    match = re.search(r"执行子任务 (T\d+-\d+)：", prompt)
    subtask = match.group(1) if match else "unknown"
    record("dispatch start %s pid=%d cwd=%s" % (subtask, os.getpid(), os.getcwd()))
    time.sleep(sleep_seconds)
    if mode == "fail_dispatch" and subtask.endswith("-1"):
        record("dispatch fail %s" % subtask)
        print("fake dispatch failure")
        raise SystemExit(2)
    outdir = Path.cwd() / "子任务" / subtask
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "DONE.md").write_text(
        "# DONE %s\n\n"
        "## 做了什么\n\n- 假 Agent 执行修正\n\n"
        "## 怎么验\n\n- 检查产物\n\n"
        "## 结果\n\n- pid=%d\n" % (subtask, os.getpid()),
        encoding="utf-8",
    )
    (outdir / "result.txt").write_text(
        "subtask=%s\n" % subtask, encoding="utf-8"
    )
    record("dispatch end %s pid=%d" % (subtask, os.getpid()))
    print("fake dispatch done " + subtask)
finally:
    if fd is not None:
        os.close(fd)
        try:
            os.unlink(held)
        except OSError:
            pass
'''


def subtasks_text(task_id: str, statuses: list) -> str:
    lines = ["subtasks:"]
    for index, status in enumerate(statuses, start=1):
        lines.extend(
            [
                f"  - id: {task_id}-{index}",
                f"    title: 继续修正子任务 {index}",
                f"    goal: 写第 {index} 份产物",
                "    acceptance: DONE.md 包含编号",
                f"    status: {status}",
                "    agent_role: demo-builder",
                '    started_at: "2026-10-24T10:00:0%d+08:00"' % index,
                '    finished_at: "2026-10-24T10:05:0%d+08:00"' % index,
                '    error: ""',
                '    log: ""',
            ]
        )
    return "\n".join(lines) + "\n"


def review_body(task_id: str, *, issues_with_ids: bool = True) -> str:
    if issues_with_ids:
        issues = (
            "| 编号 | 级别 | 问题 | 建议修正 |\n"
            "| --- | --- | --- | --- |\n"
            f"| P1 | 阻塞 | 产物不完整 | 重做 {task_id}-1 |\n"
        )
    else:
        issues = "- 缺少运行证据：DONE.md 没有记录命令输出\n"
    return (
        f"# 审查报告 · {task_id}\n\n"
        "## 结论\n\n结论：**须修正**\n\n"
        "## 逐项证据\n\n- 已核对\n\n"
        f"## 问题清单\n\n{issues}\n"
        "## 复核建议\n\n修正后重审。\n"
    )


class ReviseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: getattr(server, name) for name in GLOBALS}
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.projects = self.root / "项目"
        self.archive = self.root / "归档"
        self.roles = self.root / "协作" / "roles"
        self.agents = self.root / "协作" / "agents"
        self.dispatch_dir = self.root / "协作" / "调度台"
        for path in (
            self.projects,
            self.archive,
            self.roles,
            self.agents,
            self.dispatch_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

        self.script = self.root / "fake-revise-agent.py"
        self.script.write_text(FAKE_AGENT, encoding="utf-8")
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)
        for agent_id, agent_name in {
            "fake-dispatch": "测试分发 Agent",
            "fake-reviewer": "测试审核 Agent",
        }.items():
            (self.agents / f"{agent_id}.yaml").write_text(
                f"id: {agent_id}\n"
                f"name: {agent_name}\n"
                f"command: {self.script}\n"
                'args_template: \'"{prompt}"\'\n'
                f"cwd: {self.root}\n"
                "installed: true\n"
                "version: test\n"
                'notes: ""\n',
                encoding="utf-8",
            )
        self.write_role("demo-builder", "演示构建", "fake-dispatch")
        self.write_role("reviewer", "审核", "fake-reviewer")

        self.order_file = self.root / "revise-order.txt"
        self.held_file = self.root / "revise-held.lock"
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "T17_MODE",
                "T17_ISSUES",
                "T17_ORDER_FILE",
                "T17_HELD_FILE",
                "T17_AGENT_SLEEP",
            )
        }
        os.environ["T17_MODE"] = "pass"
        os.environ["T17_ISSUES"] = "first"
        os.environ["T17_ORDER_FILE"] = str(self.order_file)
        os.environ["T17_HELD_FILE"] = str(self.held_file)
        os.environ["T17_AGENT_SLEEP"] = "0.2"

        server.PROJECT_DIR = self.root
        server.COLLAB_DIR = self.root / "协作"
        server.PROJECTS_DIR = self.projects
        server.ARCHIVE_DIR = self.archive
        server.DISPATCH_DIR = self.dispatch_dir
        server.ROLES_DIR = self.roles
        server.AGENTS_DIR = self.agents
        server.ROLES_ARCHIVE_DIR = self.roles / ".archive"
        server.WATCH_STATE = self.dispatch_dir / "watch.state"
        server.ROUNDS_TSV = self.dispatch_dir / "rounds.tsv"
        server.JOURNAL_LOG = self.dispatch_dir / "journal.log"
        server.GUARD_STATUS = self.dispatch_dir / "guard.status"
        server.REGISTRATION_TIMEOUT_SECONDS = 8.0
        server.RUNNER_TIMEOUT_SECONDS = 8.0
        server.RUNNER = runner.Runner(self.root, timeout_seconds=8.0)
        server.PORT = 18793
        with server.ADVANCE_STATE_LOCK:
            server.ACTIVE_ADVANCE = None
        with server.DISPATCH_STATE_LOCK:
            server.ACTIVE_DISPATCH = None
        with server.REVIEW_STATE_LOCK:
            server.ACTIVE_REVIEW = None
        server.ACTIVE_REGISTRATIONS.clear()
        self.httpd = None
        self.port = 0

    def tearDown(self) -> None:
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                with server.ADVANCE_STATE_LOCK:
                    active_advance = server.ACTIVE_ADVANCE
                if (
                    active_advance is None
                    and not server._active_dispatch_task()
                    and not server._active_review_task()
                ):
                    break
                time.sleep(0.05)
            if server.RUNNER is not None:
                server.RUNNER.wait_for_registration_idle(8.0)
                server.RUNNER.wait_for_idle(8.0)
            if self.httpd is not None:
                self.httpd.shutdown()
                self.httpd.server_close()
        finally:
            with server.ADVANCE_STATE_LOCK:
                server.ACTIVE_ADVANCE = None
            with server.DISPATCH_STATE_LOCK:
                server.ACTIVE_DISPATCH = None
            with server.REVIEW_STATE_LOCK:
                server.ACTIVE_REVIEW = None
            server.ACTIVE_REGISTRATIONS.clear()
            for name, value in self._saved.items():
                setattr(server, name, value)
            for key, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self._temp.cleanup()

    def write_role(self, role_id: str, name: str, agent_id: str) -> None:
        (self.roles / f"{role_id}.yaml").write_text(
            f"id: {role_id}\n"
            f"name: {name}\n"
            f"description: 继续修正测试 {role_id}\n"
            f"agent_id: {agent_id}\n"
            "write_scope: []\n"
            "enabled: true\n"
            'notes: ""\n',
            encoding="utf-8",
        )

    def make_task(
        self,
        task_id: str,
        statuses: list,
        *,
        with_review: bool = True,
        issues_with_ids: bool = True,
        revise_round: int = 0,
    ) -> Path:
        project = self.projects / f"{task_id}_继续修正测试"
        docking = project / "对接"
        docking.mkdir(parents=True)
        (docking / "任务单.md").write_text(
            f"# {task_id} — 继续修正测试\n\n## 验收标准\n\n- 子任务交付\n",
            encoding="utf-8",
        )
        (docking / "subtasks.yaml").write_text(
            subtasks_text(task_id, statuses), encoding="utf-8"
        )
        (docking / "task.yaml").write_text(
            f"id: {task_id}\n"
            f"title: 继续修正测试 {task_id}\n"
            f"workdir: {project / '工作区'}\n"
            f"created_at: 2026-10-24T09:00:00+08:00\n"
            f"status: registered\n"
            f"revise_round: {revise_round}\n",
            encoding="utf-8",
        )
        (docking / "登记完成.md").write_text(
            f"# 登记完成\n\n- 任务编号：{task_id}\n"
            f"- 子任务数量：{len(statuses)}\n",
            encoding="utf-8",
        )
        for index, status in enumerate(statuses, start=1):
            if status in {"delivered", "passed"}:
                outdir = project / "工作区" / "子任务" / f"{task_id}-{index}"
                outdir.mkdir(parents=True, exist_ok=True)
                (outdir / "DONE.md").write_text(
                    f"# DONE {task_id}-{index}\n\n"
                    "## 做了什么\n\n- 预置交付\n\n"
                    "## 怎么验\n\n- 核对文件\n\n"
                    "## 结果\n\n- 通过\n",
                    encoding="utf-8",
                )
                (outdir / "result.txt").write_text(
                    f"{task_id}-{index}\n", encoding="utf-8"
                )
        if with_review:
            (docking / "审查.md").write_text(
                review_body(task_id, issues_with_ids=issues_with_ids),
                encoding="utf-8",
            )
            (docking / "审核完成.md").write_text("审核完成\n", encoding="utf-8")
        return project

    def start_server(self) -> None:
        self.httpd = server.ConsoleServer(("127.0.0.1", 0), server.ConsoleHandler)
        self.port = self.httpd.server_address[1]
        server.PORT = self.port
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()

    def request(
        self,
        method: str,
        path: str,
        *,
        accept: str = "",
        body: bytes = b"",
    ):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if accept:
            headers["Accept"] = accept
        if body:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read().decode("utf-8", errors="replace")
        status = response.status
        headers_out = dict(response.getheaders())
        connection.close()
        return status, data, headers_out

    def read_subtasks(self, project: Path):
        subtasks, error = server._load_subtasks(project / "对接" / "subtasks.yaml")
        self.assertEqual(error, "")
        return subtasks

    def write_review_naming(self, project: Path, task_id: str, ids: list) -> None:
        rows = "、".join(ids)
        body = (
            f"# 审查报告 · {task_id}\n\n"
            "## 结论\n\n结论：**须修正**\n\n"
            "## 问题清单\n\n"
            "| 编号 | 级别 | 问题 | 建议修正 |\n"
            "| --- | --- | --- | --- |\n"
            f"| P1 | 阻塞 | 产物不完整 | 重做 {rows} |\n"
        )
        (project / "对接" / "审查.md").write_text(body, encoding="utf-8")

    def revise_meta(self, project: Path) -> dict:
        return server._read_task_meta(project, project.name.split("_", 1)[0])

    def wait_advance(self, task_id: str, timeout: float = 25.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = server._advance_state_snapshot(task_id)
            if state and str(state.get("state") or "") in {
                "completed",
                "stopped",
                "failed",
            }:
                if not server._active_advance_task():
                    return state
            time.sleep(0.03)
        self.fail(f"{task_id} 继续修正没有在 {timeout} 秒内结束")

    def order_lines(self) -> list:
        if not self.order_file.exists():
            return []
        return self.order_file.read_text(encoding="utf-8").splitlines()

    # ------------------------------------------------------------------

    def test_detail_shows_revise_button_issues_and_targets(self) -> None:
        project = self.make_task("T81", ["delivered", "delivered"])
        self.start_server()

        status, page, _headers = self.request("GET", "/task/T81")
        self.assertEqual(status, 200)
        self.assertIn("当前阶段 · 须修正", page)
        self.assertIn("继续修正（第 1 轮）", page)
        self.assertIn("问题清单摘要", page)
        self.assertIn("本轮将重做的子任务", page)
        self.assertIn("T81-1", page)
        self.assertIn("round-1.md", page)
        self.assertIn('name="mode" value="affected"', page)
        self.assertNotIn("全部子任务重做", page)

        status, body, _headers = self.request(
            "GET", "/api/task/T81/revise-status", accept="application/json"
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["phase"], "revise")
        self.assertEqual(payload["revise_round"], 0)
        self.assertEqual(payload["next_round"], 1)
        self.assertEqual(payload["target_ids"], ["T81-1"])
        self.assertFalse(payload["needs_choice"])
        self.assertTrue(payload["can_start"])
        self.assertEqual(len(payload["issues"]), 1)

        status, body, _headers = self.request(
            "GET", "/api/tasks", accept="application/json"
        )
        self.assertEqual(status, 200)
        item = next(
            row for row in json.loads(body)["tasks"] if row["id"] == "T81"
        )
        self.assertEqual(item["revise_round"], 0)
        self.assertTrue(item["revise_can_start"])
        self.assertEqual(item["revise_targets"], ["T81-1"])
        self.assertFalse(item["revise_needs_choice"])
        self.assertFalse(server.RUNNER.lock_path.exists())
        self.assertFalse((project / "对接" / "审查历史").exists())

    def test_revise_resets_only_affected_and_advances_to_passed(self) -> None:
        project = self.make_task("T82", ["delivered", "delivered"])
        original_review = (project / "对接" / "审查.md").read_text(encoding="utf-8")
        original = self.read_subtasks(project)
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/revise/T82", accept="application/json"
        )
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        self.assertEqual(payload["revise_round"], 1)
        self.assertEqual(payload["mode"], "affected")
        self.assertEqual(payload["target_ids"], ["T82-1"])
        self.assertEqual(payload["reset_subtasks"], ["T82-1"])
        self.assertIn("审查历史/round-1.md", payload["archived_review"])
        self.assertEqual(payload["status_url"], "/api/task/T82/revise-status")

        live_status, live_body, _headers = self.request(
            "GET", "/api/task/T82/revise-status", accept="application/json"
        )
        self.assertEqual(live_status, 200)
        live = json.loads(live_body)
        self.assertIn(live["state"], {"running", "completed", "stopped"})
        self.assertEqual(live["revise_round"], 1)

        final = self.wait_advance("T82")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)
        self.assertTrue(final["revise"], final)
        self.assertEqual(final["revise_round"], 1)
        self.assertEqual(final["revise_strategy"], "affected")

        history = project / "对接" / "审查历史" / "round-1.md"
        self.assertTrue(history.is_file())
        self.assertEqual(history.read_text(encoding="utf-8"), original_review)
        self.assertTrue(
            (project / "对接" / "审查历史" / "round-1-审核完成.md").is_file()
        )
        self.assertIn(
            "结论：**通过**",
            (project / "对接" / "审查.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(self.revise_meta(project).get("revise_round"), "1")

        items = self.read_subtasks(project)
        self.assertEqual([item.status for item in items], ["delivered", "delivered"])
        self.assertNotEqual(items[0].started_at, original[0].started_at)
        self.assertEqual(items[1].started_at, original[1].started_at)
        self.assertEqual(items[1].finished_at, original[1].finished_at)

        lines = self.order_lines()
        dispatch_starts = [line for line in lines if line.startswith("dispatch start")]
        review_starts = [line for line in lines if line.startswith("review start")]
        self.assertEqual(len(dispatch_starts), 1, lines)
        self.assertIn("T82-1", dispatch_starts[0])
        self.assertFalse(any("T82-2" in line for line in lines), lines)
        self.assertEqual(len(review_starts), 1, lines)
        self.assertFalse(any(line.startswith("overlap") for line in lines), lines)

        logs = sorted((project / "工作区" / "日志").glob("revise-*.log"))
        self.assertEqual(len(logs), 1)
        log_text = logs[0].read_text(encoding="utf-8")
        self.assertIn("第 1 轮", log_text)
        self.assertIn("T82-1", log_text)
        self.assertIn("round-1.md", log_text)
        self.assertIn("重置为 pending：T82-1", log_text)

        status, page, _headers = self.request("GET", "/task/T82")
        self.assertEqual(status, 200)
        self.assertIn("当前阶段 · 已通过", page)
        self.assertIn("审核结论：通过", page)
        self.assertNotIn("继续修正（第", page)

        status, body, _headers = self.request(
            "GET", "/api/tasks", accept="application/json"
        )
        item = next(
            row for row in json.loads(body)["tasks"] if row["id"] == "T82"
        )
        self.assertEqual(item["revise_round"], 1)
        self.assertFalse(item["revise_can_start"])
        self.assertEqual(item["phase"], "passed")
        self.assertFalse(server.RUNNER.lock_path.exists())
        self.assertFalse(self.held_file.exists())

    def test_no_subtask_refs_requires_choice_and_review_only_does_not_rerun(self) -> None:
        project = self.make_task(
            "T83", ["delivered", "delivered"], issues_with_ids=False
        )
        original = self.read_subtasks(project)
        self.start_server()

        status, page, _headers = self.request("GET", "/task/T83")
        self.assertEqual(status, 200)
        self.assertIn("未识别到具体子任务", page)
        self.assertIn("全部子任务重做", page)
        self.assertIn("只重新审核（不改产物）", page)

        status, body, _headers = self.request(
            "POST", "/revise/T83", accept="application/json"
        )
        self.assertEqual(status, 409, body)
        payload = json.loads(body)
        self.assertTrue(payload["needs_choice"])
        self.assertEqual(
            [option["mode"] for option in payload["options"]], ["all", "review"]
        )
        self.assertEqual(self.revise_meta(project).get("revise_round"), "0")
        self.assertFalse((project / "对接" / "审查历史").exists())
        self.assertFalse(server.RUNNER.lock_path.exists())

        form = urlencode({"mode": "review"}).encode("utf-8")
        status, body, _headers = self.request(
            "POST", "/revise/T83", accept="application/json", body=form
        )
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["target_ids"], [])
        final = self.wait_advance("T83")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)

        lines = self.order_lines()
        self.assertFalse(
            any(line.startswith("dispatch start") for line in lines), lines
        )
        self.assertEqual(
            len([line for line in lines if line.startswith("review start")]), 1
        )
        items = self.read_subtasks(project)
        self.assertEqual([item.status for item in items], ["delivered", "delivered"])
        self.assertEqual(items[0].started_at, original[0].started_at)
        self.assertEqual(items[1].started_at, original[1].started_at)
        self.assertTrue(
            (project / "对接" / "审查历史" / "round-1.md").is_file()
        )
        self.assertEqual(self.revise_meta(project).get("revise_round"), "1")

    def test_all_mode_redoes_every_subtask(self) -> None:
        project = self.make_task(
            "T84", ["delivered", "delivered"], issues_with_ids=False
        )
        self.start_server()

        form = urlencode({"mode": "all"}).encode("utf-8")
        status, body, _headers = self.request(
            "POST", "/revise/T84", accept="application/json", body=form
        )
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        self.assertEqual(payload["mode"], "all")
        self.assertEqual(payload["target_ids"], ["T84-1", "T84-2"])

        final = self.wait_advance("T84")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)

        lines = self.order_lines()
        dispatch_starts = [line for line in lines if line.startswith("dispatch start")]
        self.assertEqual(len(dispatch_starts), 2, lines)
        self.assertTrue(any("T84-1" in line for line in dispatch_starts))
        self.assertTrue(any("T84-2" in line for line in dispatch_starts))
        self.assertEqual(
            [item.status for item in self.read_subtasks(project)],
            ["delivered", "delivered"],
        )

    def test_html_form_redirects_to_detail_and_serves_script(self) -> None:
        project = self.make_task("T87", ["delivered", "delivered"])
        self.start_server()

        status, script, _headers = self.request("GET", "/static/revise.js")
        self.assertEqual(status, 200)
        self.assertIn("修正中", script)
        self.assertIn('getElementById("revise-panel")', script)

        form = urlencode({"mode": "affected"}).encode("utf-8")
        status, _body, headers = self.request("POST", "/revise/T87", body=form)
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/task/T87?revise=1")

        status, page, _headers = self.request("GET", "/task/T87?revise=1")
        self.assertEqual(status, 200)
        self.assertIn("已启动继续修正", page)
        self.assertIn('id="revise-panel"', page)

        final = self.wait_advance("T87")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)
        self.assertTrue(
            (project / "对接" / "审查历史" / "round-1.md").is_file()
        )

    def test_affected_mode_skips_passed_subtask(self) -> None:
        project = self.make_task("T88", ["delivered", "passed"])
        self.write_review_naming(project, "T88", ["T88-1", "T88-2"])
        original = self.read_subtasks(project)
        self.start_server()

        status, page, _headers = self.request("GET", "/task/T88")
        self.assertEqual(status, 200)
        self.assertIn("T88-1", page)
        self.assertIn("T88-2", page)
        self.assertIn("已是「已通过」，默认不重跑", page)
        self.assertIn("全部重做（含已通过）", page)

        status, body, _headers = self.request(
            "POST", "/revise/T88", accept="application/json"
        )
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        self.assertEqual(payload["target_ids"], ["T88-1", "T88-2"])
        self.assertEqual(payload["reset_subtasks"], ["T88-1"])

        final = self.wait_advance("T88")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)
        items = self.read_subtasks(project)
        self.assertEqual([item.status for item in items], ["delivered", "passed"])
        self.assertNotEqual(items[0].started_at, original[0].started_at)
        self.assertEqual(items[1].started_at, original[1].started_at)
        self.assertEqual(items[1].finished_at, original[1].finished_at)
        dispatch_starts = [
            line for line in self.order_lines() if line.startswith("dispatch start")
        ]
        self.assertEqual(len(dispatch_starts), 1, dispatch_starts)
        self.assertIn("T88-1", dispatch_starts[0])
        self.assertFalse(any("T88-2" in line for line in dispatch_starts))

    def test_all_mode_resets_passed_subtask(self) -> None:
        project = self.make_task("T89", ["delivered", "passed"])
        self.write_review_naming(project, "T89", ["T89-1", "T89-2"])
        self.start_server()

        form = urlencode({"mode": "all"}).encode("utf-8")
        status, body, _headers = self.request(
            "POST", "/revise/T89", accept="application/json", body=form
        )
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        self.assertEqual(payload["reset_subtasks"], ["T89-1", "T89-2"])

        final = self.wait_advance("T89")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)
        self.assertEqual(
            [item.status for item in self.read_subtasks(project)],
            ["delivered", "delivered"],
        )
        dispatch_starts = [
            line for line in self.order_lines() if line.startswith("dispatch start")
        ]
        self.assertEqual(len(dispatch_starts), 2, dispatch_starts)
        self.assertTrue(any("T89-1" in line for line in dispatch_starts))
        self.assertTrue(any("T89-2" in line for line in dispatch_starts))

    def test_round_limit_blocks_button_and_post(self) -> None:
        project = self.make_task("T85", ["delivered"], revise_round=3)
        self.start_server()

        status, page, _headers = self.request("GET", "/task/T85")
        self.assertEqual(status, 200)
        self.assertIn("已达 3 轮修正上限，请人工介入", page)
        self.assertIn(
            'id="revise-start" type="submit" disabled', page
        )

        status, body, _headers = self.request(
            "POST", "/revise/T85", accept="application/json"
        )
        self.assertEqual(status, 409, body)
        payload = json.loads(body)
        self.assertIn("上限", payload["message"])
        self.assertIn("人工介入", payload["message"])
        self.assertEqual(self.revise_meta(project).get("revise_round"), "3")
        self.assertFalse((project / "对接" / "审查历史").exists())
        self.assertFalse(server.RUNNER.lock_path.exists())

        status, body, _headers = self.request(
            "GET", "/api/tasks", accept="application/json"
        )
        item = next(
            row for row in json.loads(body)["tasks"] if row["id"] == "T85"
        )
        self.assertFalse(item["revise_can_start"])
        self.assertTrue(item["revise_at_limit"])
        self.assertEqual(item["revise_round"], 3)

    def test_three_revise_rounds_stop_at_limit(self) -> None:
        os.environ["T17_MODE"] = "revise"
        project = self.make_task("T86", ["delivered"])
        self.start_server()

        for round_no in (1, 2, 3):
            status, body, _headers = self.request(
                "POST", "/revise/T86", accept="application/json"
            )
            self.assertEqual(status, 202, body)
            payload = json.loads(body)
            self.assertEqual(payload["revise_round"], round_no)
            final = self.wait_advance("T86")
            self.assertEqual(final["state"], "stopped", final)
            self.assertEqual(final["conclusion"], "revise", final)
            self.assertEqual(final["revise_round"], round_no)
            self.assertEqual(
                self.revise_meta(project).get("revise_round"), str(round_no)
            )
            history = project / "对接" / "审查历史" / f"round-{round_no}.md"
            self.assertTrue(history.is_file())
            self.assertIn("须修正", history.read_text(encoding="utf-8"))

            status, page, _headers = self.request("GET", "/task/T86")
            self.assertEqual(status, 200)
            self.assertIn("当前阶段 · 须修正", page)
            if round_no < 3:
                self.assertIn(f"继续修正（第 {round_no + 1} 轮）", page)
            else:
                self.assertIn("已达 3 轮修正上限，请人工介入", page)
                self.assertIn('id="revise-start" type="submit" disabled', page)

        status, body, _headers = self.request(
            "POST", "/revise/T86", accept="application/json"
        )
        self.assertEqual(status, 409, body)
        self.assertIn("人工介入", json.loads(body)["message"])
        dispatch_starts = [
            line for line in self.order_lines() if line.startswith("dispatch start")
        ]
        self.assertEqual(len(dispatch_starts), 3, dispatch_starts)
        self.assertFalse(any(line.startswith("overlap") for line in self.order_lines()))


if __name__ == "__main__":
    unittest.main()
