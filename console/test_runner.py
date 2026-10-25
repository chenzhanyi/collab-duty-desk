#!/usr/bin/env python3
"""执行器核心的隔离测试；只用标准库与临时目录。"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runner  # noqa: E402


def shell_agent(script: str, agent_id: str = "fake") -> runner.AgentSpec:
    template = f"-c {shlex.quote(script)} -- \"{{prompt}}\""
    return runner.AgentSpec(
        id=agent_id,
        name=f"Fake {agent_id}",
        command="/bin/sh",
        args_template=template,
    )


class RunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.task_id = "T90"
        self.task = self.root / "项目" / "T90_执行器测试"
        (self.task / "对接").mkdir(parents=True)
        (self.task / "对接" / "任务单.md").write_text(
            "# T90 — 执行器测试\n\n只用于测试。\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def make_runner(self, timeout: float = 10.0) -> runner.Runner:
        return runner.Runner(self.root, timeout_seconds=timeout)

    def launch(
        self,
        engine: runner.Runner,
        agent: runner.AgentSpec,
        *,
        task: Path = None,
        task_id: str = None,
        workdir: Path = None,
    ) -> dict:
        return engine.launch(
            task_id=task_id or self.task_id,
            task_dir=task or self.task,
            agent=agent,
            round_no=1,
            workdir=workdir,
        )

    def test_task_directory_must_match_task_id(self) -> None:
        engine = self.make_runner()
        other = self.root / "项目" / "T91_另一个项目"
        (other / "对接").mkdir(parents=True)
        (other / "对接" / "任务单.md").write_text("# T91\n", encoding="utf-8")
        with self.assertRaises(runner.RunnerConfigError):
            engine.launch(
                task_id="T92",
                task_dir=other,
                agent=shell_agent("printf 'must-not-run\\n'"),
                round_no=1,
            )
        self.assertFalse(engine.lock_path.exists())

    def test_prompt_and_argv(self) -> None:
        workdir = self.root / "外部工作目录"
        prompt = runner.build_prompt(
            "T90",
            3,
            desk_root=self.root,
            task_dir=self.task,
            workdir=workdir,
        )
        self.assertIn("任务 T90", prompt)
        self.assertIn("对接/交付/r3/", prompt)
        self.assertIn("DONE.md", prompt)
        self.assertIn("不得写 `对接/审查.md`", prompt)
        self.assertIn("完成后退出", prompt)
        self.assertIn(f"值班台={self.root}", prompt)
        self.assertIn(f"工作目录={workdir}", prompt)
        self.assertIn("不得向值班台的 `协作/` 写任何东西", prompt)
        self.assertIn(str(self.task / "对接" / "任务单.md"), prompt)
        self.assertIn(str(workdir), prompt)

        agent = runner.AgentSpec(
            "x",
            "X",
            "/bin/echo",
            "-p \"{prompt}\" --allowedTools \"Read,Write\"",
        )
        argv = runner.build_argv(agent, prompt)
        self.assertEqual(argv[0], "/bin/echo")
        self.assertEqual(argv[1], "-p")
        self.assertEqual(argv[2], prompt)
        self.assertEqual(argv[3:], ["--allowedTools", "Read,Write"])
        self.assertEqual(sum(1 for item in argv if prompt in item), 1)

        with self.assertRaises(runner.RunnerConfigError):
            runner.build_argv(
                runner.AgentSpec("x", "X", "/bin/echo", "--no-placeholder"), "hi"
            )
        with self.assertRaises(runner.RunnerConfigError):
            runner.build_argv(
                runner.AgentSpec(
                    "x", "X", "/bin/echo", "{prompt} {prompt}"
                ),
                "hi",
            )

    def test_success_releases_lock_and_keeps_log_history(self) -> None:
        engine = self.make_runner()
        agent = shell_agent("printf 'hello-out\\n'; printf 'hello-err\\n' >&2")
        result = self.launch(engine, agent)
        self.assertEqual(result["state"], "running")
        self.assertTrue(engine.lock_path.is_file())
        self.assertTrue(engine.wait_for_idle(10))

        snapshot = engine.snapshot()
        self.assertEqual(snapshot["state"], "success")
        self.assertEqual(snapshot["exit"], 0)
        self.assertFalse(snapshot["running"])
        self.assertFalse(engine.lock_path.exists())

        tail = engine.tail_log(snapshot.get("log"), 30)
        self.assertTrue(any("hello-out" in line for line in tail))
        self.assertTrue(any("hello-err" in line for line in tail))
        self.assertTrue(any("state: success" in line for line in tail))

        history = engine.history(10)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["state"], "success")
        self.assertEqual(history[0]["task"], "T90")
        self.assertEqual(history[0]["exit"], 0)
        self.assertGreaterEqual(history[0]["duration_seconds"], 0)

        status = engine.read_status()
        for key in ("state", "task", "agent", "pid", "started", "log", "exit"):
            self.assertIn(key, status)

    def test_workdir_is_cwd_and_artifacts_stay_outside_desk(self) -> None:
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        workdir = Path(external.name).resolve()
        engine = self.make_runner()
        agent = shell_agent("pwd; printf 'made-in-workdir\\n' > artifact.txt")
        self.launch(engine, agent, workdir=workdir)
        self.assertTrue(engine.wait_for_idle(10))
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["state"], "success")
        tail = engine.tail_log(snapshot.get("log"), 60)
        self.assertTrue(any(str(workdir) in line for line in tail), tail)
        self.assertTrue(any(f"cwd: {workdir}" in line for line in tail), tail)
        self.assertTrue(any(f"workdir: {workdir}" in line for line in tail), tail)
        self.assertTrue((workdir / "artifact.txt").is_file())
        self.assertEqual(
            (workdir / "artifact.txt").read_text(encoding="utf-8").strip(),
            "made-in-workdir",
        )
        log_path = engine.resolve_log(snapshot.get("log"))
        self.assertIsNotNone(log_path)
        self.assertEqual(
            log_path.resolve().parent, (self.task / "工作区" / "日志").resolve()
        )
        self.assertFalse((workdir / "日志").exists())
        self.assertFalse(list(workdir.glob("run-*.log")))
        self.assertFalse((self.task / "artifact.txt").exists())

    def test_relative_or_desk_root_workdir_is_rejected_before_spawn(self) -> None:
        engine = self.make_runner()
        for value in (Path("相对目录"), self.root):
            with self.assertRaises(runner.RunnerConfigError):
                self.launch(engine, shell_agent("printf 'must-not-run\\n'"), workdir=value)
            self.assertFalse(engine.lock_path.exists())

    def test_collab_workdir_is_rejected_before_spawn(self) -> None:
        collab = self.root / "协作"
        collab.mkdir(parents=True, exist_ok=True)
        engine = self.make_runner()
        with self.assertRaises(runner.RunnerConfigError):
            self.launch(
                engine,
                shell_agent("printf 'must-not-run\\n'"),
                workdir=collab / "sub",
            )
        self.assertFalse(engine.lock_path.exists())

    def test_second_launch_is_rejected_while_running(self) -> None:
        engine = self.make_runner(timeout=2.0)
        first = shell_agent("sleep 30", agent_id="slow")
        second = shell_agent("printf 'must-not-run\\n'", agent_id="second")
        self.launch(engine, first)
        with self.assertRaises(runner.RunnerBusy) as caught:
            self.launch(engine, second)
        self.assertIn("已有任务在跑", str(caught.exception))
        self.assertEqual(caught.exception.current.get("task"), "T90")
        self.assertTrue(engine.wait_for_idle(10))
        self.assertFalse(engine.lock_path.exists())
        self.assertEqual(len(engine.history(10)), 1)

    def test_registration_reuses_lock_logs_and_releases(self) -> None:
        engine = self.make_runner(timeout=5.0)
        (self.task / "对接" / "idea.md").write_text(
            "人类原文\n", encoding="utf-8"
        )
        finished = {}

        snapshot = engine.launch_registration(
            task_id=self.task_id,
            task_dir=self.task,
            agent=shell_agent("pwd; printf 'registered\\n'"),
            prompt="register T90 idea",
            timeout_seconds=5.0,
            on_finish=finished.update,
        )
        self.assertTrue(snapshot.get("ok"))
        self.assertTrue(engine.lock_info()["active"])
        with self.assertRaises(runner.RunnerBusy):
            self.launch(engine, shell_agent("printf 'must-not-run\\n'"))
        self.assertTrue(engine.wait_for_registration_idle(5.0))
        self.assertFalse(engine.lock_info()["active"])
        self.assertFalse(engine.lock_path.exists())
        self.assertEqual(finished.get("state"), "success")
        self.assertIn("register-", str(finished.get("run_id")))
        self.assertFalse(engine.status_path.exists())

        log_path = Path(str(finished.get("log_path")))
        self.assertTrue(log_path.is_file())
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn("总控登记", log_text)
        self.assertIn(str(self.task), log_text)
        self.assertIn("registered", log_text)
        self.assertFalse(finished.get("timed_out"))

    def test_registration_timeout_releases_lock(self) -> None:
        engine = self.make_runner(timeout=5.0)
        (self.task / "对接" / "idea.md").write_text(
            "人类原文\n", encoding="utf-8"
        )
        finished = {}
        engine.launch_registration(
            task_id=self.task_id,
            task_dir=self.task,
            agent=shell_agent("sleep 5"),
            prompt="register slow",
            timeout_seconds=0.5,
            on_finish=finished.update,
        )
        self.assertTrue(engine.wait_for_registration_idle(8.0))
        self.assertFalse(engine.lock_path.exists())
        self.assertEqual(finished.get("state"), "timeout")
        self.assertTrue(finished.get("timed_out"))

    def test_normal_exit_kills_background_descendants_before_lock_release(self) -> None:
        engine = self.make_runner()
        child_pid_file = self.root / "grandchild.pid"
        agent = shell_agent(
            "sleep 30 & echo $! > "
            + shlex.quote(str(child_pid_file))
            + "; echo parent-done"
        )
        self.launch(engine, agent)
        self.assertTrue(engine.wait_for_idle(10))
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["state"], "success")
        self.assertFalse(engine.lock_path.exists())
        self.assertTrue(child_pid_file.is_file())
        child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
        deadline = time.time() + 3
        alive = True
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        self.assertFalse(alive, "background descendant survived and could still write")
        tail = engine.tail_log(snapshot.get("log"), 40)
        self.assertTrue(any("parent-done" in line for line in tail))
        self.assertTrue(any("residual process group" in line for line in tail))

    def test_timeout_kills_process_group_and_releases_lock(self) -> None:
        engine = self.make_runner(timeout=0.6)
        agent = shell_agent("sleep 30")
        started = time.time()
        self.launch(engine, agent)
        self.assertTrue(engine.wait_for_idle(10))
        self.assertLess(time.time() - started, 10)
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["state"], "timeout")
        self.assertEqual(snapshot["exit"], "timeout")
        self.assertFalse(snapshot["running"])
        self.assertFalse(engine.lock_path.exists())
        tail = engine.tail_log(snapshot.get("log"), 30)
        self.assertTrue(any("state: timeout" in line for line in tail))

    def test_crash_is_failed_and_releases_lock(self) -> None:
        engine = self.make_runner()
        agent = shell_agent("printf 'before-crash\\n'; kill -9 $$")
        self.launch(engine, agent)
        self.assertTrue(engine.wait_for_idle(10))
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["state"], "failed")
        self.assertEqual(snapshot["exit"], -9)
        self.assertFalse(engine.lock_path.exists())
        tail = engine.tail_log(snapshot.get("log"), 30)
        self.assertTrue(any("before-crash" in line for line in tail))

    def test_nonzero_exit_is_failed_and_releases_lock(self) -> None:
        engine = self.make_runner()
        agent = shell_agent("exit 7")
        self.launch(engine, agent)
        self.assertTrue(engine.wait_for_idle(10))
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["state"], "failed")
        self.assertEqual(snapshot["exit"], 7)
        self.assertFalse(engine.lock_path.exists())

    def test_stale_lock_is_cleaned(self) -> None:
        engine = self.make_runner()
        engine.dispatch_dir.mkdir(parents=True, exist_ok=True)
        engine.lock_path.write_text(
            "pid: 999999\n"
            "owner_pid: 999999\n"
            "task: T90\n"
            "agent: orphan\n"
            "started: '2026-01-01 00:00:00'\n"
            "started_epoch: 1767225600\n"
            "token: stale-token\n",
            encoding="utf-8",
        )
        self.launch(engine, shell_agent("printf 'after-stale\\n'"))
        self.assertTrue(engine.wait_for_idle(10))
        self.assertEqual(engine.snapshot()["state"], "success")
        self.assertFalse(engine.lock_path.exists())

    def test_archived_log_is_still_readable(self) -> None:
        engine = self.make_runner()
        self.launch(engine, shell_agent("printf 'archive-me\\n'"))
        self.assertTrue(engine.wait_for_idle(10))
        record = engine.history(10)[0]
        archive = self.root / "归档" / self.task.name
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.task), str(archive))
        log_path = engine.resolve_log(record.get("log"))
        self.assertIsNotNone(log_path)
        self.assertEqual(
            log_path.resolve().parent, (archive / "工作区" / "日志").resolve()
        )
        self.assertTrue(any("archive-me" in line for line in engine.tail_log(record)))

    def test_reconcile_does_not_touch_live_lock(self) -> None:
        engine = self.make_runner()
        engine.dispatch_dir.mkdir(parents=True, exist_ok=True)
        engine.status_path.write_text(
            "state: running\n"
            "task: T90\n"
            "agent: live\n"
            f"pid: {os.getpid()}\n"
            f"owner_pid: {os.getpid()}\n"
            "run_id: run-live\n"
            "started: '2026-01-01 00:00:00'\n"
            "started_epoch: 1767225600\n"
            "log: ''\n"
            "exit: ''\n",
            encoding="utf-8",
        )
        engine.lock_path.write_text(
            f"pid: {os.getpid()}\n"
            f"owner_pid: {os.getpid()}\n"
            "task: T90\n"
            "agent: live\n"
            "started: '2026-01-01 00:00:00'\n"
            "started_epoch: 1767225600\n"
            "token: live-token\n",
            encoding="utf-8",
        )
        self.assertIsNone(engine.reconcile())
        self.assertEqual(engine.snapshot()["state"], "running")
        self.assertTrue(engine.lock_path.exists())

    def test_reconcile_marks_orphan_running_status_failed(self) -> None:
        engine = self.make_runner()
        engine.dispatch_dir.mkdir(parents=True, exist_ok=True)
        engine.status_path.write_text(
            "state: running\n"
            "task: T90\n"
            "agent: orphan\n"
            "pid: 999999\n"
            "owner_pid: 999998\n"
            "run_id: run-orphan\n"
            "started: '2026-01-01 00:00:00'\n"
            "started_epoch: 1767225600\n"
            "log: ''\n"
            "exit: ''\n",
            encoding="utf-8",
        )
        updated = engine.reconcile()
        self.assertIsNotNone(updated)
        snapshot = engine.snapshot()
        self.assertEqual(snapshot["state"], "failed")
        self.assertEqual(snapshot["exit"], "lost")
        history = engine.history(10)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["run_id"], "run-orphan")
        self.assertEqual(history[0]["state"], "failed")

    def test_history_limit_and_jsonl_is_append_only(self) -> None:
        engine = self.make_runner()
        for index in range(3):
            self.launch(
                engine,
                shell_agent(f"printf 'run-{index}\\n'", agent_id=f"agent-{index}"),
            )
            self.assertTrue(engine.wait_for_idle(10))
        lines = engine.history_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        for line in lines:
            payload = json.loads(line)
            self.assertEqual(payload["task"], "T90")
        recent = engine.history(2)
        self.assertEqual(len(recent), 2)
        self.assertGreaterEqual(
            recent[0]["finished_epoch"], recent[1]["finished_epoch"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
