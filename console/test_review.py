#!/usr/bin/env python3
"""T13 审核阶段：阶段判定、POST /review、串行锁、结论与看板字段。

所有测试在临时仓库中进行，不触碰真实的 项目/、协作/ 或 8787。
"""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

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
    "PORT",
)

FAKE_REVIEWER = '''#!/usr/bin/env python3
import os
import re
import sys
import time

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
task_match = re.search(r"审核任务 (T\\d+)", prompt)
task = task_match.group(1) if task_match else "unknown"
review_match = re.search(r"绝对路径：(\\S+?/审查\\.md)", prompt)
done_match = re.search(r"绝对路径：(\\S+?/审核完成\\.md)", prompt)
mode = os.environ.get("T13_MODE", "pass")
order = os.environ.get("T13_ORDER_FILE", "")
held = os.environ.get("T13_HELD_FILE", "")


def record(text):
    if order:
        with open(order, "a", encoding="utf-8") as handle:
            handle.write(text + "\\n")
            handle.flush()


record("start %s %.3f pid=%d cwd=%s" % (task, time.time(), os.getpid(), os.getcwd()))
fd = None
if held:
    try:
        fd = os.open(held, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        record("overlap %s" % task)
        print("OVERLAP " + task)
        sys.exit(3)

try:
    time.sleep(float(os.environ.get("T13_AGENT_SLEEP", "0.3")))
    if mode == "fail":
        print("fake review failure")
        sys.exit(2)
    if mode == "nofiles":
        print("fake review produced no files")
        sys.exit(0)
    if not review_match or not done_match:
        print("MISSING PATHS IN PROMPT")
        sys.exit(4)
    review_path = review_match.group(1)
    done_path = done_match.group(1)
    revise = mode == "revise"
    verdict = "须修正" if revise else "通过"
    issues = (
        "- 缺少运行证据：DONE.md 没有记录命令输出\\n"
        "- 边界未覆盖：未验证非法输入"
        if revise
        else "- 无"
    )
    body = (
        "# 审查\\n\\n"
        "## 结论\\n\\n结论：**%s**\\n\\n"
        "## 逐项证据\\n\\n"
        "- 任务单与 subtasks.yaml 已读取\\n"
        "- 每个子任务的 DONE.md 已核对\\n\\n"
        "## 问题清单\\n\\n%s\\n" % (verdict, issues)
    )
    os.makedirs(os.path.dirname(review_path), exist_ok=True)
    with open(review_path, "w", encoding="utf-8") as handle:
        handle.write(body)
    with open(done_path, "w", encoding="utf-8") as handle:
        handle.write("审核完成\\n")
    print("fake review %s %s" % (task, verdict))
finally:
    if fd is not None:
        os.close(fd)
        try:
            os.unlink(held)
        except OSError:
            pass
    record("end %s %.3f pid=%d" % (task, time.time(), os.getpid()))
'''


def subtasks_text(task_id: str, statuses: list) -> str:
    lines = ["subtasks:"]
    for index, status in enumerate(statuses, start=1):
        lines.extend(
            [
                f"  - id: {task_id}-{index}",
                f"    title: 子任务 {index}",
                f"    goal: 完成第 {index} 件事",
                "    acceptance: DONE.md 落盘且包含编号",
                f"    status: {status}",
                "    agent_role: demo-builder",
            ]
        )
    return "\n".join(lines) + "\n"


class ReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: getattr(server, name) for name in GLOBALS}
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.projects = self.root / "项目"
        self.archive = self.root / "归档"
        self.roles = self.root / "协作" / "roles"
        self.agents = self.root / "协作" / "agents"
        self.dispatch_dir = self.root / "协作" / "调度台"
        for path in (self.projects, self.archive, self.roles, self.agents, self.dispatch_dir):
            path.mkdir(parents=True, exist_ok=True)

        self.script = self.root / "fake-review-agent.py"
        self.script.write_text(FAKE_REVIEWER, encoding="utf-8")
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)
        (self.agents / "demo-reviewer.yaml").write_text(
            "id: demo-reviewer\n"
            "name: 测试审核 Agent\n"
            "command: "
            + str(self.script)
            + "\n"
            "args_template: '\"{prompt}\"'\n"
            f"cwd: {self.root}\n"
            "installed: true\n"
            "version: test\n"
            'notes: ""\n',
            encoding="utf-8",
        )
        self.write_reviewer("demo-reviewer")
        (self.roles / "demo-builder.yaml").write_text(
            "id: demo-builder\n"
            "name: 测试构建\n"
            "description: 隔离审核测试\n"
            "agent_id: demo-reviewer\n"
            "write_scope: []\n"
            "enabled: true\n"
            'notes: ""\n',
            encoding="utf-8",
        )

        self.order_file = self.root / "review-order.txt"
        self.held_file = self.root / "review-held.lock"
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "T13_ORDER_FILE",
                "T13_HELD_FILE",
                "T13_AGENT_SLEEP",
                "T13_MODE",
            )
        }
        os.environ["T13_ORDER_FILE"] = str(self.order_file)
        os.environ["T13_HELD_FILE"] = str(self.held_file)
        os.environ["T13_AGENT_SLEEP"] = "0.35"
        os.environ.pop("T13_MODE", None)

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
        server.RUNNER = runner.Runner(self.root, timeout_seconds=8.0)
        server.PORT = 18790
        with server.DISPATCH_STATE_LOCK:
            server.ACTIVE_DISPATCH = None
        with server.REVIEW_STATE_LOCK:
            server.ACTIVE_REVIEW = None
        self.httpd = None
        self.port = 0

    def tearDown(self) -> None:
        try:
            deadline = time.time() + 12
            while time.time() < deadline and server._active_review_task():
                time.sleep(0.05)
            if server.RUNNER is not None:
                server.RUNNER.wait_for_idle(8.0)
            if self.httpd is not None:
                self.httpd.shutdown()
                self.httpd.server_close()
        finally:
            with server.DISPATCH_STATE_LOCK:
                server.ACTIVE_DISPATCH = None
            with server.REVIEW_STATE_LOCK:
                server.ACTIVE_REVIEW = None
            for name, value in self._saved.items():
                setattr(server, name, value)
            for key, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self._temp.cleanup()

    def write_reviewer(self, agent_id: str) -> None:
        (self.roles / "reviewer.yaml").write_text(
            "id: reviewer\n"
            "name: 审核\n"
            "description: 隔离审核测试\n"
            "agent_id: " + agent_id + "\n"
            "write_scope: []\n"
            "enabled: true\n"
            'notes: ""\n',
            encoding="utf-8",
        )

    def make_task(self, task_id: str, statuses: list, *, title: str = "审核测试") -> Path:
        project = self.projects / f"{task_id}_审核测试"
        docking = project / "对接"
        docking.mkdir(parents=True)
        (docking / "任务单.md").write_text(
            f"# {task_id} — {title}\n\n## 验收标准\n\n- 功能可用\n- 边界覆盖\n- 证据完整\n",
            encoding="utf-8",
        )
        (docking / "subtasks.yaml").write_text(
            subtasks_text(task_id, statuses), encoding="utf-8"
        )
        (docking / "task.yaml").write_text(
            f"id: {task_id}\n"
            f"title: 审核测试 {task_id}\n"
            f"workdir: {project / '工作区'}\n"
            "created_at: 2026-10-24T09:00:00+08:00\n"
            "status: registered\n",
            encoding="utf-8",
        )
        (docking / "登记完成.md").write_text(
            f"# 登记完成\n\n- 任务编号：{task_id}\n"
            f"- 子任务数量：{len(statuses)}\n",
            encoding="utf-8",
        )
        for index, _status in enumerate(statuses, start=1):
            outdir = project / "工作区" / "子任务" / f"{task_id}-{index}"
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "DONE.md").write_text(
                f"# DONE {task_id}-{index}\n\n"
                "## 做了什么\n\n- 预置交付\n\n"
                "## 怎么验\n\n- 核对文件\n\n"
                "## 结果\n\n"
                f"- cwd={project}\n",
                encoding="utf-8",
            )
            (outdir / "result.txt").write_text(
                f"{task_id}-{index}\n", encoding="utf-8"
            )
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
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
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

    def find_task(self, task_id: str):
        task = server._find_task(task_id)
        self.assertIsNotNone(task, f"任务 {task_id} 应该可读")
        return task

    def wait_review(self, task_id: str, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = server._review_state_snapshot(task_id)
            if (
                state
                and str(state.get("state") or "") in {"completed", "review_failed"}
                and not server._active_review_task()
            ):
                return state
            time.sleep(0.03)
        self.fail(f"{task_id} 审核没有在 {timeout} 秒内结束")

    def review_files(self, project: Path):
        return (
            project / "对接" / "审查.md",
            project / "对接" / "审核完成.md",
        )

    # ------------------------------------------------------------------

    def test_detail_page_switches_to_start_review_when_all_delivered(self) -> None:
        project = self.make_task("T60", ["delivered", "delivered"])
        self.find_task("T60")
        self.start_server()

        status, page, _headers = self.request("GET", "/task/T60")
        self.assertEqual(status, 200)
        self.assertIn("开始审核", page)
        self.assertIn("当前阶段 · 待审核", page)
        self.assertIn("阶段 · 待审核", page)
        self.assertIn("子任务 2/2", page)
        self.assertIn('id="review-start" type="submit">', page)
        self.assertNotIn("开始分发实现", page)
        self.assertIn("/static/review.js", page)

        status, body, _headers = self.request(
            "GET", "/api/tasks", accept="application/json"
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        task = next(item for item in payload["tasks"] if item["id"] == "T60")
        self.assertEqual(task["phase"], "review_ready")
        self.assertEqual(task["phase_label"], "待审核")
        self.assertEqual(task["subtask_total"], 2)
        self.assertEqual(task["subtask_done"], 2)
        self.assertEqual(task["subtasks_total"], 2)
        self.assertEqual(task["subtasks_done"], 2)
        self.assertFalse(task["has_review"])
        self.assertTrue(project.is_dir())

    def make_task_without_subtasks(
        self, task_id: str, *, delivery: bool = True, workspace_product: bool = False
    ) -> Path:
        project = self.projects / f"{task_id}_无子任务审核"
        docking = project / "对接"
        docking.mkdir(parents=True)
        (docking / "任务单.md").write_text(
            f"# {task_id} — 无子任务审核\n\n## 验收标准\n\n- 交付物完整\n- 证据可核\n",
            encoding="utf-8",
        )
        if delivery:
            done = docking / "交付" / "r1" / "DONE.md"
            done.parent.mkdir(parents=True, exist_ok=True)
            done.write_text(
                f"# {task_id} · 任务级交付\n\n- 产物：工作区/report.md\n",
                encoding="utf-8",
            )
        if workspace_product:
            report = project / "工作区" / "report.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("# 报告\n\n结论：可审核。\n", encoding="utf-8")
        return project

    def test_review_verdict_treats_negative_passed_phrases_as_revise(self) -> None:
        for text in (
            "## 结论\n\n结论：未通过\n",
            "## 结论\n\n结论：不通过\n",
            "## 结论\n\n结论：须修正\n",
        ):
            self.assertEqual(server._review_verdict(text), "revise", text)
        self.assertEqual(server._review_verdict("## 结论\n\n结论：通过\n"), "passed")

    def test_review_ready_without_subtasks_uses_task_delivery(self) -> None:
        project = self.make_task_without_subtasks("T68")
        task = self.find_task("T68")
        self.assertEqual(len(task.subtasks), 0)
        self.assertEqual(server._task_phase(task), server.PHASE_REVIEW_READY)
        available, reason = server._review_availability(task)
        self.assertTrue(available, reason)
        self.assertIn("审核本任务交付物", reason)
        self.start_server()

        status, page, _headers = self.request("GET", "/task/T68")
        self.assertEqual(status, 200)
        self.assertIn("当前阶段 · 待审核", page)
        self.assertIn('id="review-panel"', page)
        self.assertIn("开始审核", page)
        self.assertIn("任务级交付物已就绪", page)
        self.assertIn("审核本任务交付物", page)
        self.assertNotIn('id="dispatch-panel"', page)
        self.assertNotIn("开始分发实现", page)

        status, body, _headers = self.request(
            "GET", "/api/tasks", accept="application/json"
        )
        self.assertEqual(status, 200)
        item = next(
            row for row in json.loads(body)["tasks"] if row["id"] == "T68"
        )
        self.assertEqual(item["phase"], "review_ready")
        self.assertEqual(item["subtasks_total"], 0)

        status, body, _headers = self.request(
            "POST", "/review/T68", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        final = self.wait_review("T68")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)
        review_path, done_path = self.review_files(project)
        self.assertTrue(review_path.is_file())
        self.assertTrue(done_path.is_file())
        self.assertEqual(self.find_task("T68").review_path, review_path)

    def test_review_ready_without_subtasks_uses_workspace_product(self) -> None:
        self.make_task_without_subtasks(
            "T69", delivery=False, workspace_product=True
        )
        task = self.find_task("T69")
        self.assertEqual(server._task_phase(task), server.PHASE_REVIEW_READY)
        available, reason = server._review_availability(task)
        self.assertTrue(available, reason)
        self.assertIn("工作区已有 1 个产物", reason)
        self.start_server()

        status, page, _headers = self.request("GET", "/task/T69")
        self.assertEqual(status, 200)
        self.assertIn("当前阶段 · 待审核", page)
        self.assertIn('id="review-panel"', page)
        self.assertIn("开始审核", page)
        self.assertIn("工作区已有 1 个产物", page)
        self.assertNotIn('id="dispatch-panel"', page)

    def test_review_runs_serially_and_writes_conclusion(self) -> None:
        project = self.make_task("T61", ["delivered", "delivered"])
        self.find_task("T61")
        self.start_server()

        os.environ["T13_AGENT_SLEEP"] = "0.7"
        status, body, _headers = self.request(
            "POST",
            "/review/T61",
            accept="application/json",
            body=b"",
        )
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        self.assertEqual(payload["review"]["state"], "running")
        self.assertEqual(payload["status_url"], "/api/task/T61/review-status")

        deadline = time.time() + 3
        while time.time() < deadline:
            if server._active_review_task():
                break
            time.sleep(0.02)
        self.assertEqual(server._active_review_task(), "T61")

        status, body, _headers = self.request(
            "POST",
            "/review/T61",
            accept="application/json",
            body=b"",
        )
        self.assertEqual(status, 409, body)
        self.assertIn("已有审核在跑", json.loads(body)["error"])

        status, body, _headers = self.request(
            "GET", "/api/task/T61/review-status", accept="application/json"
        )
        self.assertEqual(status, 200)
        live = json.loads(body)
        self.assertEqual(live["phase"], "reviewing")
        self.assertTrue(live["running"])
        self.assertEqual(live["done"], 2)
        self.assertEqual(live["total"], 2)

        final = self.wait_review("T61")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)

        review_path, done_path = self.review_files(project)
        self.assertTrue(review_path.is_file())
        self.assertTrue(done_path.is_file())
        self.assertIn("结论：**通过**", review_path.read_text(encoding="utf-8"))

        runner_status = server.RUNNER.read_status()
        self.assertEqual(runner_status.get("kind"), "review")
        self.assertEqual(runner_status.get("task"), "T61")
        self.assertEqual(runner_status.get("state"), "success")
        self.assertIn("review-", Path(str(runner_status.get("log"))).name)
        self.assertFalse(server.RUNNER.lock_path.exists())
        history = server.RUNNER.history(10)
        self.assertEqual(
            len([record for record in history if record.get("kind") == "review"]), 1
        )

        log_path = server.RUNNER.resolve_log(runner_status.get("log"))
        self.assertIsNotNone(log_path)
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        self.assertIn("kind: review", log_text)
        self.assertIn("你是审核角色", log_text)
        self.assertIn("只允许审核角色写 审查.md", log_text)
        self.assertIn("不得改子任务产物", log_text)
        self.assertIn("不删文件", log_text)
        self.assertIn("fake review T61 通过", log_text)

        order = self.order_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(order), 2, order)
        self.assertTrue(order[0].startswith("start T61"))
        self.assertTrue(order[1].startswith("end T61"))
        self.assertIn(f"cwd={project}", order[0])
        self.assertFalse(any(line.startswith("overlap") for line in order))
        self.assertFalse(self.held_file.exists())

        self.assertEqual(
            [item.status for item in self.find_task("T61").subtasks],
            ["delivered", "delivered"],
        )

        status, page, _headers = self.request("GET", "/task/T61")
        self.assertEqual(status, 200)
        self.assertIn("当前阶段 · 已通过", page)
        self.assertIn("审核结论：通过", page)
        self.assertIn("审核完成信号已落盘", page)
        self.assertIn("问题清单摘要", page)
        self.assertNotIn('id="review-start" type="submit">', page)

        status, body, _headers = self.request(
            "GET", "/api/task/T61/review-status", accept="application/json"
        )
        self.assertEqual(status, 200)
        done_payload = json.loads(body)
        self.assertEqual(done_payload["state"], "completed")
        self.assertEqual(done_payload["conclusion"], "passed")
        self.assertEqual(done_payload["phase"], "passed")
        self.assertTrue(done_payload["has_review"])
        self.assertTrue(done_payload["review_done"])
        self.assertFalse(done_payload["can_start"])

        status, body, _headers = self.request(
            "GET", "/api/tasks", accept="application/json"
        )
        task = next(
            item
            for item in json.loads(body)["tasks"]
            if item["id"] == "T61"
        )
        self.assertEqual(task["phase"], "passed")
        self.assertTrue(task["has_review_done"])
        self.assertEqual(task["review_verdict"], "passed")

    def test_revise_conclusion_and_issue_summary(self) -> None:
        project = self.make_task("T62", ["delivered"])
        os.environ["T13_MODE"] = "revise"
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/review/T62", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        final = self.wait_review("T62")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "revise", final)

        review_path, done_path = self.review_files(project)
        self.assertTrue(review_path.is_file())
        self.assertTrue(done_path.is_file())

        status, page, _headers = self.request("GET", "/task/T62")
        self.assertEqual(status, 200)
        self.assertIn("当前阶段 · 须修正", page)
        self.assertIn("审核结论：须修正", page)
        self.assertIn("缺少运行证据", page)
        self.assertIn("边界未覆盖", page)

        status, body, _headers = self.request(
            "GET", "/api/task/T62/review-status", accept="application/json"
        )
        payload = json.loads(body)
        self.assertEqual(payload["phase"], "revise")
        self.assertEqual(payload["conclusion"], "revise")
        self.assertEqual(len(payload["issues"]), 2)

    def test_review_preflight_rejects_incomplete_subtasks(self) -> None:
        project = self.make_task("T63", ["delivered", "pending"])
        self.find_task("T63")
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/review/T63", accept="application/json", body=b""
        )
        self.assertEqual(status, 409, body)
        payload = json.loads(body)
        self.assertIn("尚未全部交付", payload["message"])
        self.assertIn("T63-2", payload["message"])
        self.assertFalse(server.RUNNER.lock_path.exists())
        self.assertEqual(server._active_review_task(), "")
        self.assertFalse(self.order_file.exists())
        review_path, done_path = self.review_files(project)
        self.assertFalse(review_path.exists())
        self.assertFalse(done_path.exists())

        status, page, _headers = self.request("GET", "/task/T63")
        self.assertEqual(status, 200)
        self.assertIn("当前阶段 · 未分发", page)
        self.assertIn("继续分发（余 1）", page)
        self.assertNotIn('id="review-panel"', page)

    def test_unbound_reviewer_returns_409_with_clear_message(self) -> None:
        self.make_task("T64", ["delivered"])
        self.write_reviewer("")
        self.find_task("T64")
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/review/T64", accept="application/json", body=b""
        )
        self.assertEqual(status, 409, body)
        payload = json.loads(body)
        self.assertIn("审核角色未绑定", payload["error"])
        self.assertIn("审核角色未绑定", payload["message"])
        self.assertFalse(server.RUNNER.lock_path.exists())
        self.assertEqual(server._active_review_task(), "")
        self.assertFalse(self.order_file.exists())

        status, page, _headers = self.request("GET", "/task/T64")
        self.assertEqual(status, 200)
        self.assertIn("审核角色未就绪", page)
        self.assertIn("审核角色未绑定", page)
        self.assertIn('id="review-start" type="submit" disabled>', page)

    def test_review_failure_exposes_log_and_can_retry(self) -> None:
        project = self.make_task("T65", ["delivered"])
        os.environ["T13_MODE"] = "fail"
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/review/T65", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        failed = self.wait_review("T65")
        self.assertEqual(failed["state"], "review_failed", failed)
        self.assertTrue(failed.get("error"))
        self.assertIn("review-", Path(str(failed.get("log"))).name)
        self.assertFalse(self.review_files(project)[0].exists())
        self.assertFalse(self.held_file.exists())
        self.assertFalse(server.RUNNER.lock_path.exists())

        status, page, _headers = self.request("GET", "/task/T65")
        self.assertEqual(status, 200)
        self.assertIn("上次审核未完成，可重试", page)
        self.assertIn("重新开始审核", page)
        self.assertIn("查看审核日志", page)

        status, body, _headers = self.request(
            "GET", "/api/task/T65/review-status", accept="application/json"
        )
        failed_payload = json.loads(body)
        self.assertEqual(failed_payload["state"], "review_failed")
        self.assertEqual(failed_payload["phase"], "review_ready")
        self.assertTrue(failed_payload["can_start"])
        self.assertTrue(failed_payload["log_url"])

        os.environ["T13_MODE"] = "pass"
        status, body, _headers = self.request(
            "POST", "/review/T65", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        final = self.wait_review("T65")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)
        review_path, done_path = self.review_files(project)
        self.assertTrue(review_path.is_file())
        self.assertTrue(done_path.is_file())

    def test_dispatch_is_rejected_while_review_holds_the_lock(self) -> None:
        self.make_task("T66", ["delivered"])
        self.make_task("T67", ["pending"])
        self.find_task("T66")
        self.find_task("T67")
        self.start_server()
        os.environ["T13_AGENT_SLEEP"] = "0.9"

        status, body, _headers = self.request(
            "POST", "/review/T66", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)

        status, body, _headers = self.request(
            "POST", "/dispatch/T67", accept="application/json", body=b""
        )
        self.assertEqual(status, 409, body)
        self.assertIn("审核正在运行", json.loads(body)["error"])
        self.assertEqual(server._dispatch_state_snapshot("T67"), {})

        final = self.wait_review("T66")
        self.assertEqual(final["state"], "completed", final)
        order = []
        if self.order_file.exists():
            order = self.order_file.read_text(encoding="utf-8").splitlines()
        self.assertTrue(all("T66" in line for line in order), order)
        self.assertFalse(any(line.startswith("overlap") for line in order))


if __name__ == "__main__":
    unittest.main()
