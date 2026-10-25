#!/usr/bin/env python3
"""T10 子任务分发：预检、串行执行、原子状态更新与详情页。

所有测试在临时仓库中进行，不触碰真实的 项目/、协作/ 或 8787。
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
from urllib.parse import quote

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

FAKE_AGENT = '''#!/usr/bin/env python3
import os
import re
import sys
import time

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
match = re.search(r"执行子任务 (T\\d+-\\d+)：", prompt)
subtask = match.group(1) if match else "unknown"
order = os.environ.get("T10_ORDER_FILE", "")
held = os.environ.get("T10_HELD_FILE", "")


def record(text):
    if order:
        with open(order, "a", encoding="utf-8") as handle:
            handle.write(text + "\\n")
            handle.flush()


record("start %s %.3f" % (subtask, time.time()))
fd = None
if held:
    try:
        fd = os.open(held, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        record("overlap %s" % subtask)
        print("OVERLAP " + subtask)
        sys.exit(3)

try:
    outdir = os.path.join("子任务", subtask)
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "DONE.md"), "w", encoding="utf-8") as handle:
        handle.write(
            "# DONE %s\\n\\n"
            "## 做了什么\\n\\n- 假 Agent 执行子任务\\n\\n"
            "## 怎么验\\n\\n- 检查产物\\n\\n"
            "## 结果\\n\\ncwd=%s\\n"
            % (subtask, os.getcwd())
        )
    with open(os.path.join(outdir, "result.txt"), "w", encoding="utf-8") as handle:
        handle.write("subtask=%s\\n" % subtask)
    time.sleep(float(os.environ.get("T10_AGENT_SLEEP", "0.4")))
    print("fake agent " + subtask)
    if os.environ.get("T10_FAIL_SUBTASK") == subtask:
        record("end %s %.3f" % (subtask, time.time()))
        sys.exit(2)
finally:
    if fd is not None:
        os.close(fd)
        try:
            os.unlink(held)
        except OSError:
            pass

record("end %s %.3f" % (subtask, time.time()))
'''


def subtasks_text(task_id: str, roles: list) -> str:
    lines = ["subtasks:"]
    for index, role_id in enumerate(roles, start=1):
        lines.extend(
            [
                f"  - id: {task_id}-{index}",
                f"    title: 子任务 {index}",
                f"    goal: 完成第 {index} 件事",
                "    acceptance: DONE.md 落盘",
                "    status: pending",
                f"    agent_role: {role_id}",
            ]
        )
    return "\n".join(lines) + "\n"


class DispatchTest(unittest.TestCase):
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

        self.script = self.root / "fake-dispatch-agent.py"
        self.script.write_text(FAKE_AGENT, encoding="utf-8")
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)
        (self.agents / "demo-dispatch.yaml").write_text(
            "id: demo-dispatch\n"
            "name: 测试分发 Agent\n"
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
        (self.roles / "demo-builder.yaml").write_text(
            "id: demo-builder\n"
            "name: 测试构建\n"
            "description: 隔离分发测试\n"
            "agent_id: demo-dispatch\n"
            "write_scope: []\n"
            "enabled: true\n"
            'notes: ""\n',
            encoding="utf-8",
        )
        (self.roles / "demo-unbound.yaml").write_text(
            "id: demo-unbound\n"
            "name: 测试未绑定\n"
            "description: 预检拒绝测试\n"
            'agent_id: ""\n'
            "write_scope: []\n"
            "enabled: true\n"
            'notes: ""\n',
            encoding="utf-8",
        )

        self.order_file = self.root / "agent-order.txt"
        self.held_file = self.root / "agent-held.lock"
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("T10_ORDER_FILE", "T10_HELD_FILE", "T10_AGENT_SLEEP", "T10_FAIL_SUBTASK")
        }
        os.environ["T10_ORDER_FILE"] = str(self.order_file)
        os.environ["T10_HELD_FILE"] = str(self.held_file)
        os.environ["T10_AGENT_SLEEP"] = "0.4"
        os.environ.pop("T10_FAIL_SUBTASK", None)

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
        server.PORT = 18789
        with server.DISPATCH_STATE_LOCK:
            server.ACTIVE_DISPATCH = None
        self.httpd = None
        self.port = 0

    def tearDown(self) -> None:
        try:
            deadline = time.time() + 12
            while time.time() < deadline and server._active_dispatch_task():
                time.sleep(0.05)
            if server.RUNNER is not None:
                server.RUNNER.wait_for_idle(8.0)
            if self.httpd is not None:
                self.httpd.shutdown()
                self.httpd.server_close()
        finally:
            with server.DISPATCH_STATE_LOCK:
                server.ACTIVE_DISPATCH = None
            for name, value in self._saved.items():
                setattr(server, name, value)
            for key, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self._temp.cleanup()

    def make_task(self, task_id: str, roles: list, *, title: str = "分发测试") -> Path:
        project = self.projects / f"{task_id}_演示分发"
        docking = project / "对接"
        docking.mkdir(parents=True)
        (docking / "任务单.md").write_text(
            f"# {task_id} — {title}\n\n## 目标\n\n分发测试。\n", encoding="utf-8"
        )
        (docking / "subtasks.yaml").write_text(
            subtasks_text(task_id, roles), encoding="utf-8"
        )
        (docking / "task.yaml").write_text(
            f"id: {task_id}\n"
            f"title: 分发测试 {task_id}\n"
            f"workdir: {project / '工作区'}\n"
            "created_at: 2026-10-24T09:00:00+08:00\n"
            "status: registered\n",
            encoding="utf-8",
        )
        (docking / "登记完成.md").write_text(
            f"# 登记完成\n\n- 任务编号：{task_id}\n"
            f"- 子任务数量：{len(roles)}\n",
            encoding="utf-8",
        )
        return project

    def write_statuses(
        self, project: Path, task_id: str, statuses: list, *, old_logs: bool = False
    ) -> None:
        lines = ["subtasks:"]
        for index, status in enumerate(statuses, start=1):
            lines.extend(
                [
                    f"  - id: {task_id}-{index}",
                    f"    title: 子任务 {index}",
                    f"    goal: 完成第 {index} 件事",
                    "    acceptance: DONE.md 落盘",
                    f"    status: {status}",
                    "    agent_role: demo-builder",
                ]
            )
            if old_logs and status in {"delivered", "passed"}:
                lines.append(
                    f"    log: 项目/{project.name}/工作区/日志/old-{task_id}-{index}.log"
                )
                lines.append(
                    f"    started_at: 2026-10-24T18:00:0{index}+08:00"
                )
                lines.append(
                    f"    finished_at: 2026-10-24T18:00:1{index}+08:00"
                )
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
        (project / "对接" / "subtasks.yaml").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

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

    def wait_dispatch(self, task_id: str, timeout: float = 12.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = server._dispatch_state_snapshot(task_id)
            if (
                state
                and str(state.get("state") or "") in {"completed", "failed"}
                and not server._active_dispatch_task()
            ):
                return state
            time.sleep(0.03)
        self.fail(f"{task_id} 分发没有在 {timeout} 秒内结束")

    def read_subtasks(self, project: Path):
        subtasks, error = server._load_subtasks(project / "对接" / "subtasks.yaml")
        self.assertEqual(error, "")
        return subtasks

    def test_detail_page_has_dispatch_button_and_no_agent_picker(self) -> None:
        project = self.make_task("T50", ["demo-builder", "demo-builder"])
        self.find_task("T50")
        self.start_server()

        status, page, _headers = self.request("GET", "/task/T50")
        self.assertEqual(status, 200)
        self.assertNotIn("派给 " + "Agent", page)
        self.assertNotIn('name="agent_id"', page)
        self.assertNotIn('name="role_id"', page)
        self.assertIn("开始分发实现", page)
        self.assertIn("分发中…", page)  # 隐藏的轮询区域模板
        self.assertIn(">ID</th>", page)
        self.assertIn(">标题</th>", page)
        self.assertIn(">角色</th>", page)
        self.assertIn(">状态</th>", page)
        self.assertIn(">日志</th>", page)
        self.assertIn("/static/dispatch.js", page)
        self.assertTrue((project / "对接" / "subtasks.yaml").is_file())

    def test_preflight_409_lists_unbound_and_starts_nothing(self) -> None:
        project = self.make_task("T51", ["demo-builder", "demo-unbound"])
        self.find_task("T51")
        self.start_server()
        before = self.read_subtasks(project)

        status, body, _headers = self.request(
            "POST",
            "/dispatch/T51",
            accept="application/json",
            body=b"",
        )
        self.assertEqual(status, 409, body)
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual(len(payload["violations"]), 1)
        self.assertIn("T51-2", payload["violations"][0])
        self.assertIn("未绑定 Agent", payload["violations"][0])
        self.assertIn("T51-2", payload["message"])
        self.assertFalse(self.order_file.exists())
        self.assertFalse(server.RUNNER.lock_path.exists())
        self.assertEqual(server._active_dispatch_task(), "")
        after = self.read_subtasks(project)
        self.assertEqual(
            [(item.id, item.status) for item in before],
            [(item.id, item.status) for item in after],
        )
        self.assertTrue(all(item.status == "pending" for item in after))

    def test_serial_dispatch_updates_yaml_logs_and_runner_status(self) -> None:
        project = self.make_task("T52", ["demo-builder", "demo-builder"])
        self.find_task("T52")
        self.start_server()

        status, body, _headers = self.request(
            "POST",
            "/dispatch/T52",
            accept="application/json",
            body=b"",
        )
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        self.assertEqual(payload["dispatch"]["state"], "running")
        self.assertEqual(payload["status_url"], "/api/task/T52/dispatch-status")

        with self.assertRaises(server.ActionError) as caught:
            server._start_dispatch(self.find_task("T52"))
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("已有分发在跑", caught.exception.title)

        seen_running = set()
        deadline = time.time() + 12
        while time.time() < deadline:
            items = self.read_subtasks(project)
            for item in items:
                if item.status == "running":
                    seen_running.add(item.id)
            state = server._dispatch_state_snapshot("T52")
            if (
                state
                and str(state.get("state") or "") in {"completed", "failed"}
                and not server._active_dispatch_task()
            ):
                break
            time.sleep(0.02)
        self.assertEqual(seen_running, {"T52-1", "T52-2"})

        final = self.wait_dispatch("T52")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["success"], 2)
        self.assertEqual(final["failed"], 0)
        self.assertIn("成功 2 / 失败 0", final["message"])

        items = self.read_subtasks(project)
        self.assertEqual([item.status for item in items], ["delivered", "delivered"])
        for item in items:
            self.assertTrue(item.log, item)
            self.assertTrue(item.finished_at, item)
            log_path = server.RUNNER.resolve_log(item.log)
            self.assertIsNotNone(log_path, item.log)
            tail = server.RUNNER.tail_log(item.log, 50)
            self.assertTrue(any(f"fake agent {item.id}" in line for line in tail))
            self.assertTrue(
                any(f"subtask: {item.id}" in line for line in tail), tail
            )
            self.assertIn(f"dispatch-{item.id}", Path(item.log).name)
            done = project / "工作区" / "子任务" / item.id / "DONE.md"
            self.assertTrue(done.is_file(), done)

        order = self.order_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(order), 4, order)
        self.assertTrue(order[0].startswith("start T52-1"))
        self.assertTrue(order[1].startswith("end T52-1"))
        self.assertTrue(order[2].startswith("start T52-2"))
        self.assertTrue(order[3].startswith("end T52-2"))
        self.assertFalse(any(line.startswith("overlap") for line in order))
        self.assertFalse(self.held_file.exists())

        runner_status = server.RUNNER.read_status()
        self.assertEqual(runner_status.get("kind"), "dispatch")
        self.assertEqual(runner_status.get("subtask"), "T52-2")
        self.assertEqual(runner_status.get("state"), "success")
        history = server.RUNNER.history(10)
        dispatch_history = [
            record for record in history if record.get("kind") == "dispatch"
        ]
        self.assertEqual(len(dispatch_history), 2)
        self.assertFalse(server.RUNNER.lock_path.exists())

        status, body, _headers = self.request(
            "GET", "/api/task/T52/dispatch-status", accept="application/json"
        )
        self.assertEqual(status, 200)
        status_payload = json.loads(body)
        self.assertEqual(status_payload["state"], "completed")
        self.assertEqual(status_payload["success"], 2)
        self.assertEqual(status_payload["done"], 2)
        self.assertEqual(
            [item["status"] for item in status_payload["subtasks"]],
            ["delivered", "delivered"],
        )

        status, page, _headers = self.request("GET", "/task/T52")
        self.assertEqual(status, 200)
        self.assertIn("分发完成：成功 2 / 失败 0", page)
        self.assertIn("查看日志", page)
        self.assertIn("/log?path=", page)

        first_log = items[0].log
        status, log_text, _headers = self.request(
            "GET", "/log?path=" + quote(first_log, safe="")
        )
        self.assertEqual(status, 200)
        self.assertIn("fake agent T52-1", log_text)

    def test_failure_stops_following_subtasks(self) -> None:
        project = self.make_task("T53", ["demo-builder", "demo-builder"])
        self.find_task("T53")
        os.environ["T10_FAIL_SUBTASK"] = "T53-1"

        self.assertIsNotNone(server._start_dispatch(self.find_task("T53")))
        final = self.wait_dispatch("T53")
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["success"], 0)
        self.assertEqual(final["failed"], 1)

        items = self.read_subtasks(project)
        self.assertEqual(items[0].status, "failed")
        self.assertTrue(items[0].error)
        self.assertEqual(items[1].status, "pending")
        self.assertEqual(items[1].log, "")
        order = self.order_file.read_text(encoding="utf-8").splitlines()
        self.assertTrue(order[0].startswith("start T53-1"))
        self.assertTrue(order[1].startswith("end T53-1"))
        self.assertFalse(any("T53-2" in line for line in order))
        self.assertFalse(server.RUNNER.lock_path.exists())
        self.assertEqual(server.RUNNER.read_status().get("state"), "failed")

    def test_continue_dispatch_only_runs_pending(self) -> None:
        project = self.make_task("T56", ["demo-builder"] * 5)
        self.write_statuses(
            project,
            "T56",
            ["delivered", "delivered", "pending", "pending", "pending"],
            old_logs=True,
        )
        task = self.find_task("T56")
        self.start_server()

        status, page, _headers = self.request("GET", "/task/T56")
        self.assertEqual(status, 200)
        self.assertIn("继续分发（余 3）", page)
        self.assertIn("已交付 2 个；本次只处理待开始与失败的子任务", page)
        self.assertIn("不会重跑", page)

        status, body, _headers = self.request(
            "GET", "/api/task/T56/dispatch-status", accept="application/json"
        )
        self.assertEqual(status, 200)
        status_payload = json.loads(body)
        self.assertEqual(status_payload["action_label"], "继续分发（余 3）")
        self.assertEqual(status_payload["pending"], 3)
        self.assertEqual(status_payload["delivered"], 2)
        self.assertTrue(status_payload["can_start"])

        before = self.read_subtasks(project)
        status, body, _headers = self.request(
            "POST", "/dispatch/T56", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        final = self.wait_dispatch("T56")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["success"], 3)
        self.assertEqual(final["retried"], 0)

        after = self.read_subtasks(project)
        self.assertEqual([item.status for item in after], ["delivered"] * 5)
        self.assertEqual(after[0].log, before[0].log)
        self.assertEqual(after[1].log, before[1].log)
        self.assertEqual(after[0].finished_at, before[0].finished_at)
        self.assertTrue(after[2].log)
        self.assertTrue(after[3].log)
        self.assertTrue(after[4].log)

        order = self.order_file.read_text(encoding="utf-8").splitlines()
        starts = [line for line in order if line.startswith("start ")]
        self.assertEqual(
            [line.split()[1] for line in starts],
            ["T56-3", "T56-4", "T56-5"],
        )
        self.assertFalse(any("T56-1" in line or "T56-2" in line for line in order))

    def test_failed_subtasks_reset_and_retry(self) -> None:
        project = self.make_task("T57", ["demo-builder"] * 3)
        task = self.find_task("T57")
        self.start_server()
        os.environ["T10_FAIL_SUBTASK"] = "T57-1"
        self.assertIsNotNone(server._start_dispatch(task))
        first = self.wait_dispatch("T57")
        self.assertEqual(first["state"], "failed", first)
        items = self.read_subtasks(project)
        self.assertEqual(
            [item.status for item in items], ["failed", "pending", "pending"]
        )
        failed_log = items[0].log
        self.assertTrue(failed_log)

        status, page, _headers = self.request("GET", "/task/T57")
        self.assertEqual(status, 200)
        self.assertIn("继续分发（余 2 · 重试 1）", page)
        self.assertIn("分发已停止", page)

        os.environ.pop("T10_FAIL_SUBTASK", None)
        status, body, _headers = self.request(
            "POST", "/dispatch/T57", accept="application/json", body=b""
        )
        self.assertEqual(status, 202, body)
        final = self.wait_dispatch("T57")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["success"], 3)
        self.assertEqual(final["retried"], 1)

        items = self.read_subtasks(project)
        self.assertEqual([item.status for item in items], ["delivered"] * 3)
        self.assertTrue(all(item.log for item in items))
        self.assertTrue(all(item.finished_at for item in items))
        self.assertNotEqual(items[0].log, failed_log)
        order = self.order_file.read_text(encoding="utf-8").splitlines()
        starts = [line.split()[1] for line in order if line.startswith("start ")]
        self.assertEqual(starts, ["T57-1", "T57-1", "T57-2", "T57-3"])

    def test_missing_or_empty_subtasks_returns_400(self) -> None:
        project = self.projects / "T54_演示分发"
        (project / "对接").mkdir(parents=True)
        (project / "对接" / "任务单.md").write_text("# T54\n", encoding="utf-8")
        task = self.find_task("T54")
        with self.assertRaises(server.ActionError) as caught:
            server._start_dispatch(task)
        self.assertEqual(caught.exception.status, 400)
        self.assertFalse(server.RUNNER.lock_path.exists())

        (project / "对接" / "subtasks.yaml").write_text("subtasks:\n", encoding="utf-8")
        task = self.find_task("T54")
        with self.assertRaises(server.ActionError) as caught:
            server._start_dispatch(task)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("没有列出子任务", caught.exception.message)

    def test_update_subtask_text_preserves_comments_and_block_text(self) -> None:
        text = (
            "subtasks:\n"
            "  # 保留注释\n"
            "  - id: T55-1\n"
            "    title: 一\n"
            "    goal: >\n"
            "      第一行\n"
            "      - 这是块文本，不是新纪录\n"
            "    status: pending\n"
            "    agent_role: demo-builder\n"
            "    custom: keep-me\n"
            "  - id: T55-2\n"
            "    title: 二\n"
            "    status: pending\n"
        )
        updated = server._update_subtask_text(
            text,
            "T55-1",
            {
                "status": "running",
                "log": "项目/T55_演示分发/工作区/日志/dispatch-T55-1.log",
                "finished_at": "2026-10-24T18:00:00+08:00",
            },
        )
        self.assertIn("# 保留注释", updated)
        self.assertIn("custom: keep-me", updated)
        self.assertIn("- 这是块文本，不是新纪录", updated)
        parsed = server._parse_subtasks_text(updated)
        self.assertEqual(parsed[0].status, "running")
        self.assertEqual(
            parsed[0].log,
            "项目/T55_演示分发/工作区/日志/dispatch-T55-1.log",
        )
        self.assertEqual(parsed[0].finished_at, "2026-10-24T18:00:00+08:00")
        self.assertEqual(parsed[1].status, "pending")
        with self.assertRaises(server.ConfigError):
            server._update_subtask_text(text, "T55-9", {"status": "running"})


if __name__ == "__main__":
    unittest.main()
