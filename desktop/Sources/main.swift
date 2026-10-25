import AppKit
import Darwin
import Foundation

// -----------------------------------------------------------------------------
// 路径与常量
// -----------------------------------------------------------------------------

enum RuntimePaths {
    static let consolePort: UInt16 = 8787
    static let watchSeconds = 7200
    static let guardRestartDelay: TimeInterval = 3

    /// 项目根目录：含 console/server.py 与 协作/调度台/watch.sh 的最近祖先目录。
    /// 可通过 COLLAB_ROOT 覆盖（便于隔离测试），Finder 双击时自动从 .app 位置向上查找。
    static let locatedRoot: URL? = locateProjectRoot()
    static var hasValidRoot: Bool { locatedRoot != nil }
    static var projectRoot: URL {
        locatedRoot ?? URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
    }

    static var consoleScript: URL { projectRoot.appendingPathComponent("console/server.py") }
    static var consoleLog: URL { projectRoot.appendingPathComponent("协作/调度台/console.log") }
    static var dispatchDir: URL { projectRoot.appendingPathComponent("协作/调度台", isDirectory: true) }
    static var watchScript: URL { dispatchDir.appendingPathComponent("watch.sh") }
    static var ackScript: URL { dispatchDir.appendingPathComponent("ack.sh") }
    static var journalLog: URL { dispatchDir.appendingPathComponent("journal.log") }
    static var guardStatus: URL { dispatchDir.appendingPathComponent("guard.status") }
    static var controlToken: URL { dispatchDir.appendingPathComponent("menubar.control-token") }

    static var runnerScript: URL {
        if let bundled = Bundle.main.url(forResource: "runner", withExtension: "sh") {
            return bundled
        }
        return projectRoot.appendingPathComponent("desktop/runner.sh")
    }

    private static func locateProjectRoot() -> URL? {
        let fm = FileManager.default
        var starts: [URL] = []

        if let override = ProcessInfo.processInfo.environment["COLLAB_ROOT"], !override.isEmpty {
            starts.append(URL(fileURLWithPath: override, isDirectory: true))
        }
        starts.append(Bundle.main.bundleURL)
        if let resources = Bundle.main.resourceURL { starts.append(resources) }
        if let executable = Bundle.main.executableURL { starts.append(executable) }
        starts.append(URL(fileURLWithPath: fm.currentDirectoryPath, isDirectory: true))

        var seen = Set<String>()
        for start in starts {
            var candidate = start.standardizedFileURL
            for _ in 0..<12 {
                let key = candidate.path
                if seen.insert(key).inserted, isProjectRoot(candidate) {
                    return candidate
                }
                let parent = candidate.deletingLastPathComponent()
                if parent.path == candidate.path { break }
                candidate = parent
            }
        }
        return nil
    }

    private static func isProjectRoot(_ candidate: URL) -> Bool {
        let fm = FileManager.default
        return fm.fileExists(atPath: candidate.appendingPathComponent("console/server.py").path)
            && fm.fileExists(atPath: candidate.appendingPathComponent("协作/调度台/watch.sh").path)
    }
}

// -----------------------------------------------------------------------------
// 小工具：时间、日志、状态文件、进程启动
// -----------------------------------------------------------------------------

enum Clock {
    static func display(_ date: Date = Date()) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone.current
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter.string(from: date)
    }

    static func epoch(_ date: Date = Date()) -> Int {
        Int(date.timeIntervalSince1970)
    }
}

enum FileIO {
    private static let appendQueue = DispatchQueue(label: "collab.menubar.file-append")

    static func append(_ text: String, to url: URL) {
        appendQueue.sync {
            _ = appendDirect(text, to: url)
        }
    }

    @discardableResult
    static func appendDirect(_ text: String, to url: URL) -> Bool {
        let fm = FileManager.default
        do {
            try fm.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
        } catch {
            return false
        }

        let fd = open(url.path, O_WRONLY | O_CREAT | O_APPEND, 0o644)
        guard fd >= 0 else { return false }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? handle.close() }
        do {
            try handle.write(contentsOf: Data(text.utf8))
            return true
        } catch {
            return false
        }
    }

    /// 用 O_APPEND 打开，进程继承后与其它追加者也不会互相覆盖。
    static func openAppendHandle(_ url: URL) -> FileHandle? {
        do {
            try FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
        } catch {
            return nil
        }
        let fd = open(url.path, O_WRONLY | O_CREAT | O_APPEND, 0o644)
        guard fd >= 0 else { return nil }
        return FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    }
}

enum ConsoleTextLog {
    static func line(_ message: String) {
        FileIO.append("[菜单栏] \(Clock.display()) \(message)\n", to: RuntimePaths.consoleLog)
    }
}

