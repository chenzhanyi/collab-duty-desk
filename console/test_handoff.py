#!/usr/bin/env python3
"""T18 交接闸门：缺件拦截、回叫补交、handoff_blocked 与重试体检。

所有测试都在临时仓库运行，不触碰真实 项目/、协作/ 或 8787。
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
mode = os.environ.get("T18_HANDOFF_MODE", "repair")
review_mode = os.environ.get("T18_REVIEW_MODE", "pass")
order = os.environ.get("T18_ORDER_FILE", "")
held = os.environ.get("T18_HELD_FILE", "")
sleep_seconds = float(os.environ.get("T18_AGENT_SLEEP", "0.12"))


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
    time.sleep(sleep_seconds)
    if "交接检查未通过" in prompt:
        role_match = re.search(r"责任角色 .*?（([^）]+)）", prompt)
        role = role_match.group(1) if role_match else "unknown"
        record("handoff start %s pid=%d" % (role, os.getpid()))
        if mode == "fail":
            record("handoff fail %s" % role)
            print("fake handoff only prints one line")
            raise SystemExit(0)

        project = Path.cwd()
        docking = project / "对接"
        task_match = re.search(r"任务：(T\d+)", prompt)
        task_id = task_match.group(1) if task_match else "T00"
        subtasks_path = docking / "subtasks.yaml"
        if not subtasks_path.is_file():
            subtasks_path.write_text(
                "subtasks:\n"
                "  - id: %s-1\n"
                "    title: 总控补交的子任务\n"
                "    goal: 完成交接测试\n"
                "    acceptance: DONE.md 与产物\n"
                "    status: pending\n"
                "    agent_role: demo-builder\n" % task_id,
                encoding="utf-8",
            )
        task_meta = docking / "task.yaml"
        if task_meta.is_file():
            meta_text = task_meta.read_text(encoding="utf-8")
            if not re.search(r"(?m)^status:\s*registered\s*$", meta_text):
                if re.search(r"(?m)^status:", meta_text):
                    meta_text = re.sub(
                        r"(?m)^status:.*$", "status: registered", meta_text
                    )
                else:
                    meta_text += "status: registered\n"
                task_meta.write_text(meta_text, encoding="utf-8")
        subtask_root = project / "工作区" / "子任务"
        if subtask_root.is_dir():
            for outdir in sorted(subtask_root.iterdir()):
                if not outdir.is_dir():
                    continue
                done = outdir / "DONE.md"
                content = done.read_text(encoding="utf-8") if done.is_file() else ""
                if not all(
                    marker in content
                    for marker in ("做了什么", "怎么验", "结果")
                ):
                    done.write_text(
                        "# DONE %s\n\n"
                        "## 做了什么\n\n- 交接补交\n\n"
                        "## 怎么验\n\n- 读取产物并核对\n\n"
                        "## 结果\n\n- 通过\n" % outdir.name,
                        encoding="utf-8",
                    )
                products = [
                    item
                    for item in outdir.iterdir()
                    if item.is_file() and item.name != "DONE.md"
                ]
                if not products:
                    (outdir / "artifact.txt").write_text(
                        "repaired product for %s\n" % outdir.name,
                        encoding="utf-8",
                    )

        for name in ("登记完成.md",):
            path = docking / name
            if not path.is_file():
                path.write_text(
                    "# 登记完成\n\n- 交接补交写入\n", encoding="utf-8"
                )
        if "review_complete" in prompt or "revise_" in prompt:
            review = docking / "审查.md"
            if not review.is_file():
                review.write_text(
                    "# 审查\n\n## 结论\n\n结论：**通过**\n\n"
                    "## 逐项证据\n\n- 交接补交核对\n",
                    encoding="utf-8",
                )
            done = docking / "审核完成.md"
            if not done.is_file():
                done.write_text("审核完成\n", encoding="utf-8")
            for history_match in re.finditer(r"round-(\d+)\.md", prompt):
                history = docking / "审查历史" / f"round-{history_match.group(1)}.md"
                if not history.is_file():
                    history.parent.mkdir(parents=True, exist_ok=True)
                    history.write_text(
                        "# 上一轮审查\n\n结论：**须修正**\n", encoding="utf-8"
                    )
        record("handoff end %s pid=%d" % (role, os.getpid()))
        print("fake handoff repaired " + role)
        raise SystemExit(0)

    if "你是审核角色" in prompt:
        task_match = re.search(r"审核任务 (T\d+)", prompt)
        task_id = task_match.group(1) if task_match else "unknown"
        record("review start %s pid=%d" % (task_id, os.getpid()))
        review_match = re.search(r"绝对路径：(\S+?/审查\.md)", prompt)
        done_match = re.search(r"绝对路径：(\S+?/审核完成\.md)", prompt)
        if not review_match or not done_match:
            print("MISSING REVIEW PATHS")
            raise SystemExit(4)
        review_path = Path(review_match.group(1))
        done_path = Path(done_match.group(1))
        review_path.write_text(
            "# 审查\n\n## 结论\n\n结论：**通过**\n\n"
            "## 逐项证据\n\n- 自动推进验收\n\n"
            "## 问题清单\n\n- 无\n",
            encoding="utf-8",
        )
        if review_mode != "no_done":
            done_path.write_text("审核完成\n", encoding="utf-8")
        record("review end %s pid=%d" % (task_id, os.getpid()))
        print("fake review done")
        raise SystemExit(0)

    match = re.search(r"执行子任务 (T\d+-\d+)：", prompt)
    subtask = match.group(1) if match else "unknown"
    record("dispatch start %s pid=%d" % (subtask, os.getpid()))
    outdir = Path.cwd() / "子任务" / subtask
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "artifact.txt").write_text(
        "dispatch product for %s\n" % subtask, encoding="utf-8"
    )
    # 故意不写 DONE.md：验收要求 G2 发现并回叫同一子任务角色补齐。
    record("dispatch end %s pid=%d" % (subtask, os.getpid()))
    print("fake dispatch without DONE " + subtask)
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
                f"    title: 交接测试子任务 {index}",
                f"    goal: 验证交接闸门 {index}",
                "    acceptance: DONE.md + 至少一个产物",
                f"    status: {status}",
                "    agent_role: demo-builder",
            ]
        )
    return "\n".join(lines) + "\n"


class HandoffTest(unittest.TestCase):
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

        self.script = self.root / "fake-handoff-agent.py"
        self.script.write_text(FAKE_AGENT, encoding="utf-8")
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)
        for agent_id, agent_name in (
            ("fake-controller", "测试总控"),
            ("fake-builder", "测试构建"),
            ("fake-reviewer", "测试审核"),
        ):
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
        self.write_role("controller", "总控", "fake-controller")
        self.write_role("demo-builder", "构建", "fake-builder")
        self.write_role("reviewer", "审核", "fake-reviewer")

        self.order_file = self.root / "handoff-order.txt"
        self.held_file = self.root / "handoff-held.lock"
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "T18_HANDOFF_MODE",
                "T18_REVIEW_MODE",
                "T18_ORDER_FILE",
                "T18_HELD_FILE",
                "T18_AGENT_SLEEP",
            )
        }
        os.environ["T18_HANDOFF_MODE"] = "repair"
        os.environ["T18_REVIEW_MODE"] = "pass"
        os.environ["T18_ORDER_FILE"] = str(self.order_file)
        os.environ["T18_HELD_FILE"] = str(self.held_file)
        os.environ["T18_AGENT_SLEEP"] = "0.12"

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
        server.REGISTRATION_TIMEOUT_SECONDS = 6.0
        server.RUNNER_TIMEOUT_SECONDS = 6.0
        server.RUNNER = runner.Runner(self.root, timeout_seconds=6.0)
        server.PORT = 18794
        with server.ADVANCE_STATE_LOCK:
            server.ACTIVE_ADVANCE = None
        with server.DISPATCH_STATE_LOCK:
            server.ACTIVE_DISPATCH = None
        with server.REVIEW_STATE_LOCK:
            server.ACTIVE_REVIEW = None
        with server.HANDOFF_STATE_LOCK:
            server.ACTIVE_HANDOFF = None
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
                    and not server._active_handoff_task()
                ):
                    break
                time.sleep(0.05)
            if server.RUNNER is not None:
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
            with server.HANDOFF_STATE_LOCK:
                server.ACTIVE_HANDOFF = None
            server.ACTIVE_REGISTRATIONS.clear()
            for name, value in self._saved.items():
                setattr(server, name, value)
            for key, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self._temp.cleanup()

    def record_evidence(self, name: str, text: str) -> None:
        target = os.environ.get("T18_EVIDENCE_DIR", "").strip()
        if not target:
            return
        directory = Path(target)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text, encoding="utf-8")

    def write_role(self, role_id: str, name: str, agent_id: str) -> None:
        (self.roles / f"{role_id}.yaml").write_text(
            f"id: {role_id}\n"
            f"name: {name}\n"
            f"description: 交接闸门测试 {role_id}\n"
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
        register_done: bool = True,
        delivery_files: bool = True,
        product: bool = True,
        review: str = "",
        review_done: bool = False,
        revise_round: int = 0,
    ) -> Path:
        project = self.projects / f"{task_id}_交接闸门测试"
        docking = project / "对接"
        docking.mkdir(parents=True)
        (docking / "任务单.md").write_text(
            f"# {task_id} — 交接闸门测试\n\n## 验收标准\n\n- 文件齐全\n",
            encoding="utf-8",
        )
        (docking / "subtasks.yaml").write_text(
            subtasks_text(task_id, statuses), encoding="utf-8"
        )
        (docking / "task.yaml").write_text(
            f"id: {task_id}\n"
            f"title: 交接闸门测试 {task_id}\n"
            f"workdir: {project / '工作区'}\n"
            "created_at: 2026-10-24T09:00:00+08:00\n"
            "status: registered\n"
            f"revise_round: {revise_round}\n",
            encoding="utf-8",
        )
        if register_done:
            (docking / "登记完成.md").write_text(
                f"# 登记完成\n\n- 任务编号：{task_id}\n",
                encoding="utf-8",
            )
        if delivery_files:
            for index, status in enumerate(statuses, start=1):
                if status not in {"delivered", "passed"}:
                    continue
                outdir = project / "工作区" / "子任务" / f"{task_id}-{index}"
                outdir.mkdir(parents=True, exist_ok=True)
                (outdir / "DONE.md").write_text(
                    f"# DONE {task_id}-{index}\n\n"
                    "## 做了什么\n\n- 预置交付\n\n"
                    "## 怎么验\n\n- 核对文件\n\n"
                    "## 结果\n\n- 通过\n",
                    encoding="utf-8",
                )
                if product:
                    (outdir / "result.txt").write_text(
                        f"{task_id}-{index}\n", encoding="utf-8"
                    )
        if review:
            (docking / "审查.md").write_text(review, encoding="utf-8")
        if review_done:
            (docking / "审核完成.md").write_text("审核完成\n", encoding="utf-8")
        return project

    def start_server(self) -> None:
        self.httpd = server.ConsoleServer(("127.0.0.1", 0), server.ConsoleHandler)
        self.port = self.httpd.server_address[1]
        server.PORT = self.port
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()

    def request(self, method: str, path: str, *, accept: str = ""):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {}
        if accept:
            headers["Accept"] = accept
        connection.request(method, path, body=b"", headers=headers)
        response = connection.getresponse()
        data = response.read().decode("utf-8", errors="replace")
        status = response.status
        connection.close()
        return status, data

    def order_lines(self) -> list:
        if not self.order_file.exists():
            return []
        return self.order_file.read_text(encoding="utf-8").splitlines()

    def wait_advance(self, task_id: str, timeout: float = 25.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = server._advance_state_snapshot(task_id)
            if state and str(state.get("state") or "") in {
                "completed",
                "stopped",
                "failed",
                "handoff_blocked",
            }:
                if not server._active_advance_task():
                    return state
            time.sleep(0.03)
        self.fail(f"{task_id} 全自动推进没有在 {timeout} 秒内结束")

    def wait_dispatch(self, task_id: str, timeout: float = 20.0) -> dict:
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

    def wait_review(self, task_id: str, timeout: float = 20.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = server._review_state_snapshot(task_id)
            if (
                state
                and str(state.get("state") or "")
                in {"completed", "review_failed"}
                and not server._active_review_task()
            ):
                return state
            time.sleep(0.03)
        self.fail(f"{task_id} 审核没有在 {timeout} 秒内结束")

    # ------------------------------------------------------------------

    def test_g2_missing_done_is_repaired_and_advance_continues(self) -> None:
        project = self.make_task("T70", ["pending"])
        self.start_server()

        status, body = self.request(
            "POST", "/advance/T70", accept="application/json"
        )
        self.assertEqual(status, 202, body)
        final = self.wait_advance("T70")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed")

        outdir = project / "工作区" / "子任务" / "T70-1"
        done_text = (outdir / "DONE.md").read_text(encoding="utf-8")
        for marker in ("做了什么", "怎么验", "结果"):
            self.assertIn(marker, done_text)
        self.assertTrue((outdir / "artifact.txt").is_file())
        self.assertTrue((project / "对接" / "审查.md").is_file())
        self.assertTrue((project / "对接" / "审核完成.md").is_file())

        log_texts = [
            path.read_text(encoding="utf-8")
            for path in (project / "工作区" / "日志").glob("handoff-*.log")
        ]
        g2_logs = [text for text in log_texts if "补交 #1" in text]
        self.assertTrue(g2_logs, log_texts)
        log_text = g2_logs[0]
        self.assertIn("G2", log_text)
        self.assertIn("fake-builder", log_text)
        self.assertIn("结论：通过", log_text)

        lines = self.order_lines()
        self.assertFalse(any(line.startswith("overlap") for line in lines), lines)
        dispatch_index = next(
            index
            for index, line in enumerate(lines)
            if line.startswith("dispatch start")
        )
        handoff_index = next(
            index
            for index, line in enumerate(lines)
            if line.startswith("handoff start demo-builder")
        )
        review_index = next(
            index
            for index, line in enumerate(lines)
            if line.startswith("review start")
        )
        self.assertLess(dispatch_index, handoff_index)
        self.assertLess(handoff_index, review_index)
        self.assertFalse(self.held_file.exists())
        self.assertFalse(server.RUNNER.lock_path.exists())
        self.assertEqual(server._task_phase(server._find_task("T70")), "passed")
        self.record_evidence(
            "01-g2-repair-advance.json",
            json.dumps(final, ensure_ascii=False, indent=2) + "\n",
        )
        self.record_evidence(
            "01-g2-repair-handoff.log",
            log_text,
        )
        self.record_evidence(
            "01-g2-repair-order.txt",
            "\n".join(lines) + "\n",
        )

    def test_two_failed_supplements_block_then_retry_check_recovers(self) -> None:
        os.environ["T18_HANDOFF_MODE"] = "fail"
        project = self.make_task("T71", ["pending"])
        self.start_server()

        status, body = self.request(
            "POST", "/advance/T71", accept="application/json"
        )
        self.assertEqual(status, 202, body)
        final = self.wait_advance("T71")
        self.assertEqual(final["state"], "handoff_blocked", final)
        self.assertIn("交接体检未通过", final["message"])
        self.assertTrue(final["handoff_missing"])
        self.assertFalse(server._active_advance_task())
        self.assertFalse(self.held_file.exists())

        task = server._find_task("T71")
        assert task is not None
        self.assertEqual(server._task_phase(task), "handoff_blocked")
        self.assertEqual(task.status, server.STATUS_NEEDS_ACTION)
        handoff = server._handoff_state_snapshot("T71")
        self.assertEqual(handoff["state"], "blocked")
        self.assertEqual(len(handoff["agent_runs"]), 2)
        self.assertEqual(handoff["attempts"]["G2:T71-1"], 2)

        status, body = self.request(
            "GET", "/api/tasks", accept="application/json"
        )
        item = next(
            row for row in json.loads(body)["tasks"] if row["id"] == "T71"
        )
        self.assertEqual(item["phase"], "handoff_blocked")
        self.assertEqual(item["status"], server.STATUS_NEEDS_ACTION)
        self.assertTrue(item["handoff_blocked"])

        status, page = self.request("GET", "/task/T71")
        self.assertEqual(status, 200)
        self.assertIn("需要人工处理", page)
        self.assertIn("重试体检", page)
        self.assertIn('id="handoff-retry"', page)
        self.assertIn("DONE.md", page)
        start = page.find('<section class="dispatch-panel handoff-panel"')
        end = page.find("</section>", start)
        if start >= 0 and end > start:
            self.record_evidence(
                "02-blocked-handoff-panel.html",
                page[start : end + len("</section>")] + "\n",
            )
        self.record_evidence(
            "02-blocked-advance.json",
            json.dumps(final, ensure_ascii=False, indent=2) + "\n",
        )
        self.record_evidence(
            "02-blocked-handoff.json",
            json.dumps(handoff, ensure_ascii=False, indent=2) + "\n",
        )
        self.record_evidence(
            "02-blocked-order.txt",
            "\n".join(self.order_lines()) + "\n",
        )
        self.record_evidence(
            "02-blocked-process.txt",
            "active_advance=%r\nactive_handoff=%r\nlock_exists=%r\n"
            % (
                server._active_advance_task(),
                server._active_handoff_task(),
                server.RUNNER.lock_path.exists(),
            ),
        )

        status, body = self.request(
            "POST", "/handoff/retry/T71", accept="application/json"
        )
        self.assertEqual(status, 409, body)
        retried = json.loads(body)
        self.assertFalse(retried["passed"])
        self.assertEqual(retried["state"], "blocked")
        self.record_evidence(
            "02-retry-still-blocked.json",
            json.dumps(retried, ensure_ascii=False, indent=2) + "\n",
        )

        os.environ["T18_HANDOFF_MODE"] = "repair"
        status, body = self.request(
            "POST", "/handoff/retry/T71", accept="application/json"
        )
        self.assertEqual(status, 200, body)
        retried = json.loads(body)
        self.assertTrue(retried["passed"])
        self.record_evidence(
            "02-retry-passed.json",
            json.dumps(retried, ensure_ascii=False, indent=2) + "\n",
        )
        self.assertTrue((project / "工作区" / "子任务" / "T71-1" / "DONE.md").is_file())
        task = server._find_task("T71")
        assert task is not None
        self.assertEqual(server._task_phase(task), "review_ready")
        self.assertEqual(server._advance_state_snapshot("T71")["state"], "stopped")
        self.assertFalse(server._handoff_blocked_for("T71"))

        status, page = self.request("GET", "/task/T71?handoff=passed")
        self.assertEqual(status, 200)
        self.assertIn("重试体检通过", page)

    def test_g1_g3_g4_missing_samples_block_with_responsible_roles(self) -> None:
        os.environ["T18_HANDOFF_MODE"] = "fail"
        g1_task_path = self.make_task(
            "T72", ["pending"], register_done=False
        )
        g1_task = server._find_task("T72")
        assert g1_task is not None
        g1_checks = server._handoff_checks_for(g1_task, "dispatch")
        g1 = next(check for check in g1_checks if check["gate"] == "G1")
        self.assertFalse(g1["passed"])
        self.assertEqual(g1["owner_role"], "controller")
        self.assertIn("对接/登记完成.md", g1["missing"])
        g1_result = server._run_handoff_gates(
            g1_task, "dispatch", source="test"
        )
        self.assertEqual(g1_result["state"], "blocked")
        self.assertEqual(len(g1_result["agent_runs"]), 2)
        self.assertTrue(
            all(run["role"] == "controller" for run in g1_result["agent_runs"])
        )

        self.make_task("T73", ["delivered"], review="", review_done=False)
        g3_task = server._find_task("T73")
        assert g3_task is not None
        g3 = server._gate_g3(g3_task)
        self.assertFalse(g3["passed"])
        self.assertEqual(g3["owner_role"], "reviewer")
        self.assertIn("对接/审查.md", g3["missing"])
        self.assertIn("对接/审核完成.md", g3["missing"])
        g3_result = server._run_handoff_gates(
            g3_task, "review_complete", source="test"
        )
        self.assertEqual(g3_result["state"], "blocked")
        self.assertTrue(
            all(run["role"] == "reviewer" for run in g3_result["agent_runs"])
        )

        self.make_task(
            "T74",
            ["delivered"],
            review="# 审查\n\n结论：**通过**\n",
            review_done=True,
            revise_round=1,
        )
        g4_task = server._find_task("T74")
        assert g4_task is not None
        g4 = server._gate_g4(g4_task, 1)
        self.assertFalse(g4["passed"])
        self.assertEqual(g4["owner_role"], "reviewer")
        self.assertIn("对接/审查历史/round-1.md", g4["missing"])
        g4_result = server._run_handoff_gates(
            g4_task, "revise_complete", round_no=1, source="test"
        )
        self.assertEqual(g4_result["state"], "blocked")
        self.assertTrue(
            any(run["role"] == "reviewer" for run in g4_result["agent_runs"])
        )
        # 只在内存里检查样例，temporary root 会整体清理；不触碰真实 T15。
        self.assertTrue(g1_task_path.is_dir())
        self.record_evidence(
            "03-gate-samples.json",
            json.dumps(
                {
                    "G1": g1,
                    "G1_result": g1_result,
                    "G3": g3,
                    "G3_result": g3_result,
                    "G4": g4,
                    "G4_result": g4_result,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def test_g3_missing_done_is_repaired_after_review(self) -> None:
        os.environ["T18_REVIEW_MODE"] = "no_done"
        project = self.make_task("T75", ["delivered"])
        self.start_server()

        status, body = self.request(
            "POST", "/review/T75", accept="application/json"
        )
        self.assertEqual(status, 202, body)
        deadline = time.time() + 20
        final = {}
        while time.time() < deadline:
            final = server._review_state_snapshot("T75")
            if str(final.get("state") or "") in {"completed", "review_failed"}:
                if not server._active_review_task():
                    break
            time.sleep(0.03)
        self.assertEqual(final.get("state"), "completed", final)
        self.assertTrue((project / "对接" / "审核完成.md").is_file())
        handoff = server._handoff_state_snapshot("T75")
        self.assertEqual(handoff["state"], "passed")
        self.assertIn("G3", "".join(
            str(check.get("gate")) for check in handoff.get("checks") or []
        ))
        self.assertTrue(
            any(run.get("role") == "reviewer" for run in handoff.get("agent_runs") or [])
        )

    def test_g1_missing_subtasks_is_repaired_before_advance(self) -> None:
        project = self.make_task("T78", ["pending"])
        (project / "对接" / "subtasks.yaml").unlink()
        self.start_server()

        status, body = self.request(
            "POST", "/advance/T78", accept="application/json"
        )
        self.assertEqual(status, 202, body)
        final = self.wait_advance("T78")
        self.assertEqual(final["state"], "completed", final)
        self.assertTrue((project / "对接" / "subtasks.yaml").is_file())
        subtasks, error = server._load_subtasks(project / "对接" / "subtasks.yaml")
        self.assertEqual(error, "")
        self.assertEqual([item.id for item in subtasks], ["T78-1"])
        self.assertTrue(
            (project / "工作区" / "子任务" / "T78-1" / "DONE.md").is_file()
        )
        log_texts = [
            path.read_text(encoding="utf-8")
            for path in (project / "工作区" / "日志").glob("handoff-*.log")
        ]
        self.assertTrue(any("G1" in text and "subtasks.yaml" in text for text in log_texts))
        self.assertFalse(
            any(line.startswith("overlap") for line in self.order_lines())
        )

    def test_manual_dispatch_checks_g1_and_manual_review_checks_g2(self) -> None:
        project = self.make_task("T77", ["pending"], register_done=False)
        self.start_server()

        status, body = self.request(
            "POST", "/dispatch/T77", accept="application/json"
        )
        self.assertEqual(status, 202, body)
        final = self.wait_dispatch("T77")
        self.assertEqual(final["state"], "completed", final)
        self.assertTrue((project / "对接" / "登记完成.md").is_file())
        self.assertTrue(
            (project / "工作区" / "子任务" / "T77-1" / "artifact.txt").is_file()
        )
        self.assertFalse(
            (project / "工作区" / "子任务" / "T77-1" / "DONE.md").is_file()
        )

        status, body = self.request(
            "POST", "/review/T77", accept="application/json"
        )
        self.assertEqual(status, 202, body)
        reviewed = self.wait_review("T77")
        self.assertEqual(reviewed["state"], "completed", reviewed)
        self.assertTrue(
            (project / "工作区" / "子任务" / "T77-1" / "DONE.md").is_file()
        )
        self.assertTrue((project / "对接" / "审查.md").is_file())
        self.assertTrue((project / "对接" / "审核完成.md").is_file())
        handoff = server._handoff_state_snapshot("T77")
        self.assertEqual(handoff["stage"], "review_complete")
        log_texts = [
            path.read_text(encoding="utf-8")
            for path in (project / "工作区" / "日志").glob("handoff-*.log")
        ]
        self.assertTrue(any("G2" in text and "补交 #1" in text for text in log_texts))
        self.assertFalse(
            any(line.startswith("overlap") for line in self.order_lines())
        )
        self.record_evidence(
            "04-manual-order.txt",
            "\n".join(self.order_lines()) + "\n",
        )
        self.record_evidence(
            "04-manual-review-state.json",
            json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n",
        )

    def test_g3_rejects_review_files_that_were_not_rewritten_this_round(self) -> None:
        self.make_task(
            "T81",
            ["delivered"],
            review="# 审查\n\n结论：**通过**\n",
            review_done=True,
        )
        task = server._find_task("T81")
        assert task is not None
        review_path = task.docking_dir / "审查.md"
        done_path = task.docking_dir / "审核完成.md"
        review_baseline = server._file_signature(review_path)
        done_baseline = server._file_signature(done_path)
        stale = server._gate_g3(
            task,
            review_baseline=review_baseline,
            review_done_baseline=done_baseline,
            require_new=True,
        )
        self.assertFalse(stale["passed"])
        self.assertTrue(
            any("本轮未重新写入" in item for item in stale["missing"]),
            stale,
        )
        time.sleep(0.01)
        review_path.write_text(
            "# 审查\n\n结论：**通过**\n\n- 本轮重写\n", encoding="utf-8"
        )
        time.sleep(0.01)
        done_path.write_text("审核完成（本轮）\n", encoding="utf-8")
        fresh = server._gate_g3(
            task,
            review_baseline=review_baseline,
            review_done_baseline=done_baseline,
            require_new=True,
        )
        self.assertTrue(fresh["passed"], fresh)

    def test_blocked_g3_can_be_retried_and_review_is_completed(self) -> None:
        os.environ["T18_HANDOFF_MODE"] = "fail"
        os.environ["T18_REVIEW_MODE"] = "no_done"
        project = self.make_task("T79", ["delivered"])
        self.start_server()

        status, body = self.request(
            "POST", "/review/T79", accept="application/json"
        )
        self.assertEqual(status, 202, body)
        failed = self.wait_review("T79")
        self.assertEqual(failed["state"], "review_failed", failed)
        self.assertTrue(failed.get("handoff_blocked"))
        task = server._find_task("T79")
        assert task is not None
        self.assertEqual(server._task_phase(task), server.PHASE_HANDOFF_BLOCKED)
        self.assertFalse((project / "对接" / "审核完成.md").is_file())

        os.environ["T18_HANDOFF_MODE"] = "repair"
        status, body = self.request(
            "POST", "/handoff/retry/T79", accept="application/json"
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["passed"])
        self.assertTrue((project / "对接" / "审核完成.md").is_file())
        completed = server._review_state_snapshot("T79")
        self.assertEqual(completed["state"], "completed", completed)
        self.assertEqual(completed["conclusion"], "passed")
        task = server._find_task("T79")
        assert task is not None
        self.assertEqual(server._task_phase(task), server.PHASE_PASSED)
        self.assertEqual(task.status, server.STATUS_PASSED)

    def test_status_comes_from_phase_and_has_delivery_uses_evidence(self) -> None:
        project = self.make_task(
            "T76", ["delivered"] * 5, product=False
        )
        task = server._find_task("T76")
        assert task is not None
        self.assertTrue(server._subtasks_all_delivered(task))
        self.assertEqual(server._task_phase(task), server.PHASE_REVIEW_READY)
        self.assertEqual(task.status, server.STATUS_DELIVERED)
        payload = server._tasks_api_payload([task])
        item = payload["tasks"][0]
        self.assertEqual(item["phase"], "review_ready")
        self.assertEqual(item["status"], server.STATUS_DELIVERED)
        self.assertTrue(item["has_delivery"])
        self.assertIn("全部子任务已交付", item["delivery_evidence"])
        page = server._render_task_detail(task)
        self.assertIn("当前阶段 · 待审核", page)
        self.assertIn("已交付", page)
        self.assertFalse((project / "对接" / "审查.md").exists())


if __name__ == "__main__":
    unittest.main()
