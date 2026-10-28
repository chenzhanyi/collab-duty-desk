#!/usr/bin/env python3
"""Portable file watcher, immutable incremental snapshots, and legacy helpers."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

ROOT = Path(os.environ.get("COLLAB_ROOT") or Path(__file__).resolve().parents[2]).resolve()
sys.path.insert(0, str(ROOT / "console"))
from policy import max_revise_rounds
from storage import file_guard

DESK = ROOT / "协作" / "调度台"
STATE = DESK / "watch.state"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def snapshot() -> dict:
    files = {}
    for docking in sorted((ROOT / "项目").glob("*/对接")):
        if docking.is_symlink():
            continue
        for directory, dirs, names in os.walk(docking):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink())
            for name in sorted(names):
                path = Path(directory) / name
                if name.startswith(".") or path.is_symlink():
                    continue
                try:
                    digest = hashlib.sha256()
                    with path.open("rb") as handle:
                        stat = os.fstat(handle.fileno())
                        for chunk in iter(lambda: handle.read(65536), b""):
                            digest.update(chunk)
                    files[str(path)] = [stat.st_mtime_ns, stat.st_size, digest.hexdigest()]
                except OSError:
                    continue
    return files


def render(files: dict) -> str:
    return "".join(path + "\t" + "\t".join(map(str, signature)) + "\n" for path, signature in sorted(files.items()))


def snapshots() -> list:
    return sorted(path for path in (DESK / "snapshots").glob("*") if path.is_dir() and not path.name.startswith("."))


def ack() -> None:
    with file_guard(DESK / ".snapshot.guard"):
        files = snapshot()
        previous = snapshots()
        previous = previous[-1] if previous else None
        old_manifest = {}
        if previous is not None:
            try:
                old_manifest = json.loads((previous / ".manifest.json").read_text())
            except (OSError, ValueError):
                pass
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1000000000:09d}"
        target = DESK / "snapshots" / name
        temporary = target.with_name("." + name + ".tmp")
        temporary.mkdir(parents=True)
        manifest = {}
        try:
            for raw, signature in files.items():
                source = Path(raw)
                relative = source.relative_to(ROOT)
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                old = previous / relative if previous else None
                if old is not None and old_manifest.get(str(relative)) == signature and old.is_file():
                    try:
                        os.link(str(old), str(destination))
                    except OSError:
                        shutil.copy2(str(source), str(destination))
                else:
                    shutil.copy2(str(source), str(destination))
                manifest[str(relative)] = signature
            (temporary / ".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            os.replace(str(temporary), str(target))
            # Commit the baseline only once the snapshot is safely available.
            atomic_write(STATE, render(files))
        except Exception:
            # Keep any partial archive for diagnosis; never acknowledge lost data.
            raise
        print(f"ACKED {time.strftime('%F %T')} 共 {len(files)} 个文件")
        print(f"SNAPSHOT {target}")


def watch(seconds: float) -> None:
    DESK.mkdir(parents=True, exist_ok=True)
    if not STATE.exists():
        with file_guard(DESK / ".snapshot.guard"):
            if not STATE.exists():
                atomic_write(STATE, render(snapshot()))
                print("RESULT=INITIALIZED")
                return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        current = snapshot()
        try:
            baseline = STATE.read_text(encoding="utf-8")
        except OSError:
            baseline = ""
        if baseline != render(current):
            print("RESULT=CHANGE")
            old = {parts[0]: parts[1:] for line in baseline.splitlines() if (parts := line.split("\t"))}
            for path in sorted(set(old) | set(current)):
                if path not in old:
                    print("新增\t" + path)
                elif path not in current:
                    print("消失\t" + path)
                elif old[path] != list(map(str, current[path])):
                    print("修改\t" + path)
            return
        time.sleep(min(3.0, max(0.0, deadline - time.monotonic())))
    print(f"RESULT=TIMEOUT_{seconds:g}s")


def since() -> None:
    versions = snapshots()
    if len(versions) < 2:
        print("快照不足两个，无法比对。")
        return
    old, new = versions[-2:]
    print(f"旧快照：{old.name}\n新快照：{new.name}")
    paths = sorted({p.relative_to(old) for p in old.rglob("*") if p.is_file() and p.name != ".manifest.json"}
                   | {p.relative_to(new) for p in new.rglob("*") if p.is_file() and p.name != ".manifest.json"})
    changed = False
    for path in paths:
        left, right = old / path, new / path
        a = left.read_bytes() if left.is_file() else b""
        b = right.read_bytes() if right.is_file() else b""
        if a == b:
            continue
        changed = True
        print("".join(difflib.unified_diff(a.decode("utf-8", "replace").splitlines(True), b.decode("utf-8", "replace").splitlines(True), fromfile=str(left), tofile=str(right))))
    if not changed:
        print("（两次快照完全相同）")


def rounds(action: str, task_id: str) -> int:
    path = DESK / "rounds.tsv"
    if action == "list":
        print(path.read_text() if path.exists() else "(尚无记录)")
        return 0
    task_id = task_id.upper()
    if not re.fullmatch(r"T\d+", task_id):
        raise ValueError("请提供有效任务编号，例如 T01。")
    with file_guard(DESK / ".locks" / f"task-{task_id}.lock"):
        lines = path.read_text().splitlines() if path.exists() else []
        count = 0
        for line in lines:
            parts = line.split("\t")
            if len(parts) > 1 and parts[0].upper() == task_id and parts[1].isdigit():
                count = max(count, int(parts[1]))
        metadata = []
        for project in (ROOT / "项目").glob("*"):
            if project.name.split("_", 1)[0].upper() == task_id:
                meta = project / "对接" / "task.yaml"
                if meta.is_file():
                    text = meta.read_text(encoding="utf-8")
                    match = re.search(r"(?m)^revise_round:\s*['\"]?(\d+)", text)
                    count = max(count, int(match.group(1)) if match else 0)
                    metadata.append((meta, text))
        limit = max_revise_rounds(ROOT)
        if count >= limit:
            print(f"STOP {task_id} 已修正 {count} 轮，达上限 {limit}，须人来定")
            return 1
        if action == "check":
            print(f"OK {task_id} 已用 {count} 轮，仍可自动修正")
            return 0
        count += 1
        lines = [line for line in lines if line.split("\t", 1)[0].upper() != task_id]
        lines.append(f"{task_id}\t{count}\t{time.strftime('%F %T')}")
        atomic_write(path, "\n".join(lines) + "\n")
        for meta, text in metadata:
            pattern = r"(?m)^revise_round:.*$"
            updated = re.sub(pattern, f"revise_round: {count}", text) if re.search(pattern, text) else text.rstrip() + f"\nrevise_round: {count}\n"
            atomic_write(meta, updated)
        print(f"BUMPED {task_id} -> 第 {count} 轮")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ack")
    sub.add_parser("since")
    watcher = sub.add_parser("watch")
    watcher.add_argument("seconds", type=float, nargs="?", default=7200)
    counter = sub.add_parser("rounds")
    counter.add_argument("action", choices=["check", "bump", "list"])
    counter.add_argument("task", nargs="?", default="")
    args = parser.parse_args()
    if args.command == "ack":
        ack()
    elif args.command == "since":
        since()
    elif args.command == "watch":
        if args.seconds <= 0:
            parser.error("观察时长必须大于 0。")
        watch(args.seconds)
    else:
        return rounds(args.action, args.task)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TimeoutError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