enum GuardJournal {
    static func event(_ title: String, output: String? = nil) {
        var text = "\n[菜单栏控制器] \(Clock.display()) \(title)\n"
        if let output {
            let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                text += trimmed + "\n"
            }
        }
        FileIO.append(text, to: RuntimePaths.journalLog)
    }
}

enum GuardStatusFile {
    private static func clean(_ value: String) -> String {
        value
            .replacingOccurrences(of: "\n", with: " ")
            .replacingOccurrences(of: "\r", with: " ")
    }

    static func write(state: String, note: String, watchPID: pid_t? = nil) {
        let now = Date()
        let pidValue = watchPID.map(String.init) ?? "-"
        let body = """
        state=\(clean(state))
        updated=\(Clock.display(now))
        updated_epoch=\(Clock.epoch(now))
        app_pid=\(getpid())
        watch_pid=\(pidValue)
        note=\(clean(note))

        """
        do {
            try FileManager.default.createDirectory(
                at: RuntimePaths.dispatchDir,
                withIntermediateDirectories: true
            )
            try Data(body.utf8).write(to: RuntimePaths.guardStatus, options: .atomic)
        } catch {
            ConsoleTextLog.line("写入 guard.status 失败：\(error.localizedDescription)")
        }
    }
}

enum PortProbe {
    /// 不发起 HTTP 请求，只检查 127.0.0.1:8787 是否在监听。
    static func isOpen(_ port: UInt16) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }

        var noSigPipe: Int32 = 1
        _ = setsockopt(
            fd,
            SOL_SOCKET,
            SO_NOSIGPIPE,
            &noSigPipe,
            socklen_t(MemoryLayout<Int32>.size)
        )

        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))

        let oldFlags = fcntl(fd, F_GETFL, 0)
        _ = fcntl(fd, F_SETFL, oldFlags | O_NONBLOCK)

        let result = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockaddrPointer in
                Darwin.connect(fd, sockaddrPointer, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if result == 0 { return true }
        guard errno == EINPROGRESS else { return false }

        var descriptor = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
        let ready = Darwin.poll(&descriptor, 1, 200)
        guard ready > 0 else { return false }

        var socketError: Int32 = 0
        var length = socklen_t(MemoryLayout<Int32>.size)
        let ok = getsockopt(fd, SOL_SOCKET, SO_ERROR, &socketError, &length)
        return ok == 0 && socketError == 0
    }
}

struct CapturedProcess {
    let process: Process
    let readHandle: FileHandle
}

enum ProcessLauncher {
    static var environment: [String: String] {
        var env = ProcessInfo.processInfo.environment
        let basePath = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
        if let oldPath = env["PATH"], !oldPath.isEmpty {
            env["PATH"] = basePath + ":" + oldPath
        } else {
            env["PATH"] = basePath
        }
        env["PYTHONUNBUFFERED"] = "1"
        if env["LANG"] == nil {
            env["LANG"] = "en_US.UTF-8"
        }
        return env
    }

    static func make(
        command: [String],
        stdout: Any?,
        stderr: Any?
    ) -> Process {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [RuntimePaths.runnerScript.path] + command
        process.currentDirectoryURL = RuntimePaths.projectRoot
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdout ?? FileHandle.nullDevice
        process.standardError = stderr ?? FileHandle.nullDevice
        return process
    }

    /// 控制台是单进程 Python 服务，直接启动，PID 即真实服务 PID。
    static func makeDirect(
        command: [String],
        stdout: Any?,
        stderr: Any?
    ) -> Process {
        let process = Process()
        if let executable = command.first {
            process.executableURL = URL(fileURLWithPath: executable)
            process.arguments = Array(command.dropFirst())
        }
        process.currentDirectoryURL = RuntimePaths.projectRoot
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdout ?? FileHandle.nullDevice
        process.standardError = stderr ?? FileHandle.nullDevice
        return process
    }

    static func launchCaptured(command: [String]) throws -> CapturedProcess {
        let pipe = Pipe()
        let process = make(command: command, stdout: pipe, stderr: pipe)
        do {
            try process.run()
        } catch {
            try? pipe.fileHandleForReading.close()
            try? pipe.fileHandleForWriting.close()
            throw error
        }
        // 父进程关闭写端，子进程退出后读端才能收到 EOF。
        try? pipe.fileHandleForWriting.close()
        return CapturedProcess(process: process, readHandle: pipe.fileHandleForReading)
    }
}

