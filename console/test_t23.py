#!/usr/bin/env python3
"""T23 外部审计 P2 回归：F06–F09 与无子任务整单重修。

所有用例只在临时目录构造任务，不触碰真实 项目/、协作/ 或 8787。
设置 T23_EVIDENCE_DIR 时可把每项证据落到 工作区/证据/。
"""

from __future__ import annotations

import dataclasses
import hashlib
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
from unittest import mock
from urllib.parse import urlencode

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import runner  # noqa: E402
import server  # noqa: E402

LEGACY_REVIEW_TS = time.mktime((2026, 10, 26, 0, 0, 0, 0, 0, -1))


def age_legacy_review(project: Path) -> None:
    for name in ("审查.md", "审核完成.md"):
        path = Path(project) / "对接" / name
        if path.is_file():
            os.utime(path, (LEGACY_REVIEW_TS, LEGACY_REVIEW_TS))


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
    "RUNNER_TIMEOUT_SECONDS",
    "REGISTRATION_TIMEOUT_SECONDS",
)

FAKE_AGENT = r'''#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

prompt = sys.argv[1] if len(sys.argv) > 1 else ""
prompt_dir = os.environ.get("T23_PROMPT_DIR", "")
order = os.environ.get("T23_ORDER_FILE", "")
sleep_seconds = float(os.environ.get("T23_AGENT_SLEEP", "0.05"))


def record(text):
    if order:
        with open(order, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
            handle.flush()


def save_prompt(kind):
    if not prompt_dir:
        return
    directory = Path(prompt_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("%s-%d-%d.txt" % (kind, time.time_ns(), os.getpid()))
    target.write_text(prompt, encoding="utf-8")


time.sleep(sleep_seconds)

if "你是审核角色" in prompt:
    save_prompt("review")
    review_match = re.search(r"绝对路径：(\S+?/审查\.md)", prompt)
    done_match = re.search(r"绝对路径：(\S+?/审核完成\.md)", prompt)
    if not review_match or not done_match:
        print("MISSING REVIEW PATHS")
        raise SystemExit(4)
    verdict = os.environ.get("T23_REVIEW_VERDICT", "通过")
    review_path = Path(review_match.group(1))
    done_path = Path(done_match.group(1))
    attribution = re.search(
        r"^- 审核角色：[^\n]*\n- 审查时间：[^\n]*", prompt, re.M
    )
    header = (attribution.group(0) + "\n\n") if attribution else ""
    review_path.write_text(
        header
        + "# 审查\n\n## 结论\n\n结论：**%s**\n\n"
        "## 逐项证据\n\n- 整单重修假审核\n\n"
        "## 问题清单\n\n- 无\n" % verdict,
        encoding="utf-8",
    )
    done_path.write_text("审核完成\n", encoding="utf-8")
    record("review %s %s" % (verdict, time.time()))
    print("fake review " + verdict)
    raise SystemExit(0)

if "进入整单重修" in prompt:
    save_prompt("whole")
    record("whole start %.3f" % time.time())
    done_match = re.search(
        r"新的 DONE\.md（绝对路径）：([^\n]+)", prompt
    )
    if not done_match:
        print("MISSING WHOLE DONE PATH")
        raise SystemExit(5)
    done_path = Path(done_match.group(1).strip())
    done_path.parent.mkdir(parents=True, exist_ok=True)
    done_path.write_text(
        "# DONE 整单重修\n\n"
        "## 做了什么\n\n- 按任务单重做整单\n\n"
        "## 怎么验\n\n- 复跑回归并逐条核对审查问题\n\n"
        "## 结果\n\n- 通过\n",
        encoding="utf-8",
    )
    (Path.cwd() / "整单重修产物.txt").write_text(
        "owner rework artifact\n", encoding="utf-8"
    )
    record("whole end %.3f" % time.time())
    print("fake whole rework")
    raise SystemExit(0)

if "执行子任务" in prompt:
    save_prompt("dispatch")
    match = re.search(r"执行子任务 (T\d+-\d+)：", prompt)
    subtask = match.group(1) if match else "unknown"
    attempt_match = re.search(r"attempt:\s*(\d+)", prompt)
    attempt_line = (
        "attempt: %s\n" % attempt_match.group(1) if attempt_match else ""
    )
    if os.environ.get("T23_OMIT_ATTEMPT") == "1":
        attempt_line = ""
    run_id_match = re.search(r"run_id[:：]\s*(\S+)", prompt)
    run_id_line = (
        "run_id: %s\n" % run_id_match.group(1) if run_id_match else ""
    )
    record("dispatch start %s %.3f" % (subtask, time.time()))
    hold = os.environ.get("T23_AGENT_HOLD_FILE", "")
    if hold:
        Path(hold + ".started").write_text("started\n", encoding="utf-8")
        deadline = time.time() + 10
        while time.time() < deadline and not Path(hold + ".release").exists():
            time.sleep(0.02)
    outdir = Path.cwd() / "子任务" / subtask
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "DONE.md").write_text(
        "# DONE %s\n\n"
        "## 做了什么\n\n- 假 Agent 交付\n\n"
        "## 怎么验\n\n- 检查产物\n\n"
        "## 结果\n\n- 通过\n"
        "%s%s" % (subtask, attempt_line, run_id_line),
        encoding="utf-8",
    )
    (outdir / "artifact.txt").write_text(
        "artifact %s\n" % subtask, encoding="utf-8"
    )
    record("dispatch end %s %.3f" % (subtask, time.time()))
    print("fake dispatch " + subtask)
    raise SystemExit(0)

print("unknown prompt")
raise SystemExit(3)
'''


