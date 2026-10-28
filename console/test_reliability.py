"""Regression tests for the 2026-10-27 audit, using only temporary tasks."""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner
import server
import test_advance as fixtures

SOURCE_ROOT = Path(__file__).resolve().parents[1]


class ReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdvanceTest()
        self.fixture.setUp()
        os.environ["T14_AGENT_SLEEP"] = "0.05"

    def tearDown(self):
        self.fixture.tearDown()

    def wait_dispatch(self):
        deadline = time.monotonic() + 8
        while server._active_dispatch_task() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(server._active_dispatch_task())

    def test_stop_between_children_waits_for_worker_and_cancels_next_launch(self):
        self.fixture.make_task("T90", ["pending", "pending"])
        engine = server.RUNNER
        original = engine.wait_for_idle
        gap, resume = threading.Event(), threading.Event()
        paused = False

        def wait(timeout=None):
            nonlocal paused
            done = original(timeout)
            if done and threading.current_thread().name == "dispatch-T90" and not paused:
                paused = True
                gap.set()
                resume.wait(5)
            return done

        stopped = []
        with mock.patch.object(engine, "wait_for_idle", wait):
            server._start_advance(server._find_task("T90"))
            self.assertTrue(gap.wait(5))
            thread = threading.Thread(target=lambda: stopped.append(server._stop_advance("T90")))
            thread.start()
            try:
                deadline = time.monotonic() + 3
                while not server._advance_stop_requested() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(server._advance_stop_requested())
                self.assertTrue(thread.is_alive(), "stop must await its outstanding worker")
            finally:
                resume.set()
                thread.join(8)
            self.assertFalse(thread.is_alive())
        self.assertEqual(stopped[0]["state"], "stopped")
        self.assertTrue(stopped[0]["lock_released"])
        self.assertFalse(any("dispatch start T90-2" in line for line in self.fixture.order_lines()))
        self.assertEqual(server._find_task("T90").subtasks[1].status, "pending")

    def test_result_belongs_to_run_even_if_other_runner_overwrites_status(self):
        self.fixture.make_task("T90", ["pending", "pending"])
        other_task = self.fixture.make_task("T91", ["pending", "pending"])
        os.environ["T14_MODE"] = "fail_dispatch"
        engine = server.RUNNER
        original = engine.wait_for_idle
        gap, resume = threading.Event(), threading.Event()
        paused = False

        def wait(timeout=None):
            nonlocal paused
            done = original(timeout)
            if done and threading.current_thread().name == "dispatch-T90" and not paused:
                paused = True
                gap.set()
                resume.wait(5)
            return done

        with mock.patch.object(engine, "wait_for_idle", wait):
            server._start_dispatch(server._find_task("T90"))
            try:
                self.assertTrue(gap.wait(5))
                failed_id = engine.read_status()["run_id"]
                other = runner.Runner(self.fixture.root)
                launched = other.launch(task_id="T91", task_dir=other_task, round_no=1,
                    agent=runner.AgentSpec("other", "other", "/bin/sh", '-c "exit 0" -- "{prompt}"'))
                self.assertTrue(other.wait_for_idle(5))
                self.assertNotEqual(failed_id, launched["run_id"])
            finally:
                resume.set()
            self.wait_dispatch()
        item = server._find_task("T90").subtasks[0]
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.run_id, failed_id)

    def test_two_stale_lock_acquirers_cannot_both_succeed(self):
        first, second = runner.Runner(self.fixture.root), runner.Runner(self.fixture.root)
        first.lock_path.write_text("owner_pid: 999999999\ntoken: stale\n")
        gap, resume = threading.Event(), threading.Event()
        original = Path.unlink
        acquired, busy, errors = [], [], []

        def unlink(path, *args, **kwargs):
            if path == first.lock_path and threading.current_thread().name == "first-acquirer" and not gap.is_set():
                gap.set()
                resume.wait(5)
            return original(path, *args, **kwargs)

        def acquire(engine, name):
            try:
                engine._acquire_lock({"owner_pid": os.getpid(), "task": "T90", "token": name})
                acquired.append(name)
            except runner.RunnerBusy:
                busy.append(name)
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(Path, "unlink", unlink):
            a = threading.Thread(target=acquire, args=(first, "a"), name="first-acquirer")
            b = threading.Thread(target=acquire, args=(second, "b"))
            a.start()
            self.assertTrue(gap.wait(5))
            b.start()
            resume.set()
            a.join(5)
            b.join(5)
        try:
            self.assertEqual(errors, [])
            self.assertEqual(acquired, ["a"])
            self.assertEqual(busy, ["b"])
            self.assertEqual(first.lock_info()["token"], "a")
        finally:
            first._release_lock("a")

    def test_publish_number_is_unique_across_processes(self):
        context = multiprocessing.get_context("fork")
        barrier, results = context.Barrier(2), context.Queue()
        def publish(title):
            barrier.wait(5)
            result = server._create_published_task({"title": title, "idea": title,
                "workdir": str(self.fixture.external_workdir)})
            results.put(result["task_id"])
        children = [context.Process(target=publish, args=(name,)) for name in ["one", "two"]]
        try:
            for child in children:
                child.start()
            ids = [results.get(timeout=8) for _ in children]
            self.assertEqual(len(set(ids)), 2)
        finally:
            for child in children:
                child.join(5)
                if child.is_alive():
                    child.terminate()
                    child.join()

    def test_revision_retry_retains_context_without_consuming_a_round(self):
        project = self.fixture.make_task("T90", ["delivered", "delivered"])
        review = server._review_attribution_header("审核", "fake-reviewer") + (
            "# 审查\n\n## 结论\n\n结论：**须修正**\n\n## 问题清单\n\n- T90-1：修复 UNIQUE-RETRY-CONTEXT。\n")
        (project / "对接/审查.md").write_text(review)
        (project / "对接/审核完成.md").write_text("完成")
        os.environ["T14_MODE"] = "fail_dispatch"
        server._start_revise(server._find_task("T90"))
        self.assertEqual(self.fixture.wait_advance("T90")["state"], "failed")
        task = server._find_task("T90")
        self.assertTrue(server._advance_availability(task)[0])
        self.assertTrue(server._revise_status_payload(task)["resumable"])
        os.environ["T14_MODE"] = "pass"
        resumed = server._start_revise(task)
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["revise_round"], 1)
        final = self.fixture.wait_advance("T90")
        self.assertEqual(final["conclusion"], "passed", final)
        self.assertEqual(server._find_task("T90").revise_round, 1)
        self.assertIn("UNIQUE-RETRY-CONTEXT", final["revise_context"]["issues_full"])

    def test_dispatch_always_includes_parent_prohibitions(self):
        project = self.fixture.make_task("T90", ["pending", "pending"])
        with (project / "对接/任务单.md").open("a") as handle:
            handle.write("\n## 禁止事项\n\n- UNIQUE-PARENT-CONSTRAINT\n")
        server._start_dispatch(server._find_task("T90"))
        self.wait_dispatch()
        log = server._find_task("T90").subtasks[0].log
        prompt = server.RUNNER.resolve_log(log).read_text()
        self.assertIn("UNIQUE-PARENT-CONSTRAINT", prompt)
        self.assertIn(str(project / "对接/任务单.md"), prompt)

    def test_find_one_task_builds_only_the_requested_task(self):
        for index in range(20):
            self.fixture.make_task(f"T{100 + index}", ["pending", "pending"])
        with mock.patch.object(server, "_build_task", wraps=server._build_task) as build:
            task = server._find_task("T100")
        self.assertEqual(task.task_id, "T100")
        self.assertEqual(build.call_count, 1)

    def install_helpers(self):
        (self.fixture.root / "console").mkdir(exist_ok=True)
        for name in ["storage.py", "policy.py"]:
            shutil.copy2(SOURCE_ROOT / "console" / name, self.fixture.root / "console" / name)
        for name in ["state.py", "watch.sh", "ack.sh", "since.sh", "rounds.sh"]:
            shutil.copy2(SOURCE_ROOT / "协作/调度台" / name, self.fixture.dispatch_dir / name)

    def helper(self, name, *args):
        environment = dict(os.environ)
        environment.pop("COLLAB_ROOT", None)
        return subprocess.run(["bash", str(self.fixture.dispatch_dir / name), *args],
            text=True, capture_output=True, timeout=8, env=environment)

    def test_watcher_detects_equal_size_equal_timestamp_content_change(self):
        self.install_helpers()
        project = self.fixture.make_task("T90", ["pending", "pending"])
        path = project / "对接/idea.md"
        path.write_text("abc")
        self.assertEqual(self.helper("ack.sh").returncode, 0)
        stat = path.stat()
        path.write_text("xyz")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        result = self.helper("watch.sh", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESULT=CHANGE", result.stdout)
        self.assertIn(str(path), result.stdout)

    def test_snapshots_share_unchanged_files_but_preserve_old_contents(self):
        self.install_helpers()
        project = self.fixture.make_task("T90", ["pending", "pending"])
        path = project / "对接/idea.md"
        path.write_text("old")
        for _ in range(2):
            result = self.helper("ack.sh")
            self.assertEqual(result.returncode, 0, result.stderr)
        snapshots = sorted((self.fixture.dispatch_dir / "snapshots").iterdir())
        relative = path.relative_to(self.fixture.root)
        self.assertEqual((snapshots[0] / relative).stat().st_ino, (snapshots[1] / relative).stat().st_ino)
        path.write_text("new")
        self.assertEqual((snapshots[0] / relative).read_text(), "old")
        result = self.helper("ack.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        diff = self.helper("since.sh")
        self.assertIn("-old", diff.stdout)
        self.assertIn("+new", diff.stdout)

    def test_rounds_helper_uses_checkout_and_shared_limit(self):
        self.install_helpers()
        self.fixture.make_task("T90", ["pending", "pending"])
        (self.fixture.root / "协作/settings.json").write_text('{"max_revise_rounds": 1}')
        result = self.helper("rounds.sh", "bump", "T90")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(server._find_task("T90").revise_round, 1)
        self.assertTrue((self.fixture.dispatch_dir / "rounds.tsv").is_file())
        self.assertEqual(self.helper("rounds.sh", "bump", "T90").returncode, 1)

    def test_registration_snapshot_reports_the_live_lock_identity(self):
        first = self.fixture.make_task("T90", ["pending", "pending"])
        second = self.fixture.make_task("T91", ["pending", "pending"])
        (second / "对接/idea.md").write_text("temporary registration")
        engine = server.RUNNER
        agent = runner.AgentSpec("fake", "fake", "/bin/sh", '-c "sleep .2" -- "{prompt}"')
        engine.launch(task_id="T90", task_dir=first, agent=agent, round_no=1)
        self.assertTrue(engine.wait_for_idle(5))
        try:
            engine.launch_registration(task_id="T91", task_dir=second, agent=agent, prompt="test")
            current = engine.snapshot()
            self.assertEqual(current["task"], "T91")
            self.assertEqual(current["kind"], "registration")
            self.assertNotIn("log", current, "previous run's log must not be attached to registration")
        finally:
            engine.wait_for_registration_idle(5)

    def test_revision_page_offers_stop_while_the_same_round_is_running(self):
        project = self.fixture.make_task("T90", ["delivered", "delivered"])
        (project / "对接/审查.md").write_text(server._review_attribution_header("审核", "fake-reviewer")
            + "# 审查\n\n## 结论\n\n结论：**须修正**\n\n## 问题清单\n\n- T90-1：补充校验。\n")
        (project / "对接/审核完成.md").write_text("完成")
        os.environ["T14_AGENT_SLEEP"] = "5"
        server._start_revise(server._find_task("T90"))
        try:
            page = server._render_task_detail(server._find_task("T90"))
            self.assertIn('action="/advance/T90/stop"', page)
            self.assertIn("停止修正", page)
        finally:
            final = server._stop_advance("T90")
        self.assertEqual(final["state"], "stopped")
        task = server._find_task("T90")
        self.assertEqual(task.revise_round, 1)
        self.assertTrue(server._revise_status_payload(task)["resumable"])

    def test_closed_runner_rejects_new_processes(self):
        project = self.fixture.make_task("T90", ["pending", "pending"])
        server.RUNNER.shutdown()
        with self.assertRaises(runner.RunnerError):
            server.RUNNER.launch(task_id="T90", task_dir=project, round_no=1,
                agent=runner.AgentSpec("fake", "fake", "/bin/echo", '"{prompt}"'))


if __name__ == "__main__":
    unittest.main()