enum ProcessKiller {
    /// 先 TERM；宽限期后仍存活则 KILL。runner.sh 会把自己的子进程组一并收走。
    static func terminate(_ process: Process?, grace: TimeInterval = 1.5) {
        guard let process, process.isRunning else { return }
        let pid = process.processIdentifier
        process.terminate()
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + grace) {
            if process.isRunning {
                kill(pid, SIGKILL)
            }
        }
    }

    /// 退出应用时使用：等待一小段时间，必要时强制杀掉。
    static func terminateAndWait(_ process: Process?, grace: TimeInterval = 2.0) {
        guard let process, process.isRunning else { return }
        let pid = process.processIdentifier
        process.terminate()
        let deadline = Date().addingTimeInterval(grace)
        while process.isRunning && Date() < deadline {
            usleep(50_000)
        }
        if process.isRunning {
            kill(pid, SIGKILL)
            usleep(100_000)
        }
    }
}

// -----------------------------------------------------------------------------
// 控制台进程
// -----------------------------------------------------------------------------

final class ConsoleController {
    struct Snapshot {
        let ownedPID: pid_t?
        let portOpen: Bool
        let restarting: Bool

        var running: Bool { ownedPID != nil || portOpen }
        var external: Bool { portOpen && ownedPID == nil }
    }

    private var process: Process?
    private var outputHandle: FileHandle?
    private var restartWork: DispatchWorkItem?
    private var restartAttempt = 0
    private var wantsToRun = false
    private var stopping = false
    private var restartAfterTermination = false
    private var shuttingDown = false
    private var launchedAt: Date?

    var onChange: (() -> Void)?

    func snapshot() -> Snapshot {
        let alive = process?.isRunning == true
        let ownedPID = alive ? process?.processIdentifier : nil
        let portOpen = PortProbe.isOpen(RuntimePaths.consolePort)
        return Snapshot(ownedPID: ownedPID, portOpen: portOpen, restarting: restartWork != nil)
    }

    func start(manual: Bool) {
        guard !shuttingDown else { return }
        if process?.isRunning == true {
            ConsoleTextLog.line("控制台已在运行，忽略重复启动。")
            return
        }
        if PortProbe.isOpen(RuntimePaths.consolePort) {
            ConsoleTextLog.line("端口 \(RuntimePaths.consolePort) 已被其他进程占用，未启动新控制台。")
            onChange?()
            return
        }

        restartWork?.cancel()
        restartWork = nil
        if manual {
            restartAttempt = 0
        }
        wantsToRun = true
        do {
            try spawn()
        } catch {
            ConsoleTextLog.line("控制台启动失败：\(error.localizedDescription)")
            onChange?()
            if manual {
                wantsToRun = false
            } else if wantsToRun {
                scheduleRestart()
            }
        }
    }