class T23Base(unittest.TestCase):
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

        self.script = self.root / "fake-t23-agent.py"
        self.script.write_text(FAKE_AGENT, encoding="utf-8")
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)
        (self.agents / "demo-t23.yaml").write_text(
            "id: demo-t23\n"
            "name: T23 测试 Agent\n"
            "command: "
            + str(self.script)
            + "\n"
            'args_template: \'"{prompt}"\'\n'
            f"cwd: {self.root}\n"
            "installed: true\n"
            "version: test\n"
            'notes: ""\n',
            encoding="utf-8",
        )
        for role_id, name in (
            ("designer", "测试设计"),
            ("reviewer", "测试审核"),
            ("builder", "测试构建"),
        ):
            (self.roles / f"{role_id}.yaml").write_text(
                f"id: {role_id}\n"
                f"name: {name}\n"
                "description: T23 隔离回归\n"
                "agent_id: demo-t23\n"
                "write_scope: []\n"
                "enabled: true\n"
                'notes: ""\n',
                encoding="utf-8",
            )

        self.prompt_dir = self.root / "prompts"
        self.order_file = self.root / "order.txt"
        self.evidence_dir = os.environ.get("T23_EVIDENCE_DIR", "").strip()
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "T23_PROMPT_DIR",
                "T23_ORDER_FILE",
                "T23_AGENT_SLEEP",
                "T23_OMIT_ATTEMPT",
                "T23_REVIEW_VERDICT",
                "T23_AGENT_HOLD_FILE",
            )
        }
        os.environ["T23_PROMPT_DIR"] = str(self.prompt_dir)
        os.environ["T23_ORDER_FILE"] = str(self.order_file)
        os.environ["T23_AGENT_SLEEP"] = "0.05"
        os.environ.pop("T23_OMIT_ATTEMPT", None)
        os.environ.pop("T23_REVIEW_VERDICT", None)
        os.environ.pop("T23_AGENT_HOLD_FILE", None)

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
        server.RUNNER_TIMEOUT_SECONDS = 8.0
        server.REGISTRATION_TIMEOUT_SECONDS = 8.0
        server.RUNNER = runner.Runner(self.root, timeout_seconds=8.0)
        server.PORT = 18791
        with server.TASK_LIFECYCLE_LOCKS_GUARD:
            server.TASK_LIFECYCLE_LOCKS.clear()
        server.TASK_LIFECYCLE_STATE.depth = 0
        with server.DISPATCH_STATE_LOCK:
            server.ACTIVE_DISPATCH = None
        with server.REVIEW_STATE_LOCK:
            server.ACTIVE_REVIEW = None
        with server.ADVANCE_STATE_LOCK:
            server.ACTIVE_ADVANCE = None
        with server.HANDOFF_STATE_LOCK:
            server.ACTIVE_HANDOFF = None
        server.ACTIVE_REGISTRATIONS.clear()
        self.httpd = None
        self.port = 0

    def tearDown(self) -> None:
        try:
            if server.RUNNER is not None:
                server.RUNNER.wait_for_idle(10.0)
            if self.httpd is not None:
                self.httpd.shutdown()
                self.httpd.server_close()
        finally:
            with server.DISPATCH_STATE_LOCK:
                server.ACTIVE_DISPATCH = None
            with server.REVIEW_STATE_LOCK:
                server.ACTIVE_REVIEW = None
            with server.ADVANCE_STATE_LOCK:
                server.ACTIVE_ADVANCE = None
            with server.HANDOFF_STATE_LOCK:
                server.ACTIVE_HANDOFF = None
            server.ACTIVE_REGISTRATIONS.clear()
            with server.TASK_LIFECYCLE_LOCKS_GUARD:
                server.TASK_LIFECYCLE_LOCKS.clear()
            server.TASK_LIFECYCLE_STATE.depth = 0
            for name, value in self._saved.items():
                setattr(server, name, value)
            for key, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            self._temp.cleanup()

    # ------------------------------------------------------------------
    # 夹具
    # ------------------------------------------------------------------

    def record_evidence(self, name: str, text: str) -> None:
        if not self.evidence_dir:
            return
        directory = Path(self.evidence_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text, encoding="utf-8")

    def make_task(
        self,
        task_id: str,
        *,
        statuses: list | None = None,
        agent_role: str = "designer",
        review: str = "",
        review_done: bool = False,
        owner_role: str = "designer",
        revise_round: int = 0,
        old_done: bool = False,
        registration_status: str = "registered",
    ) -> Path:
        project = self.projects / f"{task_id}_T23演示"
        docking = project / "对接"
        docking.mkdir(parents=True)
        (docking / "任务单.md").write_text(
            f"# {task_id} — T23 演示任务\n\n"
            "## 目标\n\n验证 T23 修复。\n\n"
            "## 范围\n\n只改测试临时目录。\n\n"
            "## 验收标准\n\n回归通过、证据落盘。\n\n"
            "## 禁止事项\n\n"
            "- BASELINE-LOCK：禁止修改基线文件\n"
            "- 禁止删除已有产物\n\n"
            "## 子任务清单\n\n- 见 subtasks.yaml\n",
            encoding="utf-8",
        )
        meta = (
            f"id: {task_id}\n"
            f"title: T23 演示任务 {task_id}\n"
            f"workdir: {project / '工作区'}\n"
            "created_at: 2026-10-26T18:00:00+08:00\n"
        )
        if registration_status:
            meta += f"status: {registration_status}\n"
        meta += f"owner_role: {owner_role}\n"
        meta += f"revise_round: {revise_round}\n"
        (docking / "task.yaml").write_text(meta, encoding="utf-8")
        if registration_status:
            (docking / "登记完成.md").write_text(
                f"# 登记完成\n\n- 任务编号：{task_id}\n", encoding="utf-8"
            )
        if statuses is not None:
            lines = ["subtasks:"]
            for index, status in enumerate(statuses, start=1):
                lines.extend(
                    [
                        f"  - id: {task_id}-{index}",
                        f"    title: 子任务 {index}",
                        f"    goal: 完成第 {index} 件事",
                        "    acceptance: DONE.md + 产物",
                        f"    status: {status}",
                        f"    agent_role: {agent_role}",
                        "    attempt: 1",
                    ]
                )
            (docking / "subtasks.yaml").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
        if review:
            (docking / "审查.md").write_text(review, encoding="utf-8")
            if review_done:
                (docking / "审核完成.md").write_text(
                    "审核完成\n", encoding="utf-8"
                )
            age_legacy_review(project)
        if old_done:
            (docking / "交付" / "r1").mkdir(parents=True, exist_ok=True)
            (docking / "交付" / "r1" / "DONE.md").write_text(
                "# DONE 旧交付\n\n## 做了什么\n\n- 旧轮次\n\n"
                "## 怎么验\n\n- 旧证据\n\n## 结果\n\n- 通过\n",
                encoding="utf-8",
            )
        return project

    def write_subtask_output(
        self,
        project: Path,
        task_id: str,
        *,
        attempt: int | None = 1,
        run_id: str = "",
    ) -> Path:
        outdir = project / "工作区" / "子任务" / f"{task_id}-1"
        outdir.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# DONE {task_id}-1",
            "",
            "## 做了什么",
            "",
            "- 预置交付",
            "",
            "## 怎么验",
            "",
            "- 检查产物",
            "",
            "## 结果",
            "",
            "- 通过",
        ]
        if attempt is not None:
            lines.append(f"attempt: {attempt}")
        if run_id:
            lines.append(f"run_id: {run_id}")
        (outdir / "DONE.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        (outdir / "artifact.txt").write_text("artifact\n", encoding="utf-8")
        return outdir

    def review_text(self, task_id: str, *targets: str) -> str:
        target = targets[0] if targets else f"{task_id}-1"
        return (
            "# 审查\n\n## 结论\n\n结论：**须修正**\n\n"
            "## 逐项证据\n\n- 预置\n\n"
            "## 问题清单\n\n"
            "| 编号 | 级别 | 问题 | 建议修正 |\n"
            "| --- | --- | --- | --- |\n"
            f"| P1 | 阻塞 | 旧证据不可采信 | 重做 {target} |\n"
        )

    def find_task(self, task_id: str):
        task = server._find_task(task_id)
        self.assertIsNotNone(task, f"任务 {task_id} 应该可读")
        return task

    def start_server(self) -> None:
        self.httpd = server.ConsoleServer(
            ("127.0.0.1", 0), server.ConsoleHandler
        )
        self.port = self.httpd.server_address[1]
        server.PORT = self.port
        thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True
        )
        thread.start()

    def request(self, method: str, path: str, *, body: bytes = b""):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=15
        )
        headers = {"Accept": "application/json"}
        if body:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read().decode("utf-8", errors="replace")
        status = response.status
        connection.close()
        return status, data

    def wait_advance(self, task_id: str, timeout: float = 20.0) -> dict:
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
        self.fail(f"{task_id} 推进没有在 {timeout} 秒内结束")

    def wait_dispatch(self, task_id: str, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = server._dispatch_state_snapshot(task_id)
            if state and str(state.get("state") or "") in {
                "completed",
                "failed",
            }:
                if not server._active_dispatch_task():
                    return state
            time.sleep(0.03)
        self.fail(f"{task_id} 分发没有在 {timeout} 秒内结束")


# ----------------------------------------------------------------------
# F06 证据新鲜度
# ----------------------------------------------------------------------


class F06EvidenceTest(T23Base):
    def test_f06_old_done_without_dispatch_record_fails_g2(self) -> None:
        project = self.make_task("T90", statuses=["delivered"])
        self.write_subtask_output(project, "T90", attempt=None)
        task = self.find_task("T90")
        checks = server._gate_g2(task)
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0]["passed"], checks[0])
        self.assertTrue(
            any("本次分发记录" in item for item in checks[0]["missing"]),
            checks[0],
        )
        self.record_evidence(
            "01-f06-old-done-g2.json",
            json.dumps(checks[0], ensure_ascii=False, indent=2) + "\n",
        )

    def test_f06_new_done_with_matching_attempt_or_run_id_passes(self) -> None:
        project = self.make_task("T90", statuses=["delivered"])
        self.write_subtask_output(
            project, "T90", attempt=2, run_id="run-T90-2"
        )
        server._update_subtask_record(
            task_path := project / "对接" / "subtasks.yaml",
            "T90-1",
            {"attempt": "2", "run_id": "run-T90-2"},
        )
        task = self.find_task("T90")
        check = server._gate_g2(task)[0]
        self.assertTrue(check["passed"], check)
        run_id_check = check
        self.write_subtask_output(
            project, "T90", attempt=None, run_id="run-T90-2"
        )
        task = self.find_task("T90")
        check = server._gate_g2(task)[0]
        self.assertTrue(check["passed"], check)
        self.record_evidence(
            "02-f06-matching-done-g2.json",
            json.dumps(
                {
                    "attempt_and_run_id": run_id_check,
                    "run_id_only": check,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

        self.write_subtask_output(
            project, "T90", attempt=3, run_id="run-T90-2"
        )
        task = self.find_task("T90")
        check = server._gate_g2(task)[0]
        self.assertFalse(check["passed"], check)
        self.assertTrue(
            any("attempt=3" in item for item in check["missing"]),
            check,
        )

        self.write_subtask_output(
            project, "T90", attempt=2, run_id="run-stale"
        )
        task = self.find_task("T90")
        check = server._gate_g2(task)[0]
        self.assertFalse(check["passed"], check)
        self.assertTrue(
            any("run_id=run-stale" in item for item in check["missing"]),
            check,
        )
        self.assertTrue(task_path.is_file())

    def test_f06_revise_moves_old_output_into_workspace_history(self) -> None:
        project = self.make_task("T90", statuses=["delivered"])
        old_outdir = self.write_subtask_output(
            project, "T90", attempt=1, run_id="run-old"
        )
        task = self.find_task("T90")
        moved = server._archive_subtask_outputs(task, ["T90-1"], 1)
        self.assertEqual(len(moved), 1)
        self.assertTrue(moved[0]["moved"], moved)
        self.assertFalse(old_outdir.exists())
        destination = Path(moved[0]["to"])
        self.assertTrue(
            destination.as_posix().startswith(
                (project / "工作区" / "历史" / "子任务" / "T90-1").as_posix()
            ),
            destination,
        )
        self.assertTrue((destination / "DONE.md").is_file())
        self.assertTrue((destination / "artifact.txt").is_file())
        self.assertIn("run_id: run-old", (destination / "DONE.md").read_text(encoding="utf-8"))
        refreshed = self.find_task("T90")
        check = server._gate_g2(refreshed)[0]
        self.assertFalse(check["passed"], check)
        self.record_evidence(
            "03-f06-history-move.json",
            json.dumps(
                {
                    "moved": moved,
                    "old_output_exists": old_outdir.exists(),
                    "history_done": str(destination / "DONE.md"),
                    "g2_after_move": check,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def test_f06_full_revise_redispatch_writes_new_attempt_done(self) -> None:
        project = self.make_task(
            "T90",
            statuses=["delivered"],
            review=self.review_text("T90"),
            review_done=True,
        )
        self.write_subtask_output(
            project, "T90", attempt=1, run_id="run-old"
        )
        task = self.find_task("T90")
        server._start_revise(task)
        final = self.wait_advance("T90")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)
        history_root = project / "工作区" / "历史" / "子任务" / "T90-1"
        histories = sorted(history_root.glob("round-1-*"))
        self.assertEqual(len(histories), 1, histories)
        self.assertTrue((histories[0] / "DONE.md").is_file())
        new_done = (
            project / "工作区" / "子任务" / "T90-1" / "DONE.md"
        ).read_text(encoding="utf-8")
        self.assertIn("attempt: 2", new_done)
        self.assertIn("run_id: run-old", (histories[0] / "DONE.md").read_text(encoding="utf-8"))
        self.record_evidence(
            "04-f06-revise-round.json",
            json.dumps(
                {
                    "advance": final,
                    "history": [str(item) for item in histories],
                    "new_done": new_done,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )


# ----------------------------------------------------------------------
# F07 角色必填
# ----------------------------------------------------------------------


class F07RoleRequiredTest(T23Base):
    def _project_for_registration(self, task_id: str) -> tuple[Path, str, float]:
        project = self.projects / f"{task_id}_登记校验"
        docking = project / "对接"
        docking.mkdir(parents=True)
        idea = "登记校验 idea"
        (docking / "idea.md").write_text(idea, encoding="utf-8")
        (docking / "任务单.md").write_text(
            f"# {task_id} — 登记校验\n\n## 目标\n\n校验。\n",
            encoding="utf-8",
        )
        (docking / "登记完成.md").write_text("登记完成\n", encoding="utf-8")
        started = time.time() - 5
        return project, hashlib.sha256(idea.encode("utf-8")).hexdigest(), started

    def _run_validation(
        self, project: Path, idea_hash: str, started: float
    ) -> tuple[str, object]:
        error, _title, _subtasks = server._registration_result_error(
            {"state": "success", "started_epoch": started},
            project_dir=project,
            idea_hash=idea_hash,
            start_epoch=started,
        )
        return error, _subtasks

    def test_f07_missing_or_unknown_role_fails_legal_role_passes(self) -> None:
        project, idea_hash, started = self._project_for_registration("T96")
        docking = project / "对接"
        subtasks_path = docking / "subtasks.yaml"
        base = (
            "subtasks:\n"
            "  - id: T96-1\n"
            "    title: 一\n"
            "    status: pending\n"
            "  - id: T96-2\n"
            "    title: 二\n"
            "    status: pending\n"
        )
        subtasks_path.write_text(base, encoding="utf-8")
        (docking / "登记完成.md").touch()
        error, _ = self._run_validation(project, idea_hash, started)
        self.assertIn("缺少 agent_role", error)

        subtasks_path.write_text(
            base.replace(
                "    title: 一\n",
                "    title: 一\n    agent_role: ghost\n",
            ).replace(
                "    title: 二\n",
                "    title: 二\n    agent_role: designer\n",
            ),
            encoding="utf-8",
        )
        (docking / "登记完成.md").touch()
        error, _ = self._run_validation(project, idea_hash, started)
        self.assertIn("agent_role=ghost 不存在", error)

        subtasks_path.write_text(
            base.replace(
                "    title: 一\n",
                "    title: 一\n    agent_role: designer\n",
            ).replace(
                "    title: 二\n",
                "    title: 二\n    agent_role: reviewer\n",
            ),
            encoding="utf-8",
        )
        (docking / "登记完成.md").touch()
        error, subtasks = self._run_validation(project, idea_hash, started)
        self.assertEqual(error, "")
        self.assertEqual(len(subtasks), 2)
        prompt = server._registration_prompt(
            task_id="T96",
            project_name=project.name,
            project_dir=project,
            workdir=project / "工作区",
            title_hint="",
        )
        self.assertIn("每个子任务必须写 agent_role", prompt)
        self.assertIn("缺角色或未知角色会让整次登记失败", prompt)
        self.assertNotIn("可省略", prompt)
        self.assertIn("owner_role: designer", prompt)
        self.record_evidence(
            "05-f07-registration-roles.json",
            json.dumps(
                {
                    "missing_error": "缺少 agent_role",
                    "unknown_error": "agent_role=ghost 不存在",
                    "legal_error": error,
                    "prompt_requires_role": "每个子任务必须写 agent_role" in prompt,
                    "prompt_omission_removed": "可省略" not in prompt,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def test_f07_finalize_registration_marks_register_failed(self) -> None:
        project, idea_hash, started = self._project_for_registration("T96")
        docking = project / "对接"
        (docking / "subtasks.yaml").write_text(
            "subtasks:\n"
            "  - id: T96-1\n"
            "    title: 一\n"
            "    status: pending\n"
            "  - id: T96-2\n"
            "    title: 二\n"
            "    status: pending\n",
            encoding="utf-8",
        )
        (docking / "登记完成.md").touch()
        server._finalize_registration(
            task_id="T96",
            project_dir=project,
            workdir=project / "工作区",
            idea_hash=idea_hash,
            title_hint="",
            controller="demo-t23",
            result={
                "state": "success",
                "started_epoch": started,
                "run_id": "run-f07",
                "log": "",
            },
        )
        meta = server._read_task_meta(project, "T96")
        self.assertEqual(meta.get("status"), server.REGISTER_FAILED)
        self.assertIn("缺少 agent_role", str(meta.get("register_error") or ""))
        self.record_evidence(
            "05b-f07-register-failed.json",
            json.dumps(
                {
                    "status": meta.get("status"),
                    "register_error": meta.get("register_error"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )


# ----------------------------------------------------------------------
# F08 子任务 ID 边界
# ----------------------------------------------------------------------


class F08SubtaskBoundaryTest(T23Base):
    def test_f08_invalid_ids_and_prefixes_are_rejected(self) -> None:
        cases = {
            "traversal": "subtasks:\n  - id: ../../etc\n    title: x\n",
            "absolute": "subtasks:\n  - id: /tmp/x\n    title: x\n",
            "dotdot": "subtasks:\n  - id: T23-1/../..\n    title: x\n",
            "duplicate_casefold": (
                "subtasks:\n"
                "  - id: T23-1\n    title: x\n"
                "  - id: t23-1\n    title: y\n"
            ),
            "wrong_prefix": "subtasks:\n  - id: T24-1\n    title: x\n",
        }
        results = {}
        for name, text in cases.items():
            with self.assertRaises(server.ConfigError) as caught:
                server._parse_subtasks_text(text, "T23")
            results[name] = str(caught.exception)
        valid = server._parse_subtasks_text(
            "subtasks:\n  - id: t23-1\n    title: lower\n", "T23"
        )
        self.assertEqual(valid[0].id, "t23-1")
        self.record_evidence(
            "06-f08-id-boundaries.json",
            json.dumps(
                {"rejected": results, "valid_lowercase": valid[0].id},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def test_f08_load_subtasks_enforces_task_prefix(self) -> None:
        project = self.projects / "T23_路径边界"
        docking = project / "对接"
        docking.mkdir(parents=True)
        (docking / "subtasks.yaml").write_text(
            "subtasks:\n  - id: T24-1\n    title: wrong\n",
            encoding="utf-8",
        )
        subtasks, error = server._load_subtasks(docking / "subtasks.yaml")
        self.assertEqual(subtasks, ())
        self.assertIn("前缀必须是任务编号 T23", error)

    def test_f08_output_dir_traversal_and_symlink_escape_rejected(self) -> None:
        project = self.make_task("T97", statuses=["pending"])
        outdir = project / "工作区" / "子任务"
        outdir.mkdir(parents=True)
        task = self.find_task("T97")
        valid = task.subtasks[0]
        self.assertEqual(
            server._subtask_output_dir(task, valid),
            (project / "工作区" / "子任务" / "T97-1").resolve(),
        )
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(outside, outdir / "T97-1", target_is_directory=True)
        with self.assertRaises(server.ConfigError) as caught:
            server._subtask_output_dir(task, valid)
        self.assertIn("越界", str(caught.exception))
        evil = dataclasses.replace(
            valid, id="../../etc", status="delivered"
        )
        with self.assertRaises(server.ConfigError):
            server._subtask_output_dir(task, evil)
        check = server._gate_g2(
            dataclasses.replace(task, subtasks=(evil,))
        )
        self.assertFalse(check[0]["passed"], check)
        self.record_evidence(
            "07-f08-output-dir.json",
            json.dumps(
                {
                    "symlink_error": str(caught.exception),
                    "gate": check,
                    "allowed_root": str(project / "工作区"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )


# ----------------------------------------------------------------------
# F09 生命周期互斥
# ----------------------------------------------------------------------


class F09LifecycleTest(T23Base):
    def test_f09_archive_refused_while_dispatch_running(self) -> None:
        project = self.make_task("T91", statuses=["pending"])
        task = self.find_task("T91")
        hold = self.root / "dispatch-hold"
        os.environ["T23_AGENT_HOLD_FILE"] = str(hold)
        server._start_dispatch(task)
        try:
            deadline = time.time() + 8
            while time.time() < deadline and not Path(
                str(hold) + ".started"
            ).exists():
                time.sleep(0.02)
            self.assertTrue(Path(str(hold) + ".started").exists())
            with self.assertRaises(server.ActionError) as caught:
                server._archive_project("T91")
        finally:
            Path(str(hold) + ".release").touch()
            os.environ.pop("T23_AGENT_HOLD_FILE", None)
        self.assertEqual(caught.exception.status, 409)
        self.assertTrue(project.is_dir())
        self.assertFalse((self.archive / project.name).exists())
        final = self.wait_dispatch("T91")
        self.assertEqual(final["state"], "completed", final)
        self.record_evidence(
            "08-f09-archive-vs-dispatch.json",
            json.dumps(
                {
                    "archive_status": caught.exception.status,
                    "archive_title": caught.exception.title,
                    "project_still_exists": project.is_dir(),
                    "dispatch_final": final,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def test_f09_idle_archive_and_restore_still_work(self) -> None:
        project = self.make_task("T99", statuses=["pending"])
        archived = server._archive_project("T99")
        self.assertTrue(archived.is_dir())
        self.assertFalse(project.exists())
        self.assertEqual(archived.name, project.name)
        restored = server._restore_project("T99")
        self.assertTrue(restored.is_dir())
        self.assertFalse(archived.exists())
        self.assertTrue((restored / "对接" / "task.yaml").is_file())
        task = self.find_task("T99")
        self.assertEqual(task.project_name, project.name)
        self.record_evidence(
            "08c-f09-idle-archive-restore.json",
            json.dumps(
                {
                    "archived": str(archived),
                    "restored": str(restored),
                    "project_name": task.project_name,
                    "healthy": restored.is_dir(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def test_f09_archive_checks_active_dispatch_record(self) -> None:
        project = self.make_task("T91", statuses=["pending"])
        with server.DISPATCH_STATE_LOCK:
            server.ACTIVE_DISPATCH = {
                "task": "T91",
                "state": {"task": "T91", "state": "running"},
            }
        try:
            with mock.patch.object(
                server.RUNNER, "snapshot", return_value={}
            ), mock.patch.object(
                server.RUNNER, "lock_info", return_value={}
            ):
                with self.assertRaises(server.ActionError) as caught:
                    server._archive_project("T91")
        finally:
            with server.DISPATCH_STATE_LOCK:
                server.ACTIVE_DISPATCH = None
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("分发", caught.exception.title)
        self.assertTrue(project.is_dir())
        self.assertFalse((self.archive / project.name).exists())
        self.record_evidence(
            "08b-f09-active-dispatch-record.json",
            json.dumps(
                {
                    "archive_status": caught.exception.status,
                    "archive_title": caught.exception.title,
                    "project_still_exists": project.is_dir(),
                    "runner_reported_idle": True,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def test_f09_dispatch_refused_while_archive_in_progress(self) -> None:
        project = self.make_task("T92", statuses=["pending"])
        task = self.find_task("T92")
        started = threading.Event()
        release = threading.Event()
        real_move = server.shutil.move

        def blocking_move(src, dst, *args, **kwargs):
            if str(src) == str(project):
                started.set()
                release.wait(8)
            return real_move(src, dst, *args, **kwargs)

        archive_errors: list = []

        def run_archive() -> None:
            try:
                server._archive_project("T92")
            except Exception as exc:  # pragma: no cover - 失败时给断言看
                archive_errors.append(exc)

        with mock.patch.object(server.shutil, "move", side_effect=blocking_move):
            archive_thread = threading.Thread(target=run_archive)
            archive_thread.start()
            self.assertTrue(started.wait(5), "归档没有进入 move 临界区")
            dispatch_errors: list = []

            def run_dispatch() -> None:
                try:
                    server._start_dispatch(task)
                except Exception as exc:
                    dispatch_errors.append(exc)

            dispatch_thread = threading.Thread(target=run_dispatch)
            dispatch_thread.start()
            time.sleep(0.2)
            self.assertTrue(
                dispatch_thread.is_alive(), "分发应在生命周期锁上等待归档"
            )
            release.set()
            archive_thread.join(10)
            dispatch_thread.join(10)
        self.assertFalse(archive_errors, archive_errors)
        self.assertTrue(dispatch_errors, "归档完成后启动分发必须被拒绝")
        self.assertIsInstance(dispatch_errors[0], server.ActionError)
        self.assertEqual(dispatch_errors[0].status, 409)
        self.assertFalse(server._active_dispatch_task())
        self.assertTrue((self.archive / project.name).is_dir())
        self.assertFalse(project.exists())
        self.record_evidence(
            "09-f09-dispatch-vs-archive.json",
            json.dumps(
                {
                    "archive_errors": [str(item) for item in archive_errors],
                    "dispatch_error": str(dispatch_errors[0]),
                    "dispatch_status": dispatch_errors[0].status,
                    "dispatch_state": server._dispatch_state_snapshot("T92"),
                    "archive_target": str(self.archive / project.name),
                    "project_moved": not project.exists(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )


# ----------------------------------------------------------------------
# 无子任务整单重修
# ----------------------------------------------------------------------


class WholeReworkTest(T23Base):
    def test_whole_rework_no_subtasks_starts_and_reviews(self) -> None:
        project = self.make_task(
            "T93",
            statuses=None,
            review=self.review_text("T93", "T93-1"),
            review_done=True,
            owner_role="designer",
            old_done=True,
            registration_status="",
        )
        self.start_server()
        status, body = self.request("GET", "/api/task/T93/revise-status")
        self.assertEqual(status, 200, body)
        before = json.loads(body)
        self.assertTrue(before["whole_rework"], before)
        self.assertFalse(before["needs_choice"], before)
        self.assertEqual(before["owner_role"], "designer")
        status, page = self.request("GET", "/task/T93")
        self.assertEqual(status, 200)
        self.assertIn("整单重修（第 1 轮）", page)

        status, body = self.request("POST", "/revise/T93")
        self.assertEqual(status, 202, body)
        payload = json.loads(body)
        self.assertTrue(payload["whole_rework"], payload)
        self.assertEqual(payload["owner_role"], "designer")
        self.assertEqual(payload["revise_round"], 1)
        self.assertTrue(payload["archived_review"].startswith("项目/T93_"))
        self.assertFalse((project / "对接" / "审查.md").exists())
        history = project / "对接" / "审查历史" / "round-1.md"
        self.assertTrue(history.is_file())
        self.assertIn("须修正", history.read_text(encoding="utf-8"))

        final = self.wait_advance("T93")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)
        self.assertTrue(final.get("whole_ran"), final)
        self.assertIn("整单重修", str(final.get("message") or ""))
        self.assertEqual(
            server._task_phase(self.find_task("T93")), server.PHASE_PASSED
        )
        done_files = sorted(
            (project / "对接" / "交付").glob("r*/DONE.md")
        )
        self.assertTrue(done_files, "整单重修必须写新的 DONE.md")
        newest = max(done_files, key=lambda path: path.stat().st_mtime)
        new_done = newest.read_text(encoding="utf-8")
        self.assertIn("## 做了什么", new_done)
        self.assertIn("整单重修", new_done)
        prompts = sorted(self.prompt_dir.glob("whole-*.txt"))
        self.assertTrue(prompts, "owner_role Agent 应收到整单重修提示词")
        self.assertTrue(
            list(self.prompt_dir.glob("review-*.txt")),
            "整单重修完成后必须自动重新审核",
        )
        prompt = prompts[-1].read_text(encoding="utf-8")
        for expected in (
            "整单重修",
            "BASELINE-LOCK",
            "禁止删除已有产物",
            "旧证据不可采信",
            "回归验收要求",
            "第 1/3 轮",
            str(project / "对接" / "任务单.md"),
            "owner_role",
        ):
            self.assertIn(expected, prompt)
        self.record_evidence(
            "10-whole-rework.json",
            json.dumps(
                {
                    "before": before,
                    "start": payload,
                    "final": final,
                    "history": str(history),
                    "done_files": [str(item) for item in done_files],
                    "phase": server._task_phase(self.find_task("T93")),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        self.record_evidence("11-whole-rework-prompt.txt", prompt + "\n")

    def test_whole_rework_default_owner_role_and_limit(self) -> None:
        project = self.make_task(
            "T94",
            statuses=None,
            review=self.review_text("T94", "T94-1"),
            review_done=True,
            revise_round=3,
            registration_status="",
        )
        meta_path = project / "对接" / "task.yaml"
        meta = meta_path.read_text(encoding="utf-8")
        meta = meta.replace("owner_role: designer\n", "")
        meta_path.write_text(meta, encoding="utf-8")
        task = self.find_task("T94")
        self.assertEqual(task.owner_role, "designer")
        with self.assertRaises(server.ActionError) as caught:
            server._start_revise(task)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("上限", caught.exception.message)
        self.assertFalse((project / "对接" / "审查历史").exists())
        self.assertFalse(server.RUNNER.lock_path.exists())

    def test_owner_role_is_editable_and_validated(self) -> None:
        project = self.make_task(
            "T98",
            statuses=None,
            review=self.review_text("T98", "T98-1"),
            review_done=True,
            registration_status="",
        )
        self.start_server()
        body = urlencode({"owner_role": "reviewer"}).encode("utf-8")
        status, payload = self.request(
            "POST", "/task/T98/owner-role", body=body
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(json.loads(payload)["owner_role"], "reviewer")
        meta = server._read_task_meta(project, "T98")
        self.assertEqual(meta.get("owner_role"), "reviewer")

        body = urlencode({"owner_role": "ghost"}).encode("utf-8")
        status, payload = self.request(
            "POST", "/task/T98/owner-role", body=body
        )
        self.assertEqual(status, 400, payload)
        meta = server._read_task_meta(project, "T98")
        self.assertEqual(meta.get("owner_role"), "reviewer")
        self.record_evidence(
            "10b-owner-role-edit.json",
            json.dumps(
                {
                    "saved": "reviewer",
                    "invalid_status": status,
                    "meta_after_invalid": meta.get("owner_role"),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    def test_whole_rework_unknown_owner_role_is_rejected(self) -> None:
        project = self.make_task(
            "T95",
            statuses=None,
            review=self.review_text("T95", "T95-1"),
            review_done=True,
            owner_role="ghost",
            registration_status="",
        )
        task = self.find_task("T95")
        available, reason = server._revise_availability(task)
        self.assertFalse(available)
        self.assertIn("owner_role=ghost", reason)
        with self.assertRaises(server.ActionError) as caught:
            server._start_revise(task)
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse((project / "对接" / "审查历史").exists())


if __name__ == "__main__":
    unittest.main()
