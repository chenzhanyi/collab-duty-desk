#!/usr/bin/env python3
"""T19 外部审计 P1 回归：F01–F05。

所有测试都在临时仓库/临时外部目录运行，使用假 Agent，不触碰真实
项目/、协作/ 或 8787。
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

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
prompt_dir = os.environ.get("T19_PROMPT_DIR", "")


def record_prompt(kind: str) -> None:
    if not prompt_dir:
        return
    directory = Path(prompt_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("%s-%d-%d.txt" % (kind, time.time_ns(), os.getpid()))
    target.write_text(prompt, encoding="utf-8")


if "你是审核角色" in prompt:
    record_prompt("review")
    review_match = re.search(r"绝对路径：(\S+?/审查\.md)", prompt)
    done_match = re.search(r"绝对路径：(\S+?/审核完成\.md)", prompt)
    if not review_match or not done_match:
        print("MISSING REVIEW PATHS")
        raise SystemExit(4)
    verdict = os.environ.get("T19_REVIEW_VERDICT", "通过")
    review_path = Path(review_match.group(1))
    done_path = Path(done_match.group(1))
    attribution = re.search(
        r"^- 审核角色：[^\n]*\n- 审查时间：[^\n]*", prompt, re.M
    )
    header = (attribution.group(0) + "\n\n") if attribution else ""
    review_path.write_text(
        header
        + "# 审查\n\n## 结论\n\n结论：**%s**\n\n"
        "## 逐项证据\n\n- T19 回归假审核\n\n"
        "## 问题清单\n\n- 无\n" % verdict,
        encoding="utf-8",
    )
    if os.environ.get("T19_REVIEW_NO_DONE") != "1":
        done_path.write_text("审核完成\n", encoding="utf-8")
    print("fake review " + verdict)
    raise SystemExit(0)

if "交接检查未通过" in prompt:
    record_prompt("handoff")
    print("UNEXPECTED HANDOFF")
    raise SystemExit(2)

record_prompt("dispatch")
match = re.search(r"执行子任务 (T\d+-\d+)：", prompt)
subtask = match.group(1) if match else "unknown"
attempt_match = re.search(r"attempt:\s*(\d+)", prompt)
attempt_line = ("attempt: %s\n" % attempt_match.group(1)) if attempt_match else ""
out_match = re.search(r"输出目录（绝对路径）：([^\n]+)", prompt)
if not out_match:
    print("MISSING OUTPUT DIR")
    raise SystemExit(5)
outdir = Path(out_match.group(1).strip())
outdir.mkdir(parents=True, exist_ok=True)
(outdir / "DONE.md").write_text(
    "# DONE %s\n\n"
    "## 做了什么\n\n- T19 回归假 Agent\n\n"
    "## 怎么验\n\n- 按提示词绝对路径写盘\n\n"
    "## 结果\n\n- 通过\n"
    "%s" % (subtask, attempt_line),
    encoding="utf-8",
)
(outdir / "result.txt").write_text(
    "subtask=%s\noutput=%s\n" % (subtask, outdir), encoding="utf-8"
)
if os.environ.get("T19_DISPATCH_FAIL") == "1":
    print("fake dispatch failure")
    raise SystemExit(2)
print("fake dispatch " + subtask)
'''


def subtasks_text(
    task_id: str,
    statuses: list,
    *,
    log: str = "",
    run_id: str = "",
) -> str:
    lines = ["subtasks:"]
    for index, status in enumerate(statuses, start=1):
        lines.extend(
            [
                f"  - id: {task_id}-{index}",
                f"    title: T19 子任务 {index}",
                f"    goal: 验证审计修复 {index}",
                "    acceptance: DONE.md + 至少一个产物",
                f"    status: {status}",
                "    agent_role: demo-builder",
            ]
        )
        if log:
            lines.append(f"    log: {log}")
        if run_id:
            lines.append(f"    run_id: {run_id}")
    return "\n".join(lines) + "\n"


REVIEW_TEMPLATE = (
    "# 审查\n\n## 结论\n\n结论：**须修正**\n\n"
    "## 逐项证据\n\n- 已核对\n\n"
    "## 问题清单\n\n"
    "| 编号 | 级别 | 问题 | 建议修正 |\n"
    "| --- | --- | --- | --- |\n"
    "| P1 | 阻塞 | 密钥硬编码 FIX-SECRET-42 | 重做 {task_id}-1 并移除硬编码 |\n"
)


class AuditP1Test(unittest.TestCase):
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

        self.external_temp = tempfile.TemporaryDirectory()
        self.external = Path(self.external_temp.name).resolve()

        self.script = self.root / "fake-t19-agent.py"
        self.script.write_text(FAKE_AGENT, encoding="utf-8")
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)
        for agent_id, agent_name in (
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
        self.write_role("demo-builder", "构建", "fake-builder")
        self.write_role("reviewer", "审核", "fake-reviewer")

        self.prompt_dir = self.root / "prompts"
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "T19_PROMPT_DIR",
                "T19_REVIEW_VERDICT",
                "T19_REVIEW_NO_DONE",
                "T19_DISPATCH_FAIL",
            )
        }
        os.environ["T19_PROMPT_DIR"] = str(self.prompt_dir)
        os.environ.pop("T19_REVIEW_VERDICT", None)
        os.environ.pop("T19_REVIEW_NO_DONE", None)
        os.environ.pop("T19_DISPATCH_FAIL", None)

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
        server.PORT = 18819
        with server.ADVANCE_STATE_LOCK:
            server.ACTIVE_ADVANCE = None
        with server.DISPATCH_STATE_LOCK:
            server.ACTIVE_DISPATCH = None
        with server.REVIEW_STATE_LOCK:
            server.ACTIVE_REVIEW = None
        with server.HANDOFF_STATE_LOCK:
            server.ACTIVE_HANDOFF = None
        server.ACTIVE_REGISTRATIONS.clear()

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
            self.external_temp.cleanup()
            self._temp.cleanup()

    # ------------------------------------------------------------------
    # 夹具
    # ------------------------------------------------------------------

    def record_evidence(self, name: str, text: str) -> None:
        target = os.environ.get("T19_EVIDENCE_DIR", "").strip()
        if not target:
            return
        directory = Path(target)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text, encoding="utf-8")

    def write_role(self, role_id: str, name: str, agent_id: str) -> None:
        (self.roles / f"{role_id}.yaml").write_text(
            f"id: {role_id}\n"
            f"name: {name}\n"
            f"description: T19 回归 {role_id}\n"
            f"agent_id: {agent_id}\n"
            "write_scope: []\n"
            "enabled: true\n"
            'notes: ""\n',
            encoding="utf-8",
        )

    def make_project(
        self,
        task_id: str,
        statuses: list,
        *,
        workdir: Path = None,
        review: str = "",
        review_done: bool = False,
        meta_extra: str = "",
        task_sheet_extra: str = "",
        deliver_files: bool = True,
        running_log: str = "",
        running_run_id: str = "",
    ) -> Path:
        project = self.projects / f"{task_id}_T19回归"
        docking = project / "对接"
        docking.mkdir(parents=True)
        (docking / "任务单.md").write_text(
            f"# {task_id} — T19 回归\n\n"
            "## 验收标准\n\n- 子任务交付\n"
            + task_sheet_extra,
            encoding="utf-8",
        )
        if workdir is None:
            workdir = project / "工作区"
        (docking / "subtasks.yaml").write_text(
            subtasks_text(
                task_id,
                statuses,
                log=running_log,
                run_id=running_run_id,
            ),
            encoding="utf-8",
        )
        (docking / "task.yaml").write_text(
            f"id: {task_id}\n"
            f"title: T19 回归 {task_id}\n"
            f"workdir: {workdir}\n"
            "created_at: 2026-10-26T09:00:00+08:00\n"
            "status: registered\n"
            + meta_extra,
            encoding="utf-8",
        )
        (docking / "登记完成.md").write_text(
            f"# 登记完成\n\n- 任务编号：{task_id}\n", encoding="utf-8"
        )
        if deliver_files:
            for index, status in enumerate(statuses, start=1):
                if status not in {"delivered", "passed"}:
                    continue
                outdir = Path(workdir) / "子任务" / f"{task_id}-{index}"
                outdir.mkdir(parents=True, exist_ok=True)
                (outdir / "DONE.md").write_text(
                    "# DONE %s\n\n## 做了什么\n\n- 预置\n\n"
                    "## 怎么验\n\n- 核对\n\n## 结果\n\n- 通过\n"
                    % f"{task_id}-{index}",
                    encoding="utf-8",
                )
                (outdir / "result.txt").write_text(
                    f"{task_id}-{index}\n", encoding="utf-8"
                )
        if review:
            (docking / "审查.md").write_text(review, encoding="utf-8")
        if review_done:
            (docking / "审核完成.md").write_text("审核完成\n", encoding="utf-8")
        if review or review_done:
            age_legacy_review(project)
        return project

    def find_task(self, task_id: str):
        task = server._find_task(task_id)
        self.assertIsNotNone(task, f"{task_id} 应该可读")
        return task

    def prompts(self, kind: str) -> list:
        if not self.prompt_dir.is_dir():
            return []
        return [
            path.read_text(encoding="utf-8")
            for path in sorted(self.prompt_dir.glob(f"{kind}-*.txt"))
        ]

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
    # F01 审核终态
    # ------------------------------------------------------------------

    def test_f01_review_without_done_is_not_passed_and_retry_quarantines_it(
        self,
    ) -> None:
        project = self.make_project(
            "T90",
            ["delivered"],
            review="# 审查\n\n## 结论\n\n结论：**通过**\n\n- OLD-REVIEW-MARK\n",
            review_done=False,
        )
        task = self.find_task("T90")
        self.assertFalse(server._review_terminal_state(task)[0])
        self.assertEqual(server._task_phase(task), server.PHASE_REVIEW_READY)
        self.assertEqual(task.status, server.STATUS_DELIVERED)
        available, reason = server._review_availability(task)
        self.assertTrue(available, reason)
        payload = server._review_status_payload(task)
        self.assertTrue(payload["can_start"], payload)
        self.assertEqual(payload["state"], "review_failed")
        self.assertEqual(payload["conclusion"], "")
        self.record_evidence(
            "02-f01-before-retry.json",
            json.dumps(
                {
                    "phase": server._task_phase(task),
                    "status": task.status,
                    "terminal": server._review_terminal_state(task),
                    "review_availability": [available, reason],
                    "review_status": payload,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

        server._start_review(task)
        final = self.wait_review("T90")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)
        self.assertIn("invalid-", final.get("quarantined_review", ""))
        history = list((project / "对接" / "审查历史").glob("invalid-*.md"))
        self.assertEqual(len(history), 1, history)
        self.assertIn("OLD-REVIEW-MARK", history[0].read_text(encoding="utf-8"))
        self.assertTrue((project / "对接" / "审查.md").is_file())
        self.assertTrue((project / "对接" / "审核完成.md").is_file())
        refreshed = self.find_task("T90")
        self.assertEqual(server._task_phase(refreshed), server.PHASE_PASSED)
        self.record_evidence(
            "02-f01-after-retry.json",
            json.dumps(final, ensure_ascii=False, indent=2) + "\n",
        )
        self.record_evidence(
            "02-f01-history.txt",
            "\n".join(str(path) for path in history)
            + "\n--- content ---\n"
            + history[0].read_text(encoding="utf-8"),
        )

    def test_f01_invalid_review_output_is_quarantined_by_review_worker(self) -> None:
        project = self.make_project("T89", ["delivered"])
        os.environ["T19_REVIEW_VERDICT"] = ""
        task = self.find_task("T89")
        server._start_review(task)
        final = self.wait_review("T89")
        self.assertEqual(final["state"], "review_failed", final)
        history_all = list((project / "对接" / "审查历史").glob("invalid-*.md"))
        history = [path for path in history_all if "审核完成" not in path.name]
        self.assertEqual(len(history), 1, history_all)
        self.assertTrue(
            any("审核完成" in path.name for path in history_all), history_all
        )
        self.assertIn("结论：****", history[0].read_text(encoding="utf-8"))
        self.assertFalse((project / "对接" / "审查.md").exists())
        self.assertFalse((project / "对接" / "审核完成.md").exists())
        self.record_evidence(
            "02b-f01-worker-quarantine.json",
            json.dumps(final, ensure_ascii=False, indent=2)
            + "\n--- history ---\n"
            + history[0].read_text(encoding="utf-8"),
        )

    def test_f01_empty_half_and_unrecognized_reviews_never_pass(self) -> None:
        cases = []
        empty = self.make_project(
            "T91",
            ["delivered"],
            review="",
            review_done=True,
        )
        cases.append(("T91", empty))
        half = self.make_project(
            "T92",
            ["delivered"],
            review="# 审查\n\n结论：**通过**\n",
            review_done=False,
        )
        cases.append(("T92", half))
        garbage = self.make_project(
            "T93",
            ["delivered"],
            review="随便写了一半，没有结论。\n",
            review_done=True,
        )
        cases.append(("T93", garbage))

        evidence = {}
        for task_id, _project in cases:
            task = self.find_task(task_id)
            terminal, verdict = server._review_terminal_state(task)
            self.assertFalse(terminal, task_id)
            self.assertNotEqual(server._task_phase(task), server.PHASE_PASSED)
            self.assertNotEqual(task.status, server.STATUS_PASSED)
            available, reason = server._review_availability(task)
            self.assertTrue(available, f"{task_id}: {reason}")
            self.assertEqual(server._review_status_payload(task)["conclusion"], "")
            api_item = server._tasks_api_payload([task])["tasks"][0]
            self.assertEqual(api_item["review_verdict"], "")
            evidence[task_id] = {
                "terminal": [terminal, verdict],
                "phase": server._task_phase(task),
                "status": task.status,
                "review_availability": [available, reason],
                "api_review_verdict": api_item["review_verdict"],
            }
        self.record_evidence(
            "03-f01-invalid-reviews.json",
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        )

    def _record_f01_unresolved_review(
        self, task_id: str, evidence_name: str, sample: str, review: str
    ) -> None:
        """构造带完整审核完成信号的反向样例，断言其仍未进终态并落盘证据。"""
        self.make_project(task_id, ["delivered"], review=review, review_done=True)
        task = self.find_task(task_id)
        verdict = server._review_verdict(task.review_text)
        terminal_state = server._review_terminal_state(task)
        phase = server._task_phase(task)
        self.assertEqual(verdict, "", task_id)
        self.assertEqual(terminal_state, (False, ""), task_id)
        self.assertNotEqual(phase, server.PHASE_PASSED, task_id)
        self.assertNotEqual(phase, server.PHASE_REVISE, task_id)
        self.assertNotEqual(task.status, server.STATUS_PASSED, task_id)
        available, reason = server._review_availability(task)
        self.assertTrue(available, f"{task_id}: {reason}")
        payload = server._review_status_payload(task)
        self.assertEqual(payload["conclusion"], "", task_id)
        api_item = server._tasks_api_payload([task])["tasks"][0]
        self.assertEqual(api_item["review_verdict"], "", task_id)

        evidence = {
            "sample": sample,
            "review_text": review,
            "review_verdict": verdict,
            "terminal_state": [terminal_state[0], terminal_state[1]],
            "phase": phase,
            "status": task.status,
            "review_availability": [available, reason],
            "review_status_conclusion": payload["conclusion"],
            "api_review_verdict": api_item["review_verdict"],
        }
        self.record_evidence(
            evidence_name + ".json",
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        )
        self.record_evidence(
            evidence_name + ".txt",
            "sample: %s\n"
            "review_verdict: %r\n"
            "terminal_state: [%s, %r]\n"
            "phase: %s\n"
            "status: %s\n"
            "review_availability: [%s, %r]\n"
            "review_status_conclusion: %r\n"
            "api_review_verdict: %r\n"
            "review_text:\n%s\n"
            % (
                sample,
                verdict,
                "True" if terminal_state[0] else "False",
                terminal_state[1],
                phase,
                task.status,
                "True" if available else "False",
                reason,
                payload["conclusion"],
                api_item["review_verdict"],
                review,
            ),
        )

    def test_f01_pending_conclusion_with_passing_body_is_not_terminal(self) -> None:
        review = (
            "# 审查\n\n## 结论\n\n结论：**待定**\n\n"
            "## 逐项核对\n\n- 其余项均通过。\n"
        )
        self._record_f01_unresolved_review(
            "T102",
            "11-f01-pending-with-passing-body",
            "结论：**待定** + 正文“其余项均通过”",
            review,
        )

    def test_f01_no_conclusion_with_passing_body_is_not_terminal(self) -> None:
        review = (
            "# 审查\n\n逐项核对完成，未发现阻塞；建议通过。\n"
            "其余项均通过。\n"
        )
        self._record_f01_unresolved_review(
            "T103",
            "12-f01-no-conclusion-suggest-pass",
            "无结论行 + 正文“建议通过”",
            review,
        )

    def test_f01_running_review_takes_precedence_over_terminal_files(self) -> None:
        self.make_project(
            "T94",
            ["delivered"],
            review="# 审查\n\n## 结论\n\n结论：**通过**\n",
            review_done=True,
        )
        task = self.find_task("T94")
        self.assertEqual(server._task_phase(task), server.PHASE_PASSED)

        fake_state = {"ok": True, "task": "T94", "state": "running"}
        with server.REVIEW_STATE_LOCK:
            server.ACTIVE_REVIEW = {"task": "T94", "state": fake_state}
        try:
            self.assertEqual(server._task_phase(task), server.PHASE_REVIEWING)
        finally:
            with server.REVIEW_STATE_LOCK:
                server.ACTIVE_REVIEW = None

        with server.DISPATCH_STATE_LOCK:
            server.ACTIVE_DISPATCH = {
                "task": "T94",
                "state": {"task": "T94", "state": "running"},
            }
        try:
            self.assertEqual(server._task_phase(task), server.PHASE_DISPATCHING)
        finally:
            with server.DISPATCH_STATE_LOCK:
                server.ACTIVE_DISPATCH = None
        self.record_evidence(
            "04-f01-running-priority.txt",
            "terminal=passed\nactive_review -> reviewing\n"
            "active_dispatch -> dispatching\n",
        )

    # ------------------------------------------------------------------
    # F02 归档/恢复元数据
    # ------------------------------------------------------------------

    def test_f02_archive_restore_preserves_all_meta_except_workdir(self) -> None:
        project = self.make_project(
            "T95",
            ["pending"],
            meta_extra=(
                "controller: fake-controller\n"
                "revise_round: 2\n"
                'auto_advance: "1"\n'
                "idea_sha256: abc123\n"
                "register_log: 项目/T95_T19回归/工作区/日志/register-x.log\n"
                "custom_extension: keep-me\n"
                "future_flag: true\n"
            ),
        )
        meta_path = project / "对接" / "task.yaml"
        before_text = meta_path.read_text(encoding="utf-8")
        before = server._parse_simple_yaml(before_text)
        self.assertEqual(str(before["workdir"]), str(project / "工作区"))

        archived = server._archive_project("T95")
        archived_meta_path = archived / "对接" / "task.yaml"
        archived_meta = server._parse_simple_yaml(
            archived_meta_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            archived_meta["workdir"], str(archived / "工作区")
        )
        self.assertEqual(set(archived_meta), set(before))
        for key, value in before.items():
            if key == "workdir":
                continue
            self.assertEqual(archived_meta[key], value, key)
        self.assertIn("custom_extension: keep-me", archived_meta_path.read_text(encoding="utf-8"))
        self.assertIn("future_flag: true", archived_meta_path.read_text(encoding="utf-8"))
        self.record_evidence(
            "05-f02-archived-meta.json",
            json.dumps(archived_meta, ensure_ascii=False, indent=2) + "\n",
        )

        restored = server._restore_project("T95")
        after_meta_path = restored / "对接" / "task.yaml"
        after = server._parse_simple_yaml(after_meta_path.read_text(encoding="utf-8"))
        self.assertEqual(set(after), set(before))
        for key, value in before.items():
            self.assertEqual(after[key], value, key)
        self.assertIn("custom_extension: keep-me", after_meta_path.read_text(encoding="utf-8"))
        restored_after = after
        self.record_evidence(
            "05-f02-roundtrip.json",
            json.dumps(
                {
                    "before": before,
                    "archived": archived_meta,
                    "after_restore": restored_after,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

        # 其它写回路径也不能丢未知字段：模拟写入 revise_round。
        server._write_revise_round(self.find_task("T95"), 3)
        rewritten = server._parse_simple_yaml(after_meta_path.read_text(encoding="utf-8"))
        self.assertEqual(rewritten["revise_round"], "3")
        for key in (
            "controller",
            "auto_advance",
            "idea_sha256",
            "register_log",
            "custom_extension",
            "future_flag",
        ):
            self.assertEqual(rewritten[key], before[key], key)
        self.record_evidence(
            "05-f02-meta-rewrite.json",
            json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n",
        )

    def test_f02_external_workdir_is_not_retargeted_but_fields_stay(self) -> None:
        external_workdir = self.external / "外部工作区"
        external_workdir.mkdir(parents=True, exist_ok=True)
        project = self.make_project(
            "T96",
            ["pending"],
            workdir=external_workdir,
            meta_extra="controller: fake-controller\nfuture_flag: true\n",
        )
        before = server._parse_simple_yaml(
            (project / "对接" / "task.yaml").read_text(encoding="utf-8")
        )
        archived = server._archive_project("T96")
        archived_meta = server._parse_simple_yaml(
            (archived / "对接" / "task.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(archived_meta["workdir"], str(external_workdir))
        self.assertEqual(archived_meta.get("controller"), "fake-controller")
        self.assertTrue(archived_meta.get("future_flag"))
        restored = server._restore_project("T96")
        after = server._parse_simple_yaml(
            (restored / "对接" / "task.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(after, before)
        self.record_evidence(
            "05-f02-external-workdir.json",
            json.dumps(
                {"before": before, "archived": archived_meta, "after": after},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    # ------------------------------------------------------------------
    # F03 崩溃恢复
    # ------------------------------------------------------------------

    def test_f03_reconcile_marks_running_interrupted_and_continue_dispatch_runs(
        self,
    ) -> None:
        project = self.make_project(
            "T97",
            ["running"],
            deliver_files=False,
            running_log="项目/T97_T19回归/工作区/日志/dispatch-T97-1-old.log",
            running_run_id="dispatch-T97-1-old",
            meta_extra="controller: fake-controller\n",
        )
        before = server._load_subtasks(project / "对接" / "subtasks.yaml")[0]
        self.assertEqual(before[0].status, "running")
        self.assertEqual(before[0].run_id, "dispatch-T97-1-old")
        self.assertTrue(before[0].log)

        repaired = server._reconcile_interrupted_subtasks()
        self.assertEqual(repaired, 1)
        after = server._load_subtasks(project / "对接" / "subtasks.yaml")[0]
        self.assertEqual(after[0].status, "failed")
        self.assertEqual(after[0].error, "interrupted")
        self.assertTrue(after[0].finished_at)
        self.assertEqual(after[0].log, before[0].log)
        self.assertEqual(after[0].run_id, before[0].run_id)

        task = self.find_task("T97")
        self.assertIn("T97-1", [item.id for item in server._dispatch_targets(task)])
        available, reason = server._dispatch_availability(task)
        self.assertTrue(available, reason)
        self.record_evidence(
            "06-f03-reconciled.json",
            json.dumps(
                {
                    "before": before[0].__dict__,
                    "after": after[0].__dict__,
                    "dispatch_availability": [available, reason],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

        task = self.find_task("T97")
        server._start_dispatch(task)
        final = self.wait_dispatch("T97")
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["success"], 1)
        refreshed = self.find_task("T97")
        self.assertEqual(refreshed.subtasks[0].status, "delivered")
        outdir = server._subtask_output_dir(refreshed, refreshed.subtasks[0])
        self.assertTrue((outdir / "DONE.md").is_file())
        self.assertTrue((outdir / "result.txt").is_file())
        log_path = server.RUNNER.resolve_log(refreshed.subtasks[0].log)
        self.assertIsNotNone(log_path, refreshed.subtasks[0].log)
        log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        self.assertIn(str(outdir), log_text)
        self.record_evidence(
            "06-f03-continue-dispatch.json",
            json.dumps(final, ensure_ascii=False, indent=2) + "\n",
        )
        self.record_evidence("06-f03-dispatch.log", log_text)

    def test_f03_live_lock_leaves_running_subtask_untouched(self) -> None:
        project = self.make_project(
            "T98",
            ["running"],
            deliver_files=False,
            running_log="项目/T98_T19回归/工作区/日志/dispatch-T98-1-live.log",
            running_run_id="dispatch-T98-1-live",
        )
        lock_path = server.RUNNER.lock_path
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            f"pid: {os.getpid()}\n"
            f"owner_pid: {os.getpid()}\n"
            f"agent_pid: {os.getpid()}\n"
            "task: T98\n"
            "agent: fake-builder\n"
            "started: 2026-10-26 09:00:00\n"
            f"started_epoch: {time.time()}\n"
            "token: live-lock-test\n",
            encoding="utf-8",
        )
        try:
            repaired = server._reconcile_interrupted_subtasks()
            self.assertEqual(repaired, 0)
            items = server._load_subtasks(project / "对接" / "subtasks.yaml")[0]
            self.assertEqual(items[0].status, "running")
            self.assertEqual(items[0].run_id, "dispatch-T98-1-live")
        finally:
            lock_path.unlink(missing_ok=True)
        self.record_evidence(
            "07-f03-live-lock.txt",
            "live lock -> running untouched\n",
        )

    # ------------------------------------------------------------------
    # F04 修订上下文
    # ------------------------------------------------------------------

    def test_f04_revision_prompt_contains_context_and_differs_from_first(
        self,
    ) -> None:
        task_id = "T99"
        review = REVIEW_TEMPLATE.format(task_id=task_id)
        project = self.make_project(
            task_id,
            ["delivered"],
            review=review,
            review_done=True,
            task_sheet_extra=(
                "\n## 禁止事项\n\n"
                "- BASELINE-LOCK：禁止修改基线文件\n"
                "- 禁止删除已有产物\n"
            ),
        )
        task = self.find_task(task_id)
        subtask = task.subtasks[0]
        role = server._find_role("demo-builder")
        self.assertIsNotNone(role)
        first_prompt = server._dispatch_prompt(role, subtask, task.workdir)
        self.assertNotIn("FIX-SECRET-42", first_prompt)
        self.assertNotIn("BASELINE-LOCK", first_prompt)

        server._start_revise(task)
        final = self.wait_advance(task_id)
        self.assertEqual(final["state"], "completed", final)
        self.assertEqual(final["conclusion"], "passed", final)

        dispatch_prompts = [
            prompt
            for prompt in self.prompts("dispatch")
            if f"执行子任务 {task_id}-1" in prompt
        ]
        self.assertEqual(len(dispatch_prompts), 1, dispatch_prompts)
        revision_prompt = dispatch_prompts[0]
        snapshot = project / "对接" / "审查历史" / "round-1.md"
        for expected in (
            "FIX-SECRET-42",
            "BASELINE-LOCK",
            "禁止删除已有产物",
            str(project / "对接" / "任务单.md"),
            str(snapshot),
            "第 1 轮",
            "回归验收要求",
        ):
            self.assertIn(expected, revision_prompt)
        self.assertNotEqual(revision_prompt, first_prompt)
        self.assertIn("FIX-SECRET-42", review)
        self.record_evidence(
            "08-f04-first-prompt.txt",
            first_prompt + "\n",
        )
        self.record_evidence(
            "08-f04-revision-prompt.txt",
            revision_prompt + "\n",
        )
        review_log = server.RUNNER.read_status().get("log")
        log_path = server.RUNNER.resolve_log(review_log)
        self.assertIsNotNone(log_path)
        review_log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        self.assertIn(str(server._subtask_output_dir(task, subtask)), review_log_text)

    # ------------------------------------------------------------------
    # F05 路径协议
    # ------------------------------------------------------------------

    def _assert_prompt_path_passes_g2(self, task_id: str) -> dict:
        task = self.find_task(task_id)
        subtask = task.subtasks[0]
        role = server._find_role("demo-builder")
        self.assertIsNotNone(role)
        output_dir = server._subtask_output_dir(task, subtask)
        prompt = server._dispatch_prompt(
            role, subtask, task.workdir, output_dir=output_dir
        )
        match = re.search(r"输出目录（绝对路径）：([^\n]+)", prompt)
        self.assertIsNotNone(match, prompt)
        prompt_output = Path(match.group(1).strip())
        done_match = re.search(r"完成后写 DONE\.md（绝对路径）：([^\n（]+)", prompt)
        self.assertIsNotNone(done_match, prompt)
        prompt_done = Path(done_match.group(1).strip())
        self.assertTrue(prompt_output.is_absolute())
        self.assertEqual(prompt_output, output_dir)
        self.assertEqual(prompt_done, output_dir / "DONE.md")

        prompt_output.mkdir(parents=True, exist_ok=True)
        prompt_done.write_text(
            "# DONE\n\n## 做了什么\n\n- 按提示词路径交付\n\n"
            "## 怎么验\n\n- 核对\n\n## 结果\n\n- 通过\n",
            encoding="utf-8",
        )
        (prompt_output / "artifact.txt").write_text("按提示词写盘\n", encoding="utf-8")
        server._update_subtask_record(
            task.subtasks_path, subtask.id, {"status": "delivered"}
        )
        refreshed = self.find_task(task_id)
        checks = [check for check in server._gate_g2(refreshed) if check["subtask"] == subtask.id]
        self.assertEqual(len(checks), 1)
        check = checks[0]
        self.assertTrue(check["passed"], check)
        self.assertEqual(check["missing"], [])
        self.assertEqual(
            str(Path(check["repair_paths"][0]).resolve()), str(prompt_done)
        )
        review_prompt = server._review_prompt(refreshed)
        self.assertIn(str(output_dir), review_prompt)
        self.assertIn(str(prompt_done), review_prompt)
        return {
            "prompt": prompt,
            "prompt_output": str(prompt_output),
            "prompt_done": str(prompt_done),
            "g2": check,
            "review_prompt": review_prompt,
        }

    def test_f05_default_workdir_prompt_path_passes_g2(self) -> None:
        project = self.make_project("T100", ["pending"], deliver_files=False)
        task = self.find_task("T100")
        self.assertEqual(
            server._subtask_output_dir(task, task.subtasks[0]),
            (project / "工作区" / "子任务" / "T100-1").resolve(),
        )
        evidence = self._assert_prompt_path_passes_g2("T100")
        self.record_evidence(
            "09-f05-default-workdir.json",
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        )

    def test_f05_external_workdir_prompt_path_passes_g2(self) -> None:
        external_workdir = self.external / "外部工作区"
        external_workdir.mkdir(parents=True, exist_ok=True)
        project = self.make_project(
            "T101",
            ["pending"],
            workdir=external_workdir,
            deliver_files=False,
        )
        task = self.find_task("T101")
        expected = (external_workdir / "子任务" / "T101-1").resolve()
        self.assertEqual(server._subtask_output_dir(task, task.subtasks[0]), expected)
        evidence = self._assert_prompt_path_passes_g2("T101")
        self.assertEqual(evidence["prompt_output"], str(expected))
        self.assertNotIn(
            str(project / "工作区" / "子任务"), evidence["prompt"]
        )
        self.assertFalse((project / "工作区" / "子任务" / "T101-1").exists())
        self.record_evidence(
            "10-f05-external-workdir.json",
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        )


if __name__ == "__main__":
    unittest.main()