    private func spawn() throws {
        guard FileManager.default.fileExists(atPath: RuntimePaths.consoleScript.path) else {
            throw NSError(
                domain: "CollabMenuBar",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "找不到 console/server.py"]
            )
        }
        guard let logHandle = FileIO.openAppendHandle(RuntimePaths.consoleLog) else {
            throw NSError(
                domain: "CollabMenuBar",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "无法打开 console.log"]
            )
        }

        let newProcess = ProcessLauncher.makeDirect(
            command: ["/usr/bin/env", "python3", RuntimePaths.consoleScript.path],
            stdout: logHandle,
            stderr: logHandle
        )
        newProcess.terminationHandler = { [weak self] exited in
            DispatchQueue.main.async {
                self?.handleTermination(exited)
            }
        }

        do {
            try newProcess.run()
        } catch {
            try? logHandle.close()
            throw error
        }

        process = newProcess
        outputHandle = logHandle
        stopping = false
        launchedAt = Date()
        ConsoleTextLog.line("启动控制台（PID \(newProcess.processIdentifier)）：python3 console/server.py")
        onChange?()
    }

    private func handleTermination(_ exited: Process) {
        guard exited === process else { return }
        let pid = exited.processIdentifier
        let uptime = launchedAt.map { Date().timeIntervalSince($0) } ?? 0
        let reason = terminationDescription(exited)

        process = nil
        try? outputHandle?.close()
        outputHandle = nil
        launchedAt = nil
        let wasStopping = stopping
        stopping = false

        if shuttingDown { return }

        if restartAfterTermination {
            restartAfterTermination = false
            wantsToRun = true
            ConsoleTextLog.line("控制台 \(pid) 已停止，执行重启。")
            start(manual: true)
            return
        }

        if wasStopping || !wantsToRun {
            wantsToRun = false
            ConsoleTextLog.line("控制台已停止（PID \(pid)，\(reason)）。")
            onChange?()
            return
        }

        if uptime >= 20 {
            restartAttempt = 0
        }
        ConsoleTextLog.line("控制台意外退出（PID \(pid)，\(reason)，运行 \(Int(uptime)) 秒）。")
        scheduleRestart()
        onChange?()
    }

    private func terminationDescription(_ process: Process) -> String {
        switch process.terminationReason {
        case .exit:
            return "退出码 \(process.terminationStatus)"
        case .uncaughtSignal:
            return "信号 \(process.terminationStatus)"
        @unknown default:
            return "状态 \(process.terminationStatus)"
        }
    }

    private func scheduleRestart() {
        restartWork?.cancel()
        if PortProbe.isOpen(RuntimePaths.consolePort) {
            wantsToRun = false
            ConsoleTextLog.line("端口已被其他进程占用，取消控制台自动重启。")
            onChange?()
            return
        }

        restartAttempt += 1
        let delays: [TimeInterval] = [1, 2, 4, 8, 16, 30]
        let delay = delays[min(restartAttempt - 1, delays.count - 1)]
        ConsoleTextLog.line("将在 \(Int(delay)) 秒后自动重启控制台（退避第 \(restartAttempt) 档）。")

        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.restartWork = nil
            guard !self.shuttingDown, self.wantsToRun, self.process?.isRunning != true else {
                return
            }
            if PortProbe.isOpen(RuntimePaths.consolePort) {
                self.wantsToRun = false
                ConsoleTextLog.line("端口已被占用，放弃本次自动重启。")
                self.onChange?()
                return
            }
            self.start(manual: false)
        }
        restartWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
        onChange?()
    }

    func stop() {
        guard !shuttingDown else { return }
        wantsToRun = false
        restartAfterTermination = false
        restartWork?.cancel()
        restartWork = nil

        if let process, process.isRunning {
            stopping = true
            let pid = process.processIdentifier
            ConsoleTextLog.line("正在停止控制台（PID \(pid)）。")
            process.terminate()
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self, weak process] in
                guard
                    let self,
                    let process,
                    self.process === process,
                    process.isRunning
                else { return }
                kill(process.processIdentifier, SIGKILL)
                ConsoleTextLog.line("控制台未在 2 秒内退出，已强制结束（PID \(pid)）。")
            }
            onChange?()
            return
        }

        if PortProbe.isOpen(RuntimePaths.consolePort) {
            ConsoleTextLog.line("8787 由外部进程监听；它不由本应用启动，应用不会终止它。")
        } else {
            ConsoleTextLog.line("控制台已经停止。")
        }
        onChange?()
    }

    func restart() {
        guard !shuttingDown else { return }
        if let process, process.isRunning {
            wantsToRun = true
            restartAfterTermination = true
            restartWork?.cancel()
            restartWork = nil
            let pid = process.processIdentifier
            ConsoleTextLog.line("重启控制台（PID \(pid)）。")
            process.terminate()
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak process] in
                if let process, process.isRunning {
                    kill(process.processIdentifier, SIGKILL)
                }
            }
            onChange?()
            return
        }
        if PortProbe.isOpen(RuntimePaths.consolePort) {
            ConsoleTextLog.line("8787 由外部进程监听，不能由本应用重启。")
            return
        }
        start(manual: true)
    }

    func shutdownSync() {
        shuttingDown = true
        wantsToRun = false
        restartAfterTermination = false
        restartWork?.cancel()
        restartWork = nil

        if let process, process.isRunning {
            ConsoleTextLog.line("菜单栏应用退出，停止受控控制台（PID \(process.processIdentifier)）。")
            ProcessKiller.terminateAndWait(process)
        }
        try? outputHandle?.close()
        outputHandle = nil
        process = nil
        launchedAt = nil
    }
}

// -----------------------------------------------------------------------------
// 常驻守卫
// -----------------------------------------------------------------------------

final class GuardController {
    enum Phase {
        case idle
        case watching
        case acking
        case waiting
    }

    private(set) var enabled = false
    private(set) var phase: Phase = .idle

    private var watchProcess: Process?
    private var ackProcess: Process?
    private var pendingRestart: DispatchWorkItem?
    private var generation = 0
    private var shuttingDown = false
    private var watchPID: pid_t?

    var onChange: (() -> Void)?

    func start(reason: String) {
        guard !enabled, !shuttingDown else { return }
        guard FileManager.default.fileExists(atPath: RuntimePaths.watchScript.path) else {
            GuardJournal.event("启动守卫失败：找不到 协作/调度台/watch.sh")
            GuardStatusFile.write(state: "stopped", note: "启动失败：找不到 watch.sh")
            onChange?()
            return
        }
        guard FileManager.default.fileExists(atPath: RuntimePaths.ackScript.path) else {
            GuardJournal.event("启动守卫失败：找不到 协作/调度台/ack.sh")
            GuardStatusFile.write(state: "stopped", note: "启动失败：找不到 ack.sh")
            onChange?()
            return
        }

        enabled = true
        generation += 1
        phase = .waiting
        pendingRestart?.cancel()
        pendingRestart = nil
        GuardStatusFile.write(state: "running", note: "常驻中 · 正在启动守卫")
        GuardJournal.event("启动常驻守卫（\(reason)）：bash 协作/调度台/watch.sh \(RuntimePaths.watchSeconds)")
        onChange?()
        startWatch()
    }

