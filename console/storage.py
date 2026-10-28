"""Short, reentrant filesystem transactions shared by console processes.

Guard files have stable inodes and must never be removed. The data protected by
them may be replaced atomically, including runner.lock itself.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import fcntl

_locks = {}
_locks_guard = threading.Lock()
_local = threading.local()


@contextmanager
def file_guard(path: Path, timeout: float = 10.0):
    key = str(Path(path).resolve())
    with _locks_guard:
        lock = _locks.setdefault(key, threading.RLock())
    deadline = time.monotonic() + timeout
    if not lock.acquire(timeout=max(0.0, timeout)):
        raise TimeoutError("另一进程正在更新状态，请稍后重试。")
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = {}
    descriptor = None
    try:
        if key not in held:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(key, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("另一进程正在更新状态，请稍后重试。")
                    time.sleep(0.02)
        held[key] = held.get(key, 0) + 1
        try:
            yield
        finally:
            held[key] -= 1
            if not held[key]:
                del held[key]
    finally:
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        lock.release()
