#!/usr/bin/env python3
"""T14 全自动推进与发布后自动推进的隔离测试。

所有测试都在临时仓库运行，使用假 Agent/假总控，不触碰真实 项目/、协作/ 或 8787。
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
    "PORT",
    "ADVANCE_MAX_RUNTIME_SECONDS",
    "ADVANCE_MAX_SUBTASKS",
    "ADVANCE_MAX_REVISE_ROUNDS",
)

FAKE_AGENT = r'''#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
mode = os.environ.get("T14_MODE", "pass")
order = os.environ.get("T14_ORDER_FILE", "")
held = os.environ.get("T14_HELD_FILE", "")
sleep_seconds = float(os.environ.get("T14_AGENT_SLEEP", "0.25"))


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
    if "你是协作值班台的总控" in prompt:
        project = Path.cwd()
        docking = project / "对接"
        task_id = project.name.split("_", 1)[0]
        record("register start %s" % task_id)
        (docking / "任务单.md").write_text(
            "# %s — 自动推进演示\n\n## 目标\n\n两个子任务自动推进。\n\n"
            "## 验收标准\n\n- 子任务交付\n- 审查通过\n\n"
            "## 禁止事项\n\n- 不写审查.md\n" % task_id,
            encoding="utf-8",
        )
        (docking / "subtasks.yaml").write_text(
            "subtasks:\n"
            "  - id: %s-1\n"
            "    title: 自动推进子任务一\n"
            "    goal: 写第一份产物\n"
            "    acceptance: DONE.md 包含编号\n"
            "    status: pending\n"
            "    agent_role: demo-builder\n"
            "  - id: %s-2\n"
            "    title: 自动推进子任务二\n"
            "    goal: 写第二份产物\n"
            "    acceptance: DONE.md 包含编号\n"
            "    status: pending\n"
            "    agent_role: demo-builder\n" % (task_id, task_id),
            encoding="utf-8",
        )
        time.sleep(0.15)
        (docking / "登记完成.md").write_text(
            "# 登记完成\n\n- 任务编号：%s\n- 子任务数量：2\n" % task_id,
            encoding="utf-8",
        )
        record("register end %s" % task_id)
        print("fake controller registered " + task_id)
        raise SystemExit(0)

    if "你是审核角色" in prompt:
        task_match = re.search(r"审核任务 (T\d+)", prompt)
        task_id = task_match.group(1) if task_match else "unknown"
        record("review start %s pid=%d cwd=%s" % (task_id, os.getpid(), os.getcwd()))
        time.sleep(sleep_seconds)
        if mode == "fail_review":
            record("review fail %s" % task_id)
            print("fake review failure")
            raise SystemExit(2)
        review_match = re.search(r"绝对路径：(\S+?/审查\.md)", prompt)
        done_match = re.search(r"绝对路径：(\S+?/审核完成\.md)", prompt)
        if not review_match or not done_match:
            print("MISSING REVIEW PATHS")
            raise SystemExit(4)
        verdict = "须修正" if mode == "revise" else "通过"
        review_path = Path(review_match.group(1))
        done_path = Path(done_match.group(1))
        attribution = re.search(
            r"^- 审核角色：[^\n]*\n- 审查时间：[^\n]*", prompt, re.M
        )
        header = (attribution.group(0) + "\n\n") if attribution else ""
        body = (
            header
            + "# 审查\n\n## 结论\n\n结论：**%s**\n\n"
            "## 逐项证据\n\n- 已核对自动推进交付\n\n"
            "## 问题清单\n\n%s\n"
            % (
                verdict,
                "- 补充边界证据" if mode == "revise" else "- 无",
            )
        )
        review_path.write_text(body, encoding="utf-8")
        done_path.write_text("审核完成\n", encoding="utf-8")
        record("review end %s pid=%d" % (task_id, os.getpid()))
        print("fake review " + verdict)
        raise SystemExit(0)

    match = re.search(r"执行子任务 (T\d+-\d+)：", prompt)
    subtask = match.group(1) if match else "unknown"
    attempt_match = re.search(r"attempt:\s*(\d+)", prompt)
    attempt_line = ("attempt: %s\n" % attempt_match.group(1)) if attempt_match else ""
    record("dispatch start %s pid=%d cwd=%s" % (subtask, os.getpid(), os.getcwd()))
    time.sleep(sleep_seconds)
    outdir = Path.cwd() / "子任务" / subtask
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "DONE.md").write_text(
        "# DONE %s\n\n"
        "## 做了什么\n\n- 假 Agent 写交付记录\n\n"
        "## 怎么验\n\n- 核对 DONE.md 与产物\n\n"
        "## 结果\n\n- pid=%d cwd=%s\n"
        "%s"
        % (subtask, os.getpid(), os.getcwd(), attempt_line),
        encoding="utf-8",
    )
    (outdir / "result.txt").write_text(
        "subtask=%s pid=%d\n" % (subtask, os.getpid()), encoding="utf-8"
    )
    record("dispatch end %s pid=%d" % (subtask, os.getpid()))
    if mode == "fail_dispatch" and subtask.endswith("-1"):
        print("fake dispatch failure")
        raise SystemExit(2)
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
                f"    title: 自动推进子任务 {index}",
                f"    goal: 写第 {index} 份产物",
                "    acceptance: DONE.md 包含编号",
                f"    status: {status}",
                "    agent_role: demo-builder",
            ]
        )
    return "\n".join(lines) + "\n"


class AdvanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: getattr(server, name) for name in GLOBALS}
        self._temp = tempfile.TemporaryDirectory()
        self._external = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.external_workdir = Path(self._external.name).resolve()
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

        self.script = self.root / "fake-advance-agent.py"
        self.script.write_text(FAKE_AGENT, encoding="utf-8")
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)
        agent_specs = {
            "fake-controller": "测试总控",
            "fake-dispatch": "测试分发 Agent",
            "fake-reviewer": "测试审核 Agent",
        }
        for agent_id, agent_name in agent_specs.items():
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
        self.write_role("controller", "controller", "fake-controller")
        self.write_role("demo-builder", "演示构建", "fake-dispatch")
        self.write_role("reviewer", "审核", "fake-reviewer")

        self.order_file = self.root / "advance-order.txt"
        self.held_file = self.root / "advance-held.lock"
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "T14_ORDER_FILE",
                "T14_HELD_FILE",
                "T14_AGENT_SLEEP",
                "T14_MODE",
            )
        }
        os.environ["T14_ORDER_FILE"] = str(self.order_file)
        os.environ["T14_HELD_FILE"] = str(self.held_file)
        os.environ["T14_AGENT_SLEEP"] = "0.3"
        os.environ["T14_MODE"] = "pass"

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
        server.RUNNER = runner.Runner(self.root, timeout_seconds=8.0)
        server.PORT = 18791
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
            deadline = time.time() + 15
            while time.time() < deadline:
                with server.ADVANCE_STATE_LOCK:
                    active_advance = server.ACTIVE_ADVANCE
                if active_advance is None and not server._active_dispatch_task():
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
            self._external.cleanup()

    def write_role(self, role_id: str, name: str, agent_id: str) -> None:
        (self.roles / f"{role_id}.yaml").write_text(
            f"id: {role_id}\n"
            f"name: {name}\n"
            f"description: 自动推进测试 {role_id}\n"
            f"agent_id: {agent_id}\n"
            "write_scope: []\n"
            "enabled: true\n"
            'notes: ""\n',
            encoding="utf-8",
        )

    def make_task(self, task_id: str, statuses: list) -> Path:
        project = self.projects / f"{task_id}_自动推进测试"
        docking = project / "对接"
        docking.mkdir(parents=True)
        (docking / "任务单.md").write_text(
            f"# {task_id} — 自动推进测试\n\n## 验收标准\n\n- 子任务交付\n- 审查通过\n",
            encoding="utf-8",
        )
        (docking / "subtasks.yaml").write_text(
            subtasks_text(task_id, statuses), encoding="utf-8"
        )
        (docking / "task.yaml").write_text(
            f"id: {task_id}\n"
            f"title: 自动推进测试 {task_id}\n"
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

    def wait_advance(self, task_id: str, timeout: float = 20.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = server._advance_state_snapshot(task_id)
            if state and str(state.get("state") or "") in {
                "completed",
                "stopped",
                "stopped_with_budget",
                "failed",
            }:
                if not server._active_advance_task():
                    return state
            time.sleep(0.03)
        self.fail(f"{task_id} 全自动推进没有在 {timeout} 秒内结束")

    def wait_registered(self, task_id: str, timeout: float = 12.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            project = server._find_registration_project(task_id)
            if project is not None:
                meta = server._read_task_meta(project, task_id)
                if str(meta.get("status") or "") in {
                    server.REGISTERED,
                    server.REGISTER_FAILED,
                }:
                    return meta
            time.sleep(0.03)
        self.fail(f"{task_id} 登记没有在 {timeout} 秒内结束")

    def order_lines(self) -> list:
        if not self.order_file.exists():
            return []
        return self.order_file.read_text(encoding="utf-8").splitlines()

    # ------------------------------------------------------------------

    def test_advance_dispatches_then_reviews_to_passed(self) -> None:
        project = self.make_task("T80", ["pending", "pending"])
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/advance/T80", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        self.assertEqual(payload["advance"]["state"], "running")
        self.assertEqual(payload["status_url"], "/api/task/T80/advance-status")

        status, body, _headers = self.request(
            "GET", "/api/task/T80/advance-status", accept="application/json"
        )
        self.assertEqual(status, 200)
        live = json.loads(body)
        self.assertEqual(live["state"], "running")
        self.assertTrue(live["running"])

        final = self.wait_advance("T80")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)
        self.assertIn("通过", final["message"])
        steps = [event["step"] for event in final["events"]]
        self.assertIn("dispatch", steps)
        self.assertIn("review", steps)
        self.assertEqual(
            [item.status for item in self.read_subtasks(project)],
            ["delivered", "delivered"],
        )
        review_path = project / "对接" / "审查.md"
        review_done = project / "对接" / "审核完成.md"
        self.assertTrue(review_path.is_file())
        self.assertTrue(review_done.is_file())
        self.assertIn("结论：**通过**", review_path.read_text(encoding="utf-8"))
        self.assertEqual(server._task_phase(server._find_task("T80")), "passed")
        self.assertFalse(server.RUNNER.lock_path.exists())

        history = server.RUNNER.history(10)
        dispatch_runs = [row for row in history if row.get("kind") == "dispatch"]
        review_runs = [row for row in history if row.get("kind") == "review"]
        self.assertEqual(len(dispatch_runs), 2)
        self.assertEqual(len(review_runs), 1)
        self.assertFalse(self.held_file.exists())

        lines = self.order_lines()
        self.assertFalse(any(line.startswith("overlap") for line in lines), lines)
        dispatch_starts = [line for line in lines if line.startswith("dispatch start")]
        review_starts = [line for line in lines if line.startswith("review start")]
        self.assertEqual(len(dispatch_starts), 2)
        self.assertEqual(len(review_starts), 1)
        self.assertLess(
            lines.index(dispatch_starts[-1]),
            lines.index(review_starts[0]),
        )

        status, page, _headers = self.request("GET", "/task/T80")
        self.assertEqual(status, 200)
        self.assertIn("当前阶段 · 已通过", page)
        self.assertIn("审核结论：通过", page)
        self.assertNotIn('id="advance-panel"', page)

        status, body, _headers = self.request(
            "GET", "/api/tasks", accept="application/json"
        )
        item = next(
            row for row in json.loads(body)["tasks"] if row["id"] == "T80"
        )
        self.assertEqual(item["phase"], "passed")
        self.assertEqual(item["subtask_done"], 2)

    def test_advance_stops_on_revise(self) -> None:
        os.environ["T14_MODE"] = "revise"
        project = self.make_task("T81", ["pending"])
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/advance/T81", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        final = self.wait_advance("T81")
        self.assertEqual(final["state"], "stopped", final)
        self.assertEqual(final["conclusion"], "revise", final)
        self.assertIn("须修正", final["message"])
        self.assertIn("人工", final["message"])
        self.assertTrue((project / "对接" / "审查.md").is_file())
        self.assertTrue((project / "对接" / "审核完成.md").is_file())
        task = server._find_task("T81")
        self.assertEqual(server._task_phase(task), "revise")
        available, reason = server._advance_availability(task)
        self.assertFalse(available)
        self.assertIn("审核结论", reason)

    def test_advance_stops_on_dispatch_failure(self) -> None:
        os.environ["T14_MODE"] = "fail_dispatch"
        project = self.make_task("T82", ["pending", "pending"])
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/advance/T82", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        final = self.wait_advance("T82")
        self.assertEqual(final["state"], "failed", final)
        self.assertIn("分发失败", final["message"])
        self.assertTrue(final["error"])
        items = self.read_subtasks(project)
        self.assertEqual(items[0].status, "failed")
        self.assertEqual(items[1].status, "pending")
        self.assertFalse((project / "对接" / "审查.md").exists())
        self.assertFalse(server._review_state_path("T82").exists())
        self.assertFalse(self.held_file.exists())
        steps = [event["step"] for event in final["events"]]
        self.assertIn("dispatch", steps)
        self.assertNotIn("review", steps)

        status, body, _headers = self.request(
            "GET", "/api/task/T82/advance-status", accept="application/json"
        )
        payload = json.loads(body)
        self.assertEqual(payload["state"], "failed")
        self.assertTrue(payload["terminal"])
        self.assertFalse(payload["running"])
        self.assertTrue(payload["error"])

    def test_advance_stops_on_review_failure(self) -> None:
        os.environ["T14_MODE"] = "fail_review"
        project = self.make_task("T83", ["delivered"])
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/advance/T83", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        final = self.wait_advance("T83")
        self.assertEqual(final["state"], "failed", final)
        self.assertIn("自动审核失败", final["message"])
        self.assertFalse((project / "对接" / "审查.md").exists())
        self.assertEqual(
            server._review_state_snapshot("T83").get("state"), "review_failed"
        )

    def test_publish_with_auto_advance_runs_without_manual_click(self) -> None:
        self.start_server()

        status, page, _headers = self.request("GET", "/new")
        self.assertEqual(status, 200)
        self.assertIn('name="auto_advance"', page)
        self.assertIn('id="field-auto-advance"', page)
        self.assertIn("发布后自动推进", page)
        checkbox = page.split('id="field-auto-advance"', 1)[1].split(">", 1)[0]
        self.assertIn("checked", checkbox)

        body = urlencode(
            {
                "title": "自动推进演示",
                "idea": "发布两个可自动执行的子任务，并自动审核到终态。",
                "workdir": str(self.external_workdir),
                "auto_advance": "1",
            }
        ).encode("utf-8")
        status, response_text, _headers = self.request(
            "POST",
            "/publish",
            accept="application/json",
            body=body,
        )
        self.assertEqual(status, 202, response_text)
        created = json.loads(response_text)
        task_id = str(created["id"])

        meta = self.wait_registered(task_id)
        self.assertEqual(meta.get("status"), server.REGISTERED, meta)
        self.assertEqual(str(meta.get("auto_advance") or ""), "1")

        final = self.wait_advance(task_id)
        self.assertEqual(final["state"], "completed", final)
        self.assertTrue(final["auto"])
        self.assertEqual(final["conclusion"], "passed")
        project = server._find_registration_project(task_id)
        assert project is not None
        self.assertTrue((project / "对接" / "审查.md").is_file())
        self.assertTrue((project / "对接" / "审核完成.md").is_file())
        self.assertEqual(server._task_phase(server._find_task(task_id)), "passed")
        self.assertEqual(
            [item.status for item in self.read_subtasks(project)],
            ["delivered", "delivered"],
        )
        lines = self.order_lines()
        self.assertFalse(any(line.startswith("overlap") for line in lines), lines)
        self.assertIn("review end " + task_id, "\n".join(lines))


    def _wait_dispatch_started(self, task_id: str, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if server._active_dispatch_task() == task_id:
                return
            time.sleep(0.03)
        self.fail(f"{task_id} 分发没有在 {timeout} 秒内启动")

    def test_advance_stop_terminates_current_agent_and_releases_lock(self) -> None:
        self.make_task("T90", ["pending", "pending"])
        os.environ["T14_AGENT_SLEEP"] = "6"
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/advance/T90", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        self._wait_dispatch_started("T90")
        self.assertTrue(server.RUNNER.lock_path.exists())

        status, page, _headers = self.request("GET", "/task/T90")
        self.assertEqual(status, 200)
        self.assertIn("停止推进", page)
        self.assertIn('action="/advance/T90/stop"', page)

        started = time.time()
        status, body, _headers = self.request(
            "POST", "/advance/T90/stop", accept="application/json", body=b""
        )
        elapsed = time.time() - started
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        final = payload["advance"]
        self.assertEqual(final["state"], "stopped", final)
        self.assertTrue(final.get("lock_released"), final)
        self.assertLess(elapsed, 15, "停止请求不应长时间卡住")
        self.assertFalse(server.RUNNER.lock_path.exists(), "停止后不得留下半锁")
        self.assertEqual(server._active_advance_task(), "")

        status, body, _headers = self.request(
            "GET", "/api/task/T90/advance-status", accept="application/json"
        )
        self.assertEqual(status, 200)
        live = json.loads(body)
        self.assertEqual(live["state"], "stopped")
        self.assertTrue(live["terminal"])
        self.assertFalse(live["running"])
        self.assertFalse(live["can_stop"])
        self.assertIn("停止", live["message"])
        self.assertFalse((self.projects / "T90_自动推进测试" / "对接" / "审查.md").exists())

    def test_advance_stop_during_review_releases_lock(self) -> None:
        self.make_task("T95", ["delivered"])
        os.environ["T14_AGENT_SLEEP"] = "6"
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/advance/T95", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        deadline = time.time() + 10
        while time.time() < deadline:
            if server._active_review_task() == "T95":
                break
            time.sleep(0.03)
        self.assertEqual(server._active_review_task(), "T95")
        self.assertTrue(server.RUNNER.lock_path.exists())

        status, body, _headers = self.request(
            "POST", "/advance/T95/stop", accept="application/json", body=b""
        )
        self.assertEqual(status, 200, body)
        final = json.loads(body)["advance"]
        self.assertEqual(final["state"], "stopped", final)
        self.assertTrue(final.get("lock_released"), final)
        self.assertFalse(server.RUNNER.lock_path.exists())
        self.assertEqual(server._active_advance_task(), "")

    def test_advance_budget_subtasks_stops_before_dispatch(self) -> None:
        server.ADVANCE_MAX_SUBTASKS = 1
        project = self.make_task("T91", ["pending", "pending"])
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/advance/T91", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        final = self.wait_advance("T91")
        self.assertEqual(final["state"], "stopped_with_budget", final)
        self.assertIn("子任务预算", str(final.get("error") or ""))
        self.assertFalse(server.RUNNER.lock_path.exists())
        self.assertEqual(
            [item.status for item in self.read_subtasks(project)],
            ["pending", "pending"],
        )
        self.assertFalse((project / "对接" / "审查.md").exists())

        status, page, _headers = self.request("GET", "/task/T91")
        self.assertEqual(status, 200)
        self.assertIn("预算用尽已停止", page)
        self.assertIn("子任务预算", page)

    def test_advance_budget_runtime_stops_and_releases_lock(self) -> None:
        server.ADVANCE_MAX_RUNTIME_SECONDS = 0.4
        os.environ["T14_AGENT_SLEEP"] = "4"
        self.make_task("T92", ["pending"])
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/advance/T92", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        self._wait_dispatch_started("T92")
        final = self.wait_advance("T92", timeout=20)
        self.assertEqual(final["state"], "stopped_with_budget", final)
        self.assertIn("运行时长", str(final.get("error") or ""))
        self.assertFalse(server.RUNNER.lock_path.exists(), "超预算后不得留下半锁")
        status, body, _headers = self.request(
            "GET", "/api/task/T92/advance-status", accept="application/json"
        )
        live = json.loads(body)
        self.assertEqual(live["state"], "stopped_with_budget")
        self.assertTrue(live["stopped_with_budget"])
        self.assertTrue(live["terminal"])
        self.assertFalse(live["running"])

    def test_advance_budget_revise_rounds_stops(self) -> None:
        server.ADVANCE_MAX_REVISE_ROUNDS = 3
        project = self.make_task("T93", ["pending"])
        meta = project / "对接" / "task.yaml"
        meta.write_text(
            meta.read_text(encoding="utf-8").rstrip("\n")
            + "\nrevise_round: 4\n",
            encoding="utf-8",
        )
        self.start_server()

        status, body, _headers = self.request(
            "POST", "/advance/T93", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        final = self.wait_advance("T93")
        self.assertEqual(final["state"], "stopped_with_budget", final)
        self.assertIn("修订轮次", str(final.get("error") or ""))
        self.assertFalse(server.RUNNER.lock_path.exists())


if __name__ == "__main__":
    unittest.main()