    private func startWatch() {
        guard enabled, !shuttingDown else { return }
        guard watchProcess == nil, ackProcess == nil else { return }
        pendingRestart?.cancel()
        pendingRestart = nil
        phase = .watching

        do {
            let captured = try ProcessLauncher.launchCaptured(
                command: [
                    "/bin/bash",
                    RuntimePaths.watchScript.path,
                    String(RuntimePaths.watchSeconds),
                ]
            )
            watchProcess = captured.process
            watchPID = captured.process.processIdentifier
            let launchedGeneration = generation
            GuardStatusFile.write(
                state: "running",
                note: "常驻中 · 正在观察 项目/*/对接/",
                watchPID: watchPID
            )
            onChange?()

            let readHandle = captured.readHandle
            let exitedProcess = captured.process
            DispatchQueue.global(qos: .utility).async { [weak self] in
                let data = readHandle.readDataToEndOfFile()
                try? readHandle.close()
                exitedProcess.waitUntilExit()
                let output = String(data: data, encoding: .utf8) ?? ""
                let exitCode = exitedProcess.terminationStatus
                let reason = exitedProcess.terminationReason
                DispatchQueue.main.async {
                    self?.handleWatchEnd(
                        process: exitedProcess,
                        generation: launchedGeneration,
                        exitCode: exitCode,
                        reason: reason,
                        output: output
                    )
                }
            }
        } catch {
            GuardJournal.event("守卫启动失败：\(error.localizedDescription)")
            GuardStatusFile.write(state: "stopped", note: "启动失败：\(error.localizedDescription)")
            enabled = false
            phase = .idle
            onChange?()
        }
    }

    private func handleWatchEnd(
        process: Process,
        generation launchedGeneration: Int,
        exitCode: Int32,
        reason: Process.TerminationReason,
        output: String
    ) {
        guard
            launchedGeneration == generation,
            enabled,
            !shuttingDown,
            watchProcess === process
        else { return }

        watchProcess = nil
        watchPID = nil
        let exitDescription = terminationDescription(code: exitCode, reason: reason)
        let trimmed = output.trimmingCharacters(in: .whitespacesAndNewlines)

        if trimmed.contains("RESULT=CHANGE") {
            phase = .waiting
            GuardJournal.event("守卫检测到变化，退出码 \(exitCode)，准备刷新基线", output: trimmed)
            GuardStatusFile.write(state: "running", note: "常驻中 · 检测到变化，正在刷新基线")
            onChange?()
            runAck(generation: launchedGeneration)
            return
        }

        if trimmed.contains("RESULT=TIMEOUT_") {
            phase = .waiting
            GuardJournal.event(
                "守卫超时，3 秒后重挂（退出码 \(exitCode)）",
                output: trimmed
            )
            GuardStatusFile.write(state: "running", note: "常驻中 · 超时，3 秒后重挂")
            scheduleRestart(after: RuntimePaths.guardRestartDelay, generation: launchedGeneration)
            return
        }

        if trimmed.contains("RESULT=INITIALIZED") {
            GuardStatusFile.write(state: "running", note: "常驻中 · watch.state 基线已初始化")
            phase = .waiting
            scheduleRestart(after: 0.2, generation: launchedGeneration)
            return
        }

        phase = .waiting
        GuardJournal.event(
            "守卫异常退出（\(exitDescription)），3 秒后重挂",
            output: trimmed
        )
        GuardStatusFile.write(state: "running", note: "常驻中 · 异常退出，3 秒后重挂")
        scheduleRestart(after: RuntimePaths.guardRestartDelay, generation: launchedGeneration)
    }

    private func terminationDescription(code: Int32, reason: Process.TerminationReason) -> String {
        switch reason {
        case .exit:
            return "退出码 \(code)"
        case .uncaughtSignal:
            return "信号 \(code)"
        @unknown default:
            return "状态 \(code)"
        }
    }

    private func runAck(generation ackGeneration: Int) {
        guard enabled, !shuttingDown, ackGeneration == generation else { return }
        phase = .acking

        do {
            let captured = try ProcessLauncher.launchCaptured(
                command: ["/bin/bash", RuntimePaths.ackScript.path]
            )
            ackProcess = captured.process
            let readHandle = captured.readHandle
            let exitedProcess = captured.process
            DispatchQueue.global(qos: .utility).async { [weak self] in
                let data = readHandle.readDataToEndOfFile()
                try? readHandle.close()
                exitedProcess.waitUntilExit()
                let output = String(data: data, encoding: .utf8) ?? ""
                let exitCode = exitedProcess.terminationStatus
                DispatchQueue.main.async {
                    self?.handleAckEnd(
                        process: exitedProcess,
                        generation: ackGeneration,
                        exitCode: exitCode,
                        output: output
                    )
                }
            }
        } catch {
            GuardJournal.event(
                "自动刷新 watch.state 基线失败，3 秒后重挂：\(error.localizedDescription)"
            )
            GuardStatusFile.write(state: "running", note: "常驻中 · 刷新基线失败，3 秒后重挂")
            scheduleRestart(after: RuntimePaths.guardRestartDelay, generation: ackGeneration)
        }
    }

