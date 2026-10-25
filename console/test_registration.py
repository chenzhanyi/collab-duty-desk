#!/usr/bin/env python3
"""T08 总控登记流程的隔离测试；只使用临时目录，不触碰 项目/ 与 协作/。"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
)

FAKE_CONTROLLER = r'''#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

project = Path.cwd()
docking = project / "对接"
idea = docking / "idea.md"
if not idea.is_file():
    print("missing idea", file=sys.stderr)
    raise SystemExit(4)

mode = os.environ.get("FAKE_CONTROLLER_MODE", "ok")
if mode == "fail":
    print("fake controller failure", file=sys.stderr)
    raise SystemExit(9)

task_id = project.name.split("_", 1)[0]
title = "临时登记测试"
if mode in {"partial", "slow"}:
    (docking / "任务单.md").write_text(
        f"# {task_id} — {title}\n\n## 目标\n\n只写任务单。\n",
        encoding="utf-8",
    )
    if mode == "slow":
        import time

        time.sleep(2)
    raise SystemExit(0)

(docking / "任务单.md").write_text(
    f"# {task_id} — {title}\n\n"
    "## 目标\n\n建立测试任务。\n\n"
    "## 范围\n\n只写测试文件。\n\n"
    "## 验收标准\n\n两个子任务可读。\n\n"
    "## 禁止事项\n\n不写审查.md。\n\n"
    "## 子任务清单\n\n- 子任务一\n- 子任务二\n",
    encoding="utf-8",
)
(docking / "subtasks.yaml").write_text(
    f"subtasks:\n"
    f"  - id: {task_id}-1\n"
    f"    title: 子任务一\n"
    f"    goal: 做第一件事\n"
    f"    acceptance: 第一件事可验收\n"
    f"    status: pending\n"
    f"    agent_role: designer\n"
    f"  - id: {task_id}-2\n"
    f"    title: 子任务二\n"
    f"    goal: 做第二件事\n"
    f"    acceptance: 第二件事可验收\n"
    f"    status: pending\n",
    encoding="utf-8",
)
(docking / "登记完成.md").write_text(
    f"# 登记完成\n\n- 任务编号：{task_id}\n- 子任务数量：2\n",
    encoding="utf-8",
)
print("fake controller registered", task_id)
'''


class RegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: getattr(server, name) for name in GLOBALS}
        self._temp = tempfile.TemporaryDirectory()
        self._workdir_temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.workdir = Path(self._workdir_temp.name).resolve()
        (self.root / "项目").mkdir(parents=True)
        (self.root / "归档").mkdir(parents=True)
        roles = self.root / "协作" / "roles"
        agents = self.root / "协作" / "agents"
        dispatch = self.root / "协作" / "调度台"
        roles.mkdir(parents=True)
        agents.mkdir(parents=True)
        dispatch.mkdir(parents=True)

        script = self.root / "fake-controller.py"
        script.write_text(FAKE_CONTROLLER, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        (roles / "controller.yaml").write_text(
            "id: controller\n"
            "name: 测试总控\n"
            "description: 隔离测试\n"
            "agent_id: fake-controller\n"
            "write_scope: []\n"
            "enabled: true\n"
            'notes: ""\n',
            encoding="utf-8",
        )
        (agents / "fake-controller.yaml").write_text(
            "id: fake-controller\n"
            "name: 测试总控 Agent\n"
            "command: "
            + str(script)
            + "\n"
            'args_template: \'"{prompt}"\'\n'
            f"cwd: {self.root}\n"
            "installed: true\n"
            "version: test\n"
            'notes: ""\n',
            encoding="utf-8",
        )

        server.PROJECT_DIR = self.root
        server.COLLAB_DIR = self.root / "协作"
        server.PROJECTS_DIR = self.root / "项目"
        server.ARCHIVE_DIR = self.root / "归档"
        server.DISPATCH_DIR = dispatch
        server.ROLES_DIR = roles
        server.AGENTS_DIR = agents
        server.ROLES_ARCHIVE_DIR = roles / ".archive"
        server.WATCH_STATE = dispatch / "watch.state"
        server.ROUNDS_TSV = dispatch / "rounds.tsv"
        server.JOURNAL_LOG = dispatch / "journal.log"
        server.GUARD_STATUS = dispatch / "guard.status"
        server.REGISTRATION_TIMEOUT_SECONDS = 8.0
        server.RUNNER = runner.Runner(self.root, timeout_seconds=8.0)
        server.ACTIVE_REGISTRATIONS.clear()
        self._old_mode = os.environ.get("FAKE_CONTROLLER_MODE")

    def tearDown(self) -> None:
        try:
            if server.RUNNER is not None:
                server.RUNNER.wait_for_registration_idle(8.0)
                server.RUNNER.wait_for_idle(8.0)
        finally:
            server.ACTIVE_REGISTRATIONS.clear()
            for name, value in self._saved.items():
                setattr(server, name, value)
            if self._old_mode is None:
                os.environ.pop("FAKE_CONTROLLER_MODE", None)
            else:
                os.environ["FAKE_CONTROLLER_MODE"] = self._old_mode
            self._temp.cleanup()
            self._workdir_temp.cleanup()

    def wait_status(self, project: Path, timeout: float = 8.0) -> dict:
        task_id = project.name.split("_", 1)[0]
        deadline = time.time() + timeout
        while time.time() < deadline:
            meta = server._read_task_meta(project, task_id)
            status = str(meta.get("status") or "")
            if status and status != server.REGISTERING:
                return meta
            time.sleep(0.05)
        self.fail("登记没有在超时前结束")

    def test_failed_registration_can_retry_to_registered(self) -> None:
        idea = "测试想法：请登记并拆两个子任务。"
        os.environ["FAKE_CONTROLLER_MODE"] = "partial"
        created = server._create_published_task(
            {"idea": idea, "title": "", "workdir": str(self.workdir)}
        )
        project = created["project_dir"]
        task_id = str(created["task_id"])
        self.assertEqual(
            (project / "对接" / "idea.md").read_text(encoding="utf-8"), idea
        )
        controller = created["controller"]
        self.assertIsNotNone(controller)

        server._start_registration(
            task_id=task_id,
            project_dir=project,
            title_hint="",
            controller=controller,
        )
        failed = self.wait_status(project)
        self.assertEqual(failed.get("status"), server.REGISTER_FAILED)
        self.assertTrue(failed.get("register_error"))
        log_path = self.root / str(failed.get("register_log") or "")
        self.assertTrue(log_path.is_file(), log_path)
        self.assertTrue(server.RUNNER.wait_for_registration_idle(8.0))
        self.assertFalse(server.RUNNER.lock_path.exists())

        os.environ["FAKE_CONTROLLER_MODE"] = "ok"
        server._start_registration(
            task_id=task_id,
            project_dir=project,
            title_hint="",
            controller=controller,
        )
        registered = self.wait_status(project)
        self.assertEqual(registered.get("status"), server.REGISTERED)
        self.assertEqual(registered.get("title"), "临时登记测试")
        self.assertTrue(server.RUNNER.wait_for_registration_idle(8.0))
        self.assertFalse(server.RUNNER.lock_path.exists())

        subtasks, parse_error = server._load_subtasks(
            project / "对接" / "subtasks.yaml"
        )
        self.assertEqual(parse_error, "")
        self.assertEqual(len(subtasks), 2)
        self.assertEqual({item.id for item in subtasks}, {f"{task_id}-1", f"{task_id}-2"})
        self.assertTrue((project / "对接" / "登记完成.md").is_file())
        self.assertEqual(
            (project / "对接" / "task.yaml").read_text(encoding="utf-8").count(
                "status: registered"
            ),
            1,
        )

        tasks = server._load_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].subtasks_done, 0)
        self.assertEqual(len(tasks[0].subtasks), 2)

    def test_archive_is_refused_while_registration_holds_lock(self) -> None:
        idea = "测试登记期间归档。"
        os.environ["FAKE_CONTROLLER_MODE"] = "slow"
        created = server._create_published_task(
            {"idea": idea, "title": "", "workdir": str(self.workdir)}
        )
        project = created["project_dir"]
        task_id = str(created["task_id"])
        controller = created["controller"]

        # 模拟上一轮留下的 runner.status：snapshot() 会保留旧 task；
        # 归档守卫必须以当前锁上的 task 为准。
        server.RUNNER.dispatch_dir.mkdir(parents=True, exist_ok=True)
        server.RUNNER.status_path.write_text(
            "state: success\n"
            "task: T88\n"
            "agent: old\n"
            "run_id: run-old\n"
            "started: '2026-01-01 00:00:00'\n"
            "started_epoch: 1767225600\n"
            "log: ''\n"
            "exit: 0\n",
            encoding="utf-8",
        )

        server._start_registration(
            task_id=task_id,
            project_dir=project,
            title_hint="",
            controller=controller,
        )
        task_sheet = project / "对接" / "任务单.md"
        deadline = time.time() + 3
        while not task_sheet.is_file() and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(task_sheet.is_file())
        with self.assertRaises(server.ActionError) as caught:
            server._archive_project(task_id, project.name)
        self.assertEqual(caught.exception.status, 409)
        self.assertTrue(project.is_dir())
        self.assertFalse((self.root / "归档" / project.name).exists())
        self.assertTrue(server.RUNNER.wait_for_registration_idle(8.0))
        self.assertEqual(
            server._read_task_meta(project, task_id).get("status"),
            server.REGISTER_FAILED,
        )

    def test_stale_files_do_not_satisfy_a_newer_registration(self) -> None:
        project = self.root / "项目" / "T71_旧产物检查"
        docking = project / "对接"
        docking.mkdir(parents=True)
        idea = "人类原文"
        (docking / "idea.md").write_text(idea, encoding="utf-8")
        (docking / "任务单.md").write_text(
            "# T71 — 旧产物检查\n\n## 目标\n\n旧内容\n", encoding="utf-8"
        )
        (docking / "subtasks.yaml").write_text(
            "subtasks:\n"
            "  - id: T71-1\n"
            "    title: 一\n"
            "    status: pending\n"
            "  - id: T71-2\n"
            "    title: 二\n"
            "    status: pending\n",
            encoding="utf-8",
        )
        (docking / "登记完成.md").write_text("旧完成\n", encoding="utf-8")
        old_time = time.time() - 60
        for name in ("任务单.md", "subtasks.yaml", "登记完成.md"):
            os.utime(docking / name, (old_time, old_time))
        idea_hash = hashlib.sha256(idea.encode("utf-8")).hexdigest()
        started = time.time() - 5
        error, _title, _subtasks = server._registration_result_error(
            {"state": "success", "started_epoch": started},
            project_dir=project,
            idea_hash=idea_hash,
            start_epoch=started,
        )
        self.assertIn("未在本次登记中写入", error)

        now = time.time()
        os.utime(docking / "任务单.md", (now - 1, now - 1))
        os.utime(docking / "subtasks.yaml", (now - 1, now - 1))
        os.utime(docking / "登记完成.md", (now, now))
        error, title, subtasks = server._registration_result_error(
            {"state": "success", "started_epoch": started},
            project_dir=project,
            idea_hash=idea_hash,
            start_epoch=started,
        )
        self.assertEqual(error, "")
        self.assertEqual(title, "旧产物检查")
        self.assertEqual(len(subtasks), 2)


if __name__ == "__main__":
    unittest.main()
