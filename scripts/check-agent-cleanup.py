#!/usr/bin/env python3
"""Compile the production controller and prove stopping cleans a TERM-resistant Agent."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder).resolve()
    console = root / 'fake-console.py'
    console.write_text('''import signal, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[0].rsplit('/', 1)[0])
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, REPO_CONSOLE)
import runner
task = ROOT / '项目' / 'T90_test'
(task / '对接').mkdir(parents=True)
(task / '对接' / '任务单.md').write_text('test')
agent = ROOT / 'fake-agent.py'
agent.write_text("import os, signal, time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(__file__).with_name('agent.pid').write_text(str(os.getpid())); time.sleep(30)")
engine = runner.Runner(ROOT)
signal.signal(signal.SIGTERM, lambda s, f: (_ for _ in ()).throw(KeyboardInterrupt()))
try:
    engine.launch(task_id='T90', task_dir=task, agent=runner.AgentSpec(id='fake', name='fake', command=sys.executable, args_template=str(agent) + ' "{prompt}"'), round_no=1)
    while True:
        time.sleep(.1)
except KeyboardInterrupt:
    engine.shutdown()
'''.replace('REPO_CONSOLE', repr(str(REPO / 'console'))))
    swift_source = (REPO / 'desktop' / 'Sources' / 'main.swift').read_text()
    swift_source = swift_source.split('// MARK: - 看护循环自检')[0] if '// MARK: - 看护循环自检' in swift_source else swift_source
    swift_source = swift_source[:swift_source.index('enum WatchdogSelfTest')]
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    listener.close()
    # Sources before WatchdogSelfTest contain the unchanged production ConsoleController.
    swift_source += r'''
let folder = URL(fileURLWithPath: CommandLine.arguments[1])
let controller = ConsoleController(port: UInt16(CommandLine.arguments[2])!, consoleScript: folder.appendingPathComponent("fake-console.py"), consoleLogURL: folder.appendingPathComponent("console.log"), projectRoot: folder)
controller.start(manual: true)
let ready = folder.appendingPathComponent("agent.pid")
let deadline = Date().addingTimeInterval(10)
while !FileManager.default.fileExists(atPath: ready.path) && Date() < deadline {
    _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
}
let pid = Int32((try String(contentsOf: ready)).trimmingCharacters(in: .whitespacesAndNewlines))!
controller.stop()
let stoppedDeadline = Date().addingTimeInterval(14)
while controller.snapshot().ownedPID != nil && Date() < stoppedDeadline {
    _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.05))
}
let orphanAlive = kill(pid, 0) == 0
print("CONSOLE_RUNNING=\(controller.snapshot().ownedPID != nil)")
print("AGENT_ALIVE_AFTER_STOP=\(orphanAlive)")
kill(-pid, SIGKILL)
controller.shutdownSync()
'''
    source = root / 'main.swift'
    source.write_text(swift_source)
    binary = root / 'audit-controller'
    subprocess.run(['swiftc', '-swift-version', '5', '-framework', 'AppKit', '-framework', 'Foundation', str(source), '-o', str(binary)], check=True, capture_output=True)
    result = subprocess.run([str(binary), str(root), str(port)], capture_output=True, text=True, timeout=30)
    print(json.dumps({'returncode': result.returncode, 'output': result.stdout, 'stderr': result.stderr, 'controller_log': (root / 'console.log').read_text()}, ensure_ascii=False, indent=2))
    if (root / 'agent.pid').exists():
        try:
            os.killpg(int((root / 'agent.pid').read_text()), 9)
        except ProcessLookupError:
            pass
    assert result.returncode == 0
    assert 'AGENT_ALIVE_AFTER_STOP=false' in result.stdout
    assert 'CONSOLE_RUNNING=false' in result.stdout