    private func handleAckEnd(
        process: Process,
        generation ackGeneration: Int,
        exitCode: Int32,
        output: String
    ) {
        guard
            enabled,
            !shuttingDown,
            ackGeneration == generation,
            ackProcess === process
        else { return }
        ackProcess = nil

        if exitCode == 0 {
            GuardJournal.event("watch.state 基线已刷新，立即重挂守卫", output: output)
            GuardStatusFile.write(state: "running", note: "常驻中 · 基线已刷新，正在重挂")
            startWatch()
        } else {
            GuardJournal.event(
                "自动刷新 watch.state 失败（退出码 \(exitCode)），3 秒后重挂",
                output: output
            )
            GuardStatusFile.write(state: "running", note: "常驻中 · 刷新基线失败，3 秒后重挂")
            scheduleRestart(after: RuntimePaths.guardRestartDelay, generation: ackGeneration)
        }
    }

    private func scheduleRestart(after delay: TimeInterval, generation scheduledGeneration: Int) {
        guard enabled, !shuttingDown, scheduledGeneration == generation else { return }
        phase = .waiting
        pendingRestart?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.pendingRestart = nil
            guard
                !self.shuttingDown,
                self.enabled,
                scheduledGeneration == self.generation
            else { return }
            self.startWatch()
        }
        pendingRestart = work
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
        onChange?()
    }

    func stop(reason: String = "菜单操作") {
        guard enabled else { return }
        let stoppedWatch = watchProcess
        let stoppedAck = ackProcess
        enabled = false
        generation += 1
        phase = .idle
        pendingRestart?.cancel()
        pendingRestart = nil
        watchProcess = nil
        ackProcess = nil
        watchPID = nil

        ProcessKiller.terminate(stoppedWatch)
        ProcessKiller.terminate(stoppedAck)

        GuardStatusFile.write(state: "stopped", note: "已停止（\(reason)）")
        GuardJournal.event("停止常驻守卫（\(reason)）")
        onChange?()
    }

    func shutdownSync() {
        let wasEnabled = enabled
        shuttingDown = true
        enabled = false
        generation += 1
        phase = .idle
        pendingRestart?.cancel()
        pendingRestart = nil

        let stoppedWatch = watchProcess
        let stoppedAck = ackProcess
        watchProcess = nil
        ackProcess = nil
        watchPID = nil

        ProcessKiller.terminateAndWait(stoppedWatch, grace: 1.5)
        ProcessKiller.terminateAndWait(stoppedAck, grace: 1.5)

        GuardStatusFile.write(state: "stopped", note: "已停止（菜单栏应用退出）")
        if wasEnabled {
            GuardJournal.event("菜单栏应用退出，停止常驻守卫")
        }
    }
}

