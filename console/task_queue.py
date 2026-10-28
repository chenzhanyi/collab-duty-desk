"""Persistent, serial task queue. Closing the browser never stops its worker."""
from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime
from pathlib import Path

try:
    from .storage import file_guard
except ImportError:
    from storage import file_guard


MAX_ITEMS = 100
ACTIVE = {"running", "pausing"}


class QueueError(ValueError):
    pass


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class TaskQueue:
    def __init__(self, path, execute):
        self.path = Path(path)
        self.execute = execute
        self.thread = None
        self.lock = threading.RLock()
        self.guard = self.path.parent / ".locks" / "task-queue.guard"
        self.worker_guard = self.path.parent / ".locks" / "task-queue-worker.guard"
        # Only recover when no other process owns the worker lease.
        try:
            with file_guard(self.worker_guard, timeout=0.05), self.lock, file_guard(self.guard):
                state = self._read()
                if state["state"] in ACTIVE:
                    state["state"] = "paused"
                    state["message"] = "控制台上次退出时队列尚未结束，确认后可继续执行。"
                    for item in state["items"]:
                        if item["status"] == "running":
                            item.update(status="pending", stage="执行中断", message="重新开始时会检查已有交付，继续未完成的步骤。")
                    self._write(state, order=True)
        except TimeoutError:
            pass

    def _read(self):
        if not self.path.exists():
            return {"state": "ready", "items": [], "revision": 0, "order_revision": 0,
                    "message": "按顺序执行，每张任务审核通过后才开始下一张。", "batches": {}}
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(state, dict) or not isinstance(state.get("items"), list)
                    or len(state["items"]) > MAX_ITEMS
                    or any(not isinstance(item, dict) or not item.get("id")
                           or item.get("status") not in {"pending", "running", "completed", "blocked"}
                           for item in state["items"])):
                raise ValueError("无效的队列结构")
            return state
        except (OSError, ValueError) as exc:
            raise QueueError("队列记录无法读取，原文件已保留，请检查 task-queue.status.json。") from exc

    def _write(self, state, order=False):
        state["revision"] = int(state.get("revision", 0)) + 1
        state["order_revision"] = int(state.get("order_revision", 0)) + int(order)
        state["updated_at"] = now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name("." + self.path.name + ".tmp-%s-%s" % (os.getpid(), threading.get_ident()))
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(self.path))
        finally:
            if temporary.exists():
                temporary.unlink()

    def snapshot(self):
        with self.lock, file_guard(self.guard):
            return copy.deepcopy(self._read())

    @staticmethod
    def item(task_id, title):
        return {"id": task_id, "title": title, "status": "pending", "stage": "等待执行",
                "message": "", "added_at": now(), "started_at": "", "finished_at": ""}

    def append(self, items):
        with self.lock, file_guard(self.guard):
            state = self._read()
            existing = {item["id"] for item in state["items"]}
            fresh = [item for item in items if item["id"] not in existing]
            if len({item["id"] for item in fresh}) != len(fresh):
                raise QueueError("同一任务只能加入队列一次。")
            if len(state["items"]) + len(fresh) > MAX_ITEMS:
                raise QueueError("队列最多保留 100 张任务，请先清理已完成记录。")
            state["items"].extend(copy.deepcopy(fresh))
            if state["state"] == "completed" and fresh:
                state["state"] = "ready"
            self._write(state, order=True)
            return copy.deepcopy(state)

    def batch(self, count, request_id, fingerprint, builder):
        """Record each created task before proceeding; HTTP retries reuse its IDs."""
        with self.lock, file_guard(self.guard):
            state = self._read()
            batches = state.setdefault("batches", {})
            batch = batches.get(request_id)
            if batch and batch["fingerprint"] != fingerprint:
                raise QueueError("这次布置请求已使用，请刷新后重新提交。")
            if batch and batch.get("completed"):
                return copy.deepcopy(state)
            batch = batch or {"fingerprint": fingerprint, "ids": [], "completed": False}
            if len(state["items"]) + count - len(batch["ids"]) > MAX_ITEMS:
                raise QueueError("队列最多保留 100 张任务，请先清理已完成记录。")
            batches[request_id] = batch
            for index in range(len(batch["ids"]), count):
                item = builder(index)
                state["items"].append(item)
                batch["ids"].append(item["id"])
                if state["state"] == "completed":
                    state["state"] = "ready"
                self._write(state, order=True)
            batch["completed"] = True
            while len(batches) > 30:
                del batches[next(iter(batches))]
            self._write(state)
            return copy.deepcopy(state)

    def edit(self, action, task_id="", ids=None, order_revision=None):
        with self.lock, file_guard(self.guard):
            state = self._read()
            if order_revision is not None and int(order_revision) != state.get("order_revision", 0):
                raise QueueError("队列顺序已更新，已显示最新顺序，请再操作一次。")
            if action == "reorder":
                movable = [item for item in state["items"] if item["status"] in {"pending", "blocked"}]
                if not isinstance(ids, list) or len(ids) != len(set(ids)) or set(ids) != {item["id"] for item in movable}:
                    raise QueueError("只能调整待处理任务的顺序，请刷新队列后重试。")
                lookup = {item["id"]: item for item in movable}
                sequence = iter(ids)
                state["items"] = [lookup[next(sequence)] if item in movable else item for item in state["items"]]
            elif action == "remove":
                item = next((item for item in state["items"] if item["id"] == task_id), None)
                if item is None:
                    raise QueueError("这个任务已不在队列中。")
                if item["status"] == "running":
                    raise QueueError("正在处理的任务不能移出；可先在当前任务结束后暂停。")
                state["items"].remove(item)
            elif action == "clear_completed":
                state["items"] = [item for item in state["items"] if item["status"] != "completed"]
            else:
                raise QueueError("未知的队列操作。")
            self._write(state, order=True)
            return copy.deepcopy(state)

    def start(self):
        with self.lock, file_guard(self.guard):
            state = self._read()
            if state["state"] in ACTIVE or (self.thread and self.thread.is_alive()):
                raise QueueError("队列正在执行，请等待或暂停后再继续。")
            if not any(item["status"] != "completed" for item in state["items"]):
                raise QueueError("先添加至少一张待处理任务。")
            for item in state["items"]:
                if item["status"] == "blocked":
                    item.update(status="pending", stage="等待重试", message="")
            state.update(state="running", message="已开始按顺序执行。")
            self._write(state, order=True)
            self.thread = threading.Thread(target=self._work, name="task-queue", daemon=True)
            try:
                self.thread.start()
            except RuntimeError:
                state.update(state="paused", message="执行线程未能启动，可以重试。")
                self._write(state)
                raise
            return copy.deepcopy(state)

    def pause(self):
        with self.lock, file_guard(self.guard):
            state = self._read()
            if state["state"] == "running":
                state.update(state="pausing", message="当前任务结束后暂停，后续任务不会开始。")
                self._write(state)
            return copy.deepcopy(state)

    def _progress(self, task_id, stage, message=""):
        with self.lock, file_guard(self.guard):
            state = self._read()
            item = next(item for item in state["items"] if item["id"] == task_id)
            if (item.get("stage"), item.get("message")) != (stage, message):
                item.update(stage=stage, message=message[:2000])
                self._write(state)

    def _work(self):
        try:
            with file_guard(self.worker_guard, timeout=0.1):
                while True:
                    with self.lock, file_guard(self.guard):
                        state = self._read()
                        if state["state"] == "pausing":
                            remaining = any(item["status"] != "completed" for item in state["items"])
                            state.update(state="paused" if remaining else "completed",
                                         message="队列已暂停，待处理顺序已保留。" if remaining else "本次队列全部完成。")
                            self._write(state)
                            return
                        if state["state"] != "running":
                            return
                        item = next((item for item in state["items"] if item["status"] != "completed"), None)
                        if item is None:
                            state.update(state="completed", message="本次队列全部完成。")
                            self._write(state)
                            return
                        item.update(status="running", stage="准备执行", started_at=now(), message="")
                        task_id = item["id"]
                        self._write(state, order=True)
                    try:
                        result = self.execute(task_id, lambda stage, message="": self._progress(task_id, stage, message))
                    except Exception as exc:
                        result = {"ok": False, "message": "执行未完成：" + str(exc)}
                    with self.lock, file_guard(self.guard):
                        state = self._read()
                        item = next(item for item in state["items"] if item["id"] == task_id)
                        if result.get("ok"):
                            item.update(status="completed", stage="审核通过", message=result.get("message", ""), finished_at=now())
                            self._write(state, order=True)
                        else:
                            item.update(status="blocked", stage="需要处理", message=str(result.get("message") or "任务未通过审核，后续任务已暂停。"), finished_at=now())
                            state.update(state="blocked", message=task_id + " 需要处理，后续任务尚未开始。")
                            self._write(state, order=True)
                            return
        except Exception as exc:
            with self.lock, file_guard(self.guard):
                state = self._read()
                state.update(state="blocked", message="队列未能继续：" + str(exc))
                for item in state["items"]:
                    if item["status"] == "running":
                        item.update(status="blocked", stage="需要处理", message=str(exc))
                self._write(state, order=True)
