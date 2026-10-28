#!/usr/bin/env python3
"""Exercise production menu navigation with an isolated, delayed fake console."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[1]

with tempfile.TemporaryDirectory(prefix="collab-menu-navigation-") as folder:
    root = Path(folder).resolve()
    (root / "console").mkdir()
    (root / "协作/调度台").mkdir(parents=True)
    (root / "协作/调度台/watch.sh").write_text("#!/bin/bash\nexit 0\n")
    (root / "console/server.py").write_text('''import argparse, http.server, time
parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int)
args = parser.parse_args()
time.sleep(.4)
http.server.HTTPServer(('127.0.0.1', args.port), http.server.BaseHTTPRequestHandler).serve_forever()
''')
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    source = (REPO / "desktop/Sources/main.swift").read_text()
    source = source[:source.rindex("// 入口")].rsplit("// -----------------------------------------------------------------------------", 1)[0]
    source += r'''
extension AppDelegate {
    static func checkNavigationIntegration() -> Int32 {
        _ = NSApplication.shared
        let app = AppDelegate()
        let controller = ConsoleController()
        app.consoleController = controller
        defer { app.pageNavigator.cancel(); controller.shutdownSync() }
        var opened: [URL] = []
        app.openPageURL = { opened.append($0) }
        func spinUntil(_ deadline: Date, _ condition: () -> Bool) {
            while !condition() && Date() < deadline {
                _ = RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
        }
        var checks: [String: Bool] = [:]
        app.openPage(.newTask)
        let pid = controller.snapshot().ownedPID
        checks["启动完成前不打开页面"] = opened.isEmpty && pid != nil
        app.openPage(.runs)
        spinUntil(Date().addingTimeInterval(8)) { !opened.isEmpty }
        checks["连续选择只打开最后的目标"] = opened.map(\.path) == ["/runs"]
        checks["重复点击复用同一个受控服务"] = controller.snapshot().ownedPID == pid
        checks["链接使用隔离测试端口"] = opened.first?.port == Int(RuntimePaths.consolePort)
        app.openPage(.dashboard)
        checks["已就绪时立即打开看板"] = opened.map(\.path) == ["/runs", "/"]
        app.stopConsoleAction()
        spinUntil(Date().addingTimeInterval(4)) { controller.snapshot().ownedPID == nil }
        app.openPage(.newTask)
        app.stopConsoleAction()
        let afterCancel = Date().addingTimeInterval(0.8)
        spinUntil(afterCancel) { false }
        checks["停止取消待打开页面"] = opened.map(\.path) == ["/runs", "/"]
        checks["停止后服务和自动启动均关闭"] = controller.snapshot().ownedPID == nil && !controller.snapshot().wantsToRun
        let report: [String: Any] = ["result": checks.values.allSatisfy { $0 } ? "PASS" : "FAIL", "checks": checks, "port": Int(RuntimePaths.consolePort)]
        let data = try! JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
        print(String(data: data, encoding: .utf8)!)
        return checks.values.allSatisfy { $0 } ? 0 : 1
    }
}
exit(AppDelegate.checkNavigationIntegration())
'''
    swift = root / "main.swift"
    swift.write_text(source)
    binary = root / "check-menu"
    compiled = subprocess.run(
        ["swiftc", "-swift-version", "5", "-framework", "AppKit", "-framework", "Foundation", str(swift), "-o", str(binary)],
        capture_output=True, text=True,
    )
    if compiled.returncode:
        raise SystemExit(compiled.stderr)
    env = os.environ.copy()
    env.update(COLLAB_ROOT=str(root), COLLAB_CONSOLE_PORT=str(port))
    result = subprocess.run([str(binary)], env=env, capture_output=True, text=True, timeout=30)
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="")
    raise SystemExit(result.returncode)
