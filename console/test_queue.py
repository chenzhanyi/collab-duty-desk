"""Serial queue contracts and real workflows; only temporary roots and fake agents."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
import task_queue
import test_advance as fixtures


def wait_until(test, condition, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    test.fail("等待队列状态超时")


class QueueStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "task-queue.status.json"
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []

        def run(task_id, progress):
            self.calls.append(task_id)
            progress("执行子任务", "fake")
            self.entered.set()
            self.release.wait(5)
            return {"ok": True}

        self.queue = task_queue.TaskQueue(self.path, run)

    def tearDown(self):
        self.release.set()
        self.queue.pause()
        if self.queue.thread:
            self.queue.thread.join(8)
        self.temp.cleanup()

    def add(self):
        self.queue.append([self.queue.item("T01", "first"), self.queue.item("T02", "second")])

    def test_pause_finishes_current_and_resume_preserves_order(self):
        self.add()
        self.queue.start()
        self.assertTrue(self.entered.wait(2))
        self.queue.pause()
        self.assertEqual(self.queue.snapshot()["state"], "pausing")
        self.release.set()
        self.queue.thread.join(3)
        state = self.queue.snapshot()
        self.assertEqual(state["state"], "paused")
        self.assertEqual(self.calls, ["T01"])
        self.assertEqual([item["status"] for item in state["items"]], ["completed", "pending"])
        self.queue.start()
        self.queue.thread.join(3)
        self.assertEqual(self.queue.snapshot()["state"], "completed")
        self.assertEqual(self.calls, ["T01", "T02"])

    def test_order_conflict_and_running_item_cannot_be_removed(self):
        self.add()
        old = self.queue.snapshot()["order_revision"]
        self.queue.edit("reorder", ids=["T02", "T01"], order_revision=old)
        with self.assertRaises(task_queue.QueueError):
            self.queue.edit("reorder", ids=["T01", "T02"], order_revision=old)
        self.queue.start()
        self.assertTrue(self.entered.wait(2))
        with self.assertRaises(task_queue.QueueError):
            self.queue.edit("remove", "T02")
        self.queue.edit("remove", "T01")
        self.release.set()
        self.queue.thread.join(3)
        self.assertEqual(self.calls, ["T02"])

    def test_recovery_never_auto_runs_interrupted_task(self):
        self.add()
        state = self.queue.snapshot()
        state["state"] = "running"
        state["items"][0]["status"] = "running"
        self.queue._write(state)
        recovered = task_queue.TaskQueue(self.path, lambda *_: self.fail("不应自动执行"))
        self.assertEqual(recovered.snapshot()["state"], "paused")
        self.assertEqual(recovered.snapshot()["items"][0]["status"], "pending")

    def test_worker_lease_prevents_recovery_of_live_queue(self):
        self.add()
        self.queue.start()
        self.assertTrue(self.entered.wait(2))
        other = task_queue.TaskQueue(self.path, lambda *_: self.fail("不应重复执行"))
        self.assertEqual(other.snapshot()["state"], "running")
        with self.assertRaises(task_queue.QueueError):
            other.start()

    def test_failed_task_blocks_following_tasks(self):
        self.add()
        self.queue.execute = lambda *_: {"ok": False, "message": "fake failure"}
        self.queue.start()
        self.queue.thread.join(3)
        state = self.queue.snapshot()
        self.assertEqual(state["state"], "blocked")
        self.assertEqual([item["status"] for item in state["items"]], ["blocked", "pending"])

    def test_batch_retry_preserves_ids_and_partial_progress(self):
        calls = []

        def build(index):
            calls.append(index)
            if index == 1 and calls.count(1) == 1:
                raise OSError("fake disk error")
            return self.queue.item("T0" + str(index + 1), "task")

        with self.assertRaises(OSError):
            self.queue.batch(2, "request", "hash", build)
        self.assertEqual(len(self.queue.snapshot()["items"]), 1)
        self.queue.batch(2, "request", "hash", build)
        self.queue.batch(2, "request", "hash", build)
        self.assertEqual(calls, [0, 1, 1])
        self.assertEqual(len(self.queue.snapshot()["items"]), 2)
        with self.assertRaises(task_queue.QueueError):
            self.queue.batch(2, "request", "changed", build)


class QueueWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AdvanceTest()
        self.fixture.setUp()
        os.environ["T14_AGENT_SLEEP"] = "0.02"
        self.queue = server._task_queue()
        self.fixture.start_server()

    def tearDown(self):
        self.queue.pause()
        if self.queue.thread:
            self.queue.thread.join(15)
        self.fixture.tearDown()

    def post(self, action, fields=None):
        status, text, _ = self.fixture.request("POST", "/api/queue/" + action, accept="application/json", body=urlencode(fields or {}).encode())
        return status, json.loads(text)

    def wait(self, state):
        wait_until(self, lambda: self.queue.snapshot()["state"] == state)
        if self.queue.thread:
            self.queue.thread.join(3)
        return self.queue.snapshot()

    def test_real_two_task_pipeline_finishes_first_review_before_next_dispatch(self):
        self.fixture.make_task("T81", ["pending"])
        self.fixture.make_task("T82", ["pending"])
        self.assertEqual(self.post("add", {"ids": '["T82", "T81"]'})[0], 200)
        self.assertEqual(self.post("start")[0], 200)
        state = self.wait("completed")
        self.assertTrue(all(item["status"] == "completed" for item in state["items"]))
        lines = self.fixture.order_lines()
        review = next(index for index, line in enumerate(lines) if line.startswith("review end T82"))
        dispatch = next(index for index, line in enumerate(lines) if line.startswith("dispatch start T81"))
        self.assertLess(review, dispatch)
        self.assertFalse(any("overlap" in line for line in lines))
        self.assertFalse(server.RUNNER.lock_info().get("active"))

    def test_batch_defers_registration_and_runs_each_through_review(self):
        fields = {"workdir": str(self.fixture.external_workdir), "work_mode": "isolated",
                  "tasks": json.dumps([{"idea": "first", "title": "first"}, {"idea": "second", "title": "second"}]),
                  "request_id": "queue-batch-test-0001"}
        status, data = self.post("batch", fields)
        self.assertEqual(status, 201, data)
        ids = [item["id"] for item in data["items"]]
        self.assertEqual(self.fixture.order_lines(), [])
        self.assertEqual(server._read_task_meta(server._find_registration_project(ids[0]), ids[0])["status"], server.QUEUED)
        status, page, _ = self.fixture.request("GET", "/registering/" + ids[0])
        self.assertEqual(status, 200)
        self.assertIn("已安排，等待执行", page)
        self.assertEqual(self.post("batch", fields)[1]["items"], data["items"])
        self.assertEqual(self.post("start")[0], 200)
        state = self.wait("completed")
        self.assertEqual(len(state["items"]), 2)
        lines = self.fixture.order_lines()
        first_review = next(index for index, line in enumerate(lines) if line.startswith("review end " + ids[0]))
        second_registration = next(index for index, line in enumerate(lines) if line.startswith("register start " + ids[1]))
        self.assertLess(first_review, second_registration)
        self.assertFalse(any("overlap" in line for line in lines))

    def test_failure_blocks_next_task_then_can_resume(self):
        self.fixture.make_task("T81", ["pending"])
        self.fixture.make_task("T82", ["pending"])
        self.post("add", {"ids": '["T81", "T82"]'})
        os.environ["T14_MODE"] = "fail_dispatch"
        self.post("start")
        state = self.wait("blocked")
        self.assertEqual([item["status"] for item in state["items"]], ["blocked", "pending"])
        self.assertFalse(any("T82" in line for line in self.fixture.order_lines()))
        os.environ["T14_MODE"] = "pass"
        self.assertEqual(self.post("start")[0], 200)
        self.wait("completed")

    def test_active_queue_rejects_manual_start_and_archive_but_accepts_more_plans(self):
        self.fixture.make_task("T81", ["pending"])
        self.fixture.make_task("T82", ["pending"])
        self.post("add", {"ids": '["T81", "T82"]'})
        os.environ["T14_AGENT_SLEEP"] = "0.3"
        self.post("start")
        wait_until(self, lambda: any(item["status"] == "running" for item in self.queue.snapshot()["items"]))
        status, text, _ = self.fixture.request("POST", "/advance/T82", accept="application/json")
        self.assertEqual(status, 409, text)
        with self.assertRaises(server.ActionError):
            server._archive_project("T82")
        with self.assertRaises(server.FormError):
            server._create_published_task({"idea": "should wait", "workdir": str(self.fixture.external_workdir)})
        status, data = self.post("batch", {"tasks": '[{"idea":"more"}]', "request_id": "queue-more-plan-0001", "workdir": str(self.fixture.external_workdir), "work_mode": "isolated"})
        self.assertEqual(status, 201, data)
        self.queue.pause()
        state = self.wait("paused")
        self.assertEqual(state["items"][0]["status"], "completed")
        self.assertEqual(state["items"][1]["status"], "pending")

    def test_batch_validation_is_complete_before_creating_any_task(self):
        status, _ = self.post("batch", {"tasks": '[{"idea":"valid"},{"idea":""}]', "request_id": "queue-invalid-test01", "workdir": str(self.fixture.external_workdir), "work_mode": "project"})
        self.assertEqual(status, 400)
        self.assertEqual(list(self.fixture.projects.iterdir()), [])
        status, _ = self.post("batch", {"tasks": '[{"idea":"valid"}]', "request_id": "queue-invalid-test02", "workdir": str(self.fixture.root), "work_mode": "project"})
        self.assertEqual(status, 400)
        status, data = self.post("batch", {"tasks": '[{"idea":"maintain"}]', "request_id": "queue-self-test-0001", "workdir": str(self.fixture.root), "work_mode": "self"})
        self.assertEqual(status, 201, data)
        task_id = data["items"][0]["id"]
        self.assertEqual(server._read_task_meta(server._find_registration_project(task_id), task_id)["work_mode"], "self")

    def test_registration_launch_error_releases_locks_and_blocks_following_tasks(self):
        self.post("batch", {"tasks": '[{"idea":"first"},{"idea":"second"}]',
                            "request_id": "queue-launch-fail001", "workdir": str(self.fixture.external_workdir), "work_mode": "isolated"})
        with mock.patch.object(server.RUNNER, "launch_registration", side_effect=server.runner_engine.RunnerConfigError("fake missing executable")):
            self.assertEqual(self.post("start")[0], 200)
            state = self.wait("blocked")
        self.assertEqual([item["status"] for item in state["items"]], ["blocked", "pending"])
        self.assertFalse(server.ACTIVE_REGISTRATIONS)
        task_id = state["items"][0]["id"]
        self.assertEqual(server._read_task_meta(server._find_registration_project(task_id), task_id)["status"], server.REGISTER_FAILED)
        self.assertFalse(server.RUNNER.lock_info().get("active"))


if __name__ == "__main__":
    unittest.main()