// -----------------------------------------------------------------------------
// AppKit 菜单栏
// -----------------------------------------------------------------------------

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private var consoleController: ConsoleController!
    private var guardController: GuardController!
    private var refreshTimer: Timer?
    private var signalSources: [DispatchSourceSignal] = []
    private var isTerminating = false
    private var pendingControlURLs: [URL] = []
    private var controlTokenValue = ""

    private var consoleStatusItem: NSMenuItem!
    private var startConsoleItem: NSMenuItem!
    private var stopConsoleItem: NSMenuItem!
    private var restartConsoleItem: NSMenuItem!
    private var openWebItem: NSMenuItem!
    private var guardStatusItem: NSMenuItem!
    private var startGuardItem: NSMenuItem!
    private var stopGuardItem: NSMenuItem!
    private var openProjectItem: NSMenuItem!
    private var openLogItem: NSMenuItem!
    private var quitItem: NSMenuItem!

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        installSignalHandlers()

        guard RuntimePaths.hasValidRoot else {
            let alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = "找不到协作项目根目录"
            alert.informativeText = """
            请把 CollabMenuBar.app 放在项目的 desktop/ 目录内再启动，\
            或用环境变量 COLLAB_ROOT 指向包含 console/server.py 与 协作/调度台/watch.sh 的目录。
            """
            alert.runModal()
            NSApp.terminate(nil)
            return
        }

        consoleController = ConsoleController()
        guardController = GuardController()
        consoleController.onChange = { [weak self] in self?.refreshUI() }
        guardController.onChange = { [weak self] in self?.refreshUI() }

        prepareControlToken()
        buildStatusItem()
        buildMenu()
        refreshUI()

        refreshTimer = Timer.scheduledTimer(
            timeInterval: 2,
            target: self,
            selector: #selector(refreshTick),
            userInfo: nil,
            repeats: true
        )
        if let refreshTimer {
            RunLoop.main.add(refreshTimer, forMode: .common)
        }

        // 「常驻守卫」随菜单栏应用启动；暂停/恢复仍由菜单控制。
        guardController.start(reason: "应用启动")

        let queued = pendingControlURLs
        pendingControlURLs.removeAll()
        queued.forEach(handleControlURL)
    }

    /// 菜单栏应用的本地 URL 控制面：与菜单项共用同一批 target/action 方法，
    /// 便于在没有辅助功能（Accessibility）授权的机器上做自动化验收。
    func application(_ application: NSApplication, open urls: [URL]) {
        guard consoleController != nil, statusItem != nil else {
            pendingControlURLs.append(contentsOf: urls)
            return
        }
        urls.forEach(handleControlURL)
    }

    private func prepareControlToken() {
        controlTokenValue = UUID().uuidString.lowercased()
        let path = RuntimePaths.controlToken.path
        do {
            try FileManager.default.createDirectory(
                at: RuntimePaths.dispatchDir,
                withIntermediateDirectories: true
            )
        } catch {
            ConsoleTextLog.line("无法创建调度台目录，URL 控制面已禁用：\(error.localizedDescription)")
            return
        }

        let fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0o600)
        guard fd >= 0 else {
            ConsoleTextLog.line("无法写入菜单控制 token，URL 控制面已禁用。")
            return
        }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        do {
            try handle.write(contentsOf: Data((controlTokenValue + "\n").utf8))
        } catch {
            ConsoleTextLog.line("写入菜单控制 token 失败，URL 控制面已禁用。")
            controlTokenValue = ""
        }
        try? handle.close()
        chmod(path, 0o600)
    }

    private func handleControlURL(_ url: URL) {
        guard url.scheme?.lowercased() == "collab-menubar" else { return }
        let rawCommand = url.host ?? url.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard !rawCommand.isEmpty else { return }
        let command = rawCommand.lowercased()

        let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        let providedToken = components?.queryItems?.first(where: { $0.name == "token" })?.value ?? ""
        guard !controlTokenValue.isEmpty, providedToken == controlTokenValue else {
            ConsoleTextLog.line("拒绝未授权的菜单控制命令：\(command)")
            return
        }

        switch command {
        case "start-console": startConsoleAction()
        case "stop-console": stopConsoleAction()
        case "restart-console": restartConsoleAction()
        case "open-web": openWebAction()
        case "start-guard": startGuardAction()
        case "stop-guard": stopGuardAction()
        case "open-project": openProjectAction()
        case "open-log": openLogAction()
        case "quit": quitAction()
        case "dump-menu": dumpMenuSnapshot(reason: "URL 请求")
        default:
            ConsoleTextLog.line("忽略未知的菜单控制命令：\(rawCommand)")
        }
    }

    private func dumpMenuSnapshot(reason: String) {
        guard let menu = statusItem.menu else { return }
        var lines = ["\n[菜单快照] \(Clock.display()) \(reason)"]
        for item in menu.items {
            if item.isSeparatorItem {
                lines.append("---")
            } else {
                lines.append("[\(item.isEnabled ? "启用" : "禁用")] \(item.title)")
            }
        }
        lines.append("")
        FileIO.append(lines.joined(separator: "\n") + "\n", to: RuntimePaths.consoleLog)
    }

    func applicationWillTerminate(_ notification: Notification) {
        isTerminating = true
        refreshTimer?.invalidate()
        refreshTimer = nil
        guardController.shutdownSync()
        consoleController.shutdownSync()
    }

    // MARK: 菜单

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.font = NSFont.systemFont(ofSize: 12, weight: .medium)
        statusItem.button?.toolTip = "多智能体协作台"
    }

    private func makeItem(
        _ title: String,
        action: Selector? = nil,
        enabled: Bool = true
    ) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = action == nil ? nil : self
        item.isEnabled = enabled
        return item
    }

    private func buildMenu() {
        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.delegate = self

        consoleStatusItem = makeItem("控制台：○ 已停止", enabled: false)
        startConsoleItem = makeItem("启动控制台", action: #selector(startConsoleAction))
        stopConsoleItem = makeItem("停止控制台", action: #selector(stopConsoleAction))
        restartConsoleItem = makeItem("重启控制台", action: #selector(restartConsoleAction))
        openWebItem = makeItem("打开网页", action: #selector(openWebAction))
        guardStatusItem = makeItem("守卫：○ 已停止", enabled: false)
        startGuardItem = makeItem("启动守卫", action: #selector(startGuardAction))
        stopGuardItem = makeItem("停止守卫", action: #selector(stopGuardAction))
        openProjectItem = makeItem("打开项目文件夹", action: #selector(openProjectAction))
        openLogItem = makeItem("打开日志", action: #selector(openLogAction))
        quitItem = makeItem("退出", action: #selector(quitAction))

        [
            consoleStatusItem,
            startConsoleItem,
            stopConsoleItem,
            restartConsoleItem,
            NSMenuItem.separator(),
            openWebItem,
            NSMenuItem.separator(),
            guardStatusItem,
            startGuardItem,
            stopGuardItem,
            NSMenuItem.separator(),
            openProjectItem,
            openLogItem,
            NSMenuItem.separator(),
            quitItem,
        ].forEach { menu.addItem($0) }

        statusItem.menu = menu
    }

    func menuWillOpen(_ menu: NSMenu) {
        refreshUI()
    }

    @objc private func refreshTick() {
        refreshUI()
    }

    private func refreshUI() {
        guard consoleController != nil, guardController != nil else { return }
        let consoleSnapshot = consoleController.snapshot()
        let guardActive = guardController.enabled

        if consoleSnapshot.restarting {
            consoleStatusItem.title = "控制台：◌ 重启中…"
        } else if consoleSnapshot.ownedPID != nil {
            consoleStatusItem.title = "控制台：● 运行中"
        } else if consoleSnapshot.portOpen {
            consoleStatusItem.title = "控制台：● 运行中（外部）"
        } else {
            consoleStatusItem.title = "控制台：○ 已停止"
        }

        guardStatusItem.title = guardActive ? "守卫：● 常驻中" : "守卫：○ 已停止"

        startConsoleItem.isEnabled = !consoleSnapshot.running && !consoleSnapshot.restarting && !isTerminating
        stopConsoleItem.isEnabled = consoleSnapshot.ownedPID != nil && !isTerminating
        restartConsoleItem.isEnabled = consoleSnapshot.ownedPID != nil && !isTerminating
        openWebItem.isEnabled = !isTerminating
        startGuardItem.isEnabled = !guardActive && !isTerminating
        stopGuardItem.isEnabled = guardActive && !isTerminating
        openProjectItem.isEnabled = !isTerminating
        openLogItem.isEnabled = !isTerminating
        quitItem.isEnabled = !isTerminating

        let consoleOn = consoleSnapshot.running
        let dot: String
        let dotColor: NSColor
        if consoleOn && guardActive {
            dot = "●"
            dotColor = .systemGreen
        } else if consoleOn || guardActive {
            dot = "◐"
            dotColor = .systemOrange
        } else {
            dot = "○"
            dotColor = .secondaryLabelColor
        }

        let title = NSMutableAttributedString(
            string: "协作 ",
            attributes: [
                .font: NSFont.systemFont(ofSize: 12, weight: .medium),
                .foregroundColor: NSColor.labelColor,
            ]
        )
        title.append(
            NSAttributedString(
                string: dot,
                attributes: [
                    .font: NSFont.systemFont(ofSize: 13, weight: .bold),
                    .foregroundColor: dotColor,
                ]
            )
        )
        statusItem.button?.attributedTitle = title
        statusItem.button?.toolTip = [
            "控制台：" + (consoleSnapshot.ownedPID.map { "运行中，PID \($0)" } ?? (consoleSnapshot.portOpen ? "运行中（外部）" : "已停止")),
            "守卫：" + (guardActive ? "常驻中" : "已停止"),
        ].joined(separator: " · ")
    }

    @objc private func startConsoleAction() {
        consoleController.start(manual: true)
    }

    @objc private func stopConsoleAction() {
        consoleController.stop()
    }

    @objc private func restartConsoleAction() {
        consoleController.restart()
    }

    @objc private func openWebAction() {
        if let url = URL(string: "http://127.0.0.1:\(RuntimePaths.consolePort)") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc private func startGuardAction() {
        guardController.start(reason: "菜单操作")
    }

    @objc private func stopGuardAction() {
        guardController.stop(reason: "菜单操作")
    }

    @objc private func openProjectAction() {
        NSWorkspace.shared.open(RuntimePaths.projectRoot)
    }

    @objc private func openLogAction() {
        let fm = FileManager.default
        if fm.fileExists(atPath: RuntimePaths.journalLog.path) {
            NSWorkspace.shared.open(RuntimePaths.journalLog)
        } else {
            NSWorkspace.shared.open(RuntimePaths.dispatchDir)
        }
    }

    @objc private func quitAction() {
        isTerminating = true
        NSApp.terminate(nil)
    }

    // MARK: 信号

    private func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in
                guard let self, !self.isTerminating else { return }
                self.isTerminating = true
                NSApp.terminate(nil)
            }
            source.resume()
            signalSources.append(source)
        }
    }
}

// -----------------------------------------------------------------------------
// 入口
// -----------------------------------------------------------------------------

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
