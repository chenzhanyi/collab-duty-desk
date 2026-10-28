import AppKit
import Darwin
import Foundation

// -----------------------------------------------------------------------------
// 路径与常量
// -----------------------------------------------------------------------------

enum RuntimePaths {
    /// 默认 8787；自动化测试可用 COLLAB_CONSOLE_PORT=8790 指向假实例。
    static let consolePort: UInt16 = {
        if
            let raw = ProcessInfo.processInfo.environment["COLLAB_CONSOLE_PORT"],
            let value = UInt16(raw),
            value >= 1024
        {
            return value
        }
        return 8787
    }()
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
    /// 不发起 HTTP 请求，只检查 127.0.0.1:<port> 是否在监听（TCP 健康检查）。
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
    static func terminateAndWait(_ process: Process?, grace: TimeInterval = 2.0, beforeKill: (() -> Void)? = nil) {
        guard let process, process.isRunning else { return }
        let pid = process.processIdentifier
        process.terminate()
        let deadline = Date().addingTimeInterval(grace)
        while process.isRunning && Date() < deadline {
            usleep(50_000)
        }
        if process.isRunning {
            beforeKill?()
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
        let stopping: Bool
        let restartCount: Int
        let lastRestartAt: Date?
        let healthFailures: Int
        let wantsToRun: Bool

        var running: Bool { ownedPID != nil || portOpen }
        var external: Bool { portOpen && ownedPID == nil }
    }

    private let port: UInt16
    private let consoleScript: URL
    private let consoleLogURL: URL
    private let projectRoot: URL
    private let watchdogInterval: TimeInterval
    private let startupGrace: TimeInterval

    // Runner allows 3s TERM + process-group cleanup; give it time to finish.
    private let shutdownGrace: TimeInterval = 12.0
    private var process: Process?
    private var outputHandle: FileHandle?
    private var restartWork: DispatchWorkItem?
    private var restartAttempt = 0
    private var wantsToRun = false
    private var stopping = false
    private var restartAfterTermination = false
    private var shuttingDown = false
    private var launchedAt: Date?
    private var watchdog: DispatchSourceTimer?
    private var healthFailures = 0
    private var restartCount = 0
    private var lastRestartAt: Date?

    var onChange: (() -> Void)?

    init(
        port: UInt16 = RuntimePaths.consolePort,
        consoleScript: URL = RuntimePaths.consoleScript,
        consoleLogURL: URL = RuntimePaths.consoleLog,
        projectRoot: URL = RuntimePaths.projectRoot,
        watchdogInterval: TimeInterval = 2.0,
        startupGrace: TimeInterval = 6.0
    ) {
        self.port = port
        self.consoleScript = consoleScript
        self.consoleLogURL = consoleLogURL
        self.projectRoot = projectRoot
        self.watchdogInterval = watchdogInterval
        self.startupGrace = startupGrace
    }

    func snapshot() -> Snapshot {
        let alive = process?.isRunning == true
        let ownedPID = alive ? process?.processIdentifier : nil
        let portOpen = PortProbe.isOpen(port)
        return Snapshot(
            ownedPID: ownedPID,
            portOpen: portOpen,
            restarting: restartWork != nil || restartAfterTermination,
            stopping: stopping,
            restartCount: restartCount,
            lastRestartAt: lastRestartAt,
            healthFailures: healthFailures,
            wantsToRun: wantsToRun
        )
    }

    private func log(_ message: String) {
        let line = "[菜单栏] \(Clock.display()) \(message)\n"
        if !FileIO.appendDirect(line, to: consoleLogURL) {
            FileIO.append(line, to: RuntimePaths.consoleLog)
        }
    }

    // MARK: 真实看护循环

    /// 每 2 秒检查 TCP 健康；不可达则终止无响应进程并按 1/2/4/8/16/30 秒退避重启。
    func startWatchdog() {
        guard watchdog == nil, !shuttingDown else { return }
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(
            deadline: .now() + watchdogInterval,
            repeating: watchdogInterval,
            leeway: .milliseconds(200)
        )
        timer.setEventHandler { [weak self] in
            self?.healthTick()
        }
        watchdog = timer
        timer.resume()
        log("控制台看护已启动：每 \(Int(watchdogInterval)) 秒检查 127.0.0.1:\(port)")
    }

    private func stopWatchdog() {
        watchdog?.cancel()
        watchdog = nil
    }

    private func healthTick() {
        guard !shuttingDown, wantsToRun else { return }
        if PortProbe.isOpen(port) {
            if healthFailures > 0 {
                log("健康检查恢复：127.0.0.1:\(port) 可达。")
            }
            healthFailures = 0
            if let work = restartWork, process?.isRunning == true {
                work.cancel()
                restartWork = nil
                log("控制台已恢复，取消待执行的重启。")
                onChange?()
            }
            return
        }

        healthFailures += 1
        if let process, process.isRunning {
            let uptime = launchedAt.map { Date().timeIntervalSince($0) } ?? 0
            if uptime < startupGrace {
                return
            }
            let pid = process.processIdentifier
            log(
                "健康检查失败（第 \(healthFailures) 次）：127.0.0.1:\(port) 不可达，"
                + "受控进程 PID \(pid) 仍存活（已运行 \(Int(uptime)) 秒），先终止再退避重启。"
            )
            stopping = false
            process.terminate()
            DispatchQueue.main.asyncAfter(deadline: .now() + shutdownGrace) { [weak self, weak process] in
                guard
                    let self,
                    let process,
                    self.process === process,
                    process.isRunning
                else { return }
                self.forceStop(process)
                self.log("PID \(pid) 未在 12 秒内退出，已强制结束。")
            }
            scheduleRestart()
            onChange?()
            return
        }

        if healthFailures == 1 {
            log("健康检查失败（第 1 次）：127.0.0.1:\(port) 不可达且无受控进程，安排退避重启。")
        }
        scheduleRestart()
        onChange?()
    }

    // MARK: 启停

    func start(manual: Bool) {
        guard !shuttingDown else { return }
        if process?.isRunning == true {
            log("控制台已在运行，忽略重复启动。")
            return
        }
        if PortProbe.isOpen(port) {
            log("端口 \(port) 已被其他进程占用，未启动新控制台。")
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
            try spawn(isAuto: !manual)
        } catch {
            log("控制台启动失败：\(error.localizedDescription)")
            onChange?()
            if manual {
                wantsToRun = false
            } else if wantsToRun {
                scheduleRestart()
            }
        }
    }

    private func spawn(isAuto: Bool) throws {
        guard FileManager.default.fileExists(atPath: consoleScript.path) else {
            throw NSError(
                domain: "CollabMenuBar",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "找不到 console/server.py"]
            )
        }
        guard let logHandle = FileIO.openAppendHandle(consoleLogURL) else {
            throw NSError(
                domain: "CollabMenuBar",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "无法打开 console.log"]
            )
        }

        let newProcess = ProcessLauncher.makeDirect(
            command: [
                "/usr/bin/env", "python3", consoleScript.path,
                "--port", String(port),
            ],
            stdout: logHandle,
            stderr: logHandle
        )
        newProcess.currentDirectoryURL = projectRoot
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
        healthFailures = 0
        log("启动控制台（PID \(newProcess.processIdentifier)）：python3 console/server.py --port \(port)")
        if isAuto {
            restartCount += 1
            lastRestartAt = Date()
            log("自动重启成功（第 \(restartCount) 次 · \(Clock.display())）：PID \(newProcess.processIdentifier)")
        }
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
        healthFailures = 0

        if shuttingDown { return }

        if restartAfterTermination {
            restartAfterTermination = false
            wantsToRun = true
            log("控制台 \(pid) 已停止，执行手动重启。")
            start(manual: true)
            return
        }

        if wasStopping || !wantsToRun {
            wantsToRun = false
            log("控制台已停止（PID \(pid)，\(reason)）。")
            onChange?()
            return
        }

        if uptime >= 20 {
            restartAttempt = 0
        }
        log("控制台意外退出（PID \(pid)，\(reason)，运行 \(Int(uptime)) 秒）。")
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
        guard !shuttingDown, wantsToRun else { return }
        if restartWork != nil { return }
        if PortProbe.isOpen(port) {
            wantsToRun = false
            log("端口已被其他进程占用，取消控制台自动重启。")
            onChange?()
            return
        }

        restartAttempt += 1
        let delays: [TimeInterval] = [1, 2, 4, 8, 16, 30]
        let delay = delays[min(restartAttempt - 1, delays.count - 1)]
        log("将在 \(Int(delay)) 秒后自动重启控制台（退避第 \(restartAttempt) 档）。")

        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.restartWork = nil
            guard !self.shuttingDown, self.wantsToRun else { return }
            if PortProbe.isOpen(self.port) {
                self.wantsToRun = false
                self.log("端口已被占用，放弃本次自动重启。")
                self.onChange?()
                return
            }
            if let process = self.process, process.isRunning {
                let pid = process.processIdentifier
                self.log("进程 PID \(pid) 正在清理 Agent，等待退出后继续重启。")
                self.scheduleRestart()
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
        healthFailures = 0

        if let process, process.isRunning {
            stopping = true
            let pid = process.processIdentifier
            log("正在停止控制台（PID \(pid)）；手动停止后不再自动拉起。")
            process.terminate()
            DispatchQueue.main.asyncAfter(deadline: .now() + shutdownGrace) { [weak self, weak process] in
                guard
                    let self,
                    let process,
                    self.process === process,
                    process.isRunning
                else { return }
                self.forceStop(process)
                self.log("控制台未在 12 秒内退出，已强制结束（PID \(pid)）。")
            }
            onChange?()
            return
        }

        if PortProbe.isOpen(port) {
            log("\(port) 由外部进程监听；它不由本应用启动，应用不会终止它。")
        } else {
            log("控制台已经停止。")
        }
        onChange?()
    }

    func restart() {
        guard !shuttingDown else { return }
        if let process, process.isRunning {
            wantsToRun = true
            restartAfterTermination = true
            restartAttempt = 0
            restartWork?.cancel()
            restartWork = nil
            let pid = process.processIdentifier
            log("重启控制台（PID \(pid)）。")
            process.terminate()
            DispatchQueue.main.asyncAfter(deadline: .now() + shutdownGrace) { [weak self, weak process] in
                if let self, let process, self.process === process, process.isRunning {
                    self.forceStop(process)
                }
            }
            onChange?()
            return
        }
        if PortProbe.isOpen(port) {
            log("\(port) 由外部进程监听，不能由本应用重启。")
            return
        }
        start(manual: true)
    }

    private func cleanupOwnedAgent(consolePID: pid_t) {
        let lockURL = projectRoot.appendingPathComponent("协作/调度台/runner.lock")
        guard let text = try? String(contentsOf: lockURL, encoding: .utf8) else { return }
        var fields: [String: String] = [:]
        for line in text.components(separatedBy: .newlines) {
            let parts = line.split(separator: ":", maxSplits: 1).map(String.init)
            if parts.count == 2 {
                fields[parts[0].trimmingCharacters(in: .whitespaces)] = parts[1].trimmingCharacters(in: .whitespaces)
            }
        }
        guard let owner = fields["owner_pid"].flatMap(Int32.init), owner == consolePID,
              let agent = fields["agent_pid"].flatMap(Int32.init), agent > 1,
              agent != getpgrp(), kill(-agent, 0) == 0 else { return }
        kill(-agent, SIGKILL)
        log("控制台清理超时，已结束本实例拥有的 Agent 进程组 \(agent)。")
    }

    private func forceStop(_ process: Process) {
        cleanupOwnedAgent(consolePID: process.processIdentifier)
        kill(process.processIdentifier, SIGKILL)
    }

    func shutdownSync() {
        shuttingDown = true
        wantsToRun = false
        restartAfterTermination = false
        restartWork?.cancel()
        restartWork = nil
        stopWatchdog()

        if let process, process.isRunning {
            log("菜单栏应用退出，停止受控控制台（PID \(process.processIdentifier)）。")
            ProcessKiller.terminateAndWait(process, grace: shutdownGrace) { [self] in
                cleanupOwnedAgent(consolePID: process.processIdentifier)
            }
        }
        try? outputHandle?.close()
        outputHandle = nil
        process = nil
        launchedAt = nil
    }
}

// -----------------------------------------------------------------------------
// 看护自检（--watchdog-selftest）：8790 假实例，keep-alive / kill / 退避恢复
// -----------------------------------------------------------------------------

enum WatchdogSelfTest {
    private static func httpOK(_ port: UInt16) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let semaphore = DispatchSemaphore(value: 0)
        var ok = false
        let task = URLSession.shared.dataTask(with: request) { _data, response, _error in
            ok = (response as? HTTPURLResponse)?.statusCode == 200
            semaphore.signal()
        }
        task.resume()
        if semaphore.wait(timeout: .now() + 2.5) == .timedOut {
            task.cancel()
            return false
        }
        return ok
    }

    private static func pump(_ seconds: TimeInterval) {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }
    }

    private static func waitUntil(
        _ timeout: TimeInterval,
        _ condition: () -> Bool
    ) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        }
        return condition()
    }

    private static func writeFakeServer(_ url: URL) throws {
        let script = #"""
        #!/usr/bin/env python3
        import http.server
        import sys

        port = 8790
        if "--port" in sys.argv:
            port = int(sys.argv[sys.argv.index("--port") + 1])

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"ok\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
        """#
        try script.write(to: url, atomically: true, encoding: .utf8)
    }

    static func run() -> Int32 {
        let env = ProcessInfo.processInfo.environment
        let port = UInt16(env["COLLAB_SELFTEST_PORT"] ?? "") ?? 8790
        let temp = FileManager.default.temporaryDirectory
            .appendingPathComponent("collab-watchdog-selftest-\(getpid())", isDirectory: true)
        do {
            try FileManager.default.createDirectory(
                at: temp,
                withIntermediateDirectories: true
            )
        } catch {
            print("WATCHDOG-SELFTEST FAIL: 无法创建临时目录 \(error)")
            return 1
        }
        defer { try? FileManager.default.removeItem(at: temp) }

        let fake = temp.appendingPathComponent("fake_console.py")
        do {
            try writeFakeServer(fake)
        } catch {
            print("WATCHDOG-SELFTEST FAIL: 无法写假实例 \(error)")
            return 1
        }

        let controller = ConsoleController(
            port: port,
            consoleScript: fake,
            consoleLogURL: temp.appendingPathComponent("console.log"),
            projectRoot: temp,
            watchdogInterval: 2.0,
            startupGrace: 4.0
        )
        print("WATCHDOG-SELFTEST start port=\(port) temp=\(temp.path)")
        controller.startWatchdog()
        controller.start(manual: true)

        guard waitUntil(8.0, { PortProbe.isOpen(port) && httpOK(port) }) else {
            print("WATCHDOG-SELFTEST FAIL: 假实例未在 8 秒内启动并返回 200")
            controller.shutdownSync()
            return 1
        }
        guard let pid1 = controller.snapshot().ownedPID else {
            print("WATCHDOG-SELFTEST FAIL: 没有受控 PID")
            controller.shutdownSync()
            return 1
        }
        print("WATCHDOG-SELFTEST first instance pid=\(pid1) up")

        var failures: [String] = []
        var lastRecovery = 0.0
        for attempt in 1...2 {
            guard let pid = controller.snapshot().ownedPID else {
                failures.append("第 \(attempt) 次 kill 前没有受控 PID")
                break
            }
            let killAt = Date()
            kill(pid, SIGKILL)
            _ = waitUntil(5.0, { !PortProbe.isOpen(port) })
            let recovered = waitUntil(12.0, {
                PortProbe.isOpen(port)
                    && controller.snapshot().ownedPID != nil
                    && httpOK(port)
            })
            let elapsed = Date().timeIntervalSince(killAt)
            lastRecovery = elapsed
            let count = controller.snapshot().restartCount
            print(
                "WATCHDOG-SELFTEST kill #\(attempt) pid=\(pid) recovered=\(recovered)"
                + " http200=\(httpOK(port))"
                + String(format: " elapsed=%.2fs restarts=%d", elapsed, count)
            )
            if !recovered {
                failures.append("第 \(attempt) 次 kill 后 12 秒内未恢复")
            } else if elapsed > 10.0 {
                failures.append(String(format: "第 %d 次恢复 %.2fs > 10s", attempt, elapsed))
            }
            if count < attempt {
                failures.append("第 \(attempt) 次恢复后 restartCount=\(count)")
            }
            _ = waitUntil(1.0, { false })
        }

        let beforeStop = controller.snapshot().restartCount
        controller.stop()
        guard waitUntil(5.0, { !PortProbe.isOpen(port) }) else {
            failures.append("手动停止后端口仍开放")
            controller.shutdownSync()
            print("WATCHDOG-SELFTEST FAIL: \(failures.joined(separator: "；"))")
            return 1
        }
        pump(4.0)
        let afterStop = controller.snapshot()
        if PortProbe.isOpen(port) {
            failures.append("手动停止后 4 秒内端口被自动拉起")
        }
        if afterStop.restartCount != beforeStop {
            failures.append("手动停止后 restartCount 从 \(beforeStop) 变为 \(afterStop.restartCount)")
        }
        print(
            "WATCHDOG-SELFTEST manual-stop portOpen=\(PortProbe.isOpen(port))"
            + " restarts=\(afterStop.restartCount)"
        )

        let logPath = temp.appendingPathComponent("console.log")
        let logText = (try? String(contentsOf: logPath, encoding: .utf8)) ?? ""
        let logLines = logText.split(separator: "\n").filter {
            $0.contains("自动重启") || $0.contains("健康检查失败")
                || $0.contains("将在") || $0.contains("看护已启动")
        }
        print("WATCHDOG-SELFTEST log:")
        logLines.forEach { print("  \($0)") }
        print(String(format: "WATCHDOG-SELFTEST lastRecovery=%.2fs", lastRecovery))

        controller.shutdownSync()
        if failures.isEmpty {
            print("WATCHDOG-SELFTEST PASS")
            return 0
        }
        print("WATCHDOG-SELFTEST FAIL: \(failures.joined(separator: "；"))")
        return 1
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

/// 菜单与 URL 控制入口共用页面定义，避免标签和跳转地址各自维护。
enum ConsolePage: String, CaseIterable {
    case dashboard, newTask, runs, dispatch, roles, agents, archive

    var title: String {
        switch self {
        case .dashboard: return "打开任务看板"
        case .newTask: return "发布新任务…"
        case .runs: return "查看运行记录"
        case .dispatch: return "查看调度台"
        case .roles: return "角色配置"
        case .agents: return "Agent 应用"
        case .archive: return "归档库"
        }
    }

    var path: String {
        switch self {
        case .dashboard: return "/"
        case .newTask: return "/new"
        case .runs: return "/runs"
        case .dispatch: return "/dispatch"
        case .roles: return "/roles"
        case .agents: return "/agents"
        case .archive: return "/archive"
        }
    }

    var command: String {
        switch self {
        case .dashboard: return "open-web"
        case .newTask: return "new-task"
        default: return "open-" + rawValue
        }
    }

    var symbol: String {
        switch self {
        case .dashboard: return "square.grid.2x2"
        case .newTask: return "plus.square"
        case .runs: return "clock.arrow.circlepath"
        case .dispatch: return "waveform.path.ecg"
        case .roles: return "person.2"
        case .agents: return "terminal"
        case .archive: return "archivebox"
        }
    }

    func url(port: UInt16 = RuntimePaths.consolePort) -> URL {
        URL(string: "http://127.0.0.1:\(port)\(path)")!
    }
}

struct ConsoleMenuState {
    let snapshot: ConsoleController.Snapshot
    let terminating: Bool

    var status: String {
        let note = snapshot.restartCount > 0 ? " · 已自动恢复 \(snapshot.restartCount) 次" : ""
        if terminating { return "控制台：正在退出…" }
        if snapshot.stopping { return "控制台：正在停止并清理任务…" }
        if snapshot.restarting { return "控制台：正在重启…" + note }
        if snapshot.external { return "控制台：已连接外部服务" }
        if snapshot.portOpen { return "控制台：就绪" + note }
        if snapshot.ownedPID != nil { return "控制台：正在启动…" + note }
        return "控制台：未启动（打开页面时自动启动）"
    }

    var powerTitle: String {
        if snapshot.stopping { return "正在停止控制台…" }
        if snapshot.external { return "停止控制台（由外部启动）" }
        if snapshot.ownedPID != nil { return "停止控制台" }
        if snapshot.restarting || snapshot.wantsToRun { return "取消控制台启动" }
        return "启动控制台"
    }

    var canNavigate: Bool { !terminating && !snapshot.stopping }
    var canControl: Bool { !terminating && !snapshot.stopping && !snapshot.external }
    var canRestart: Bool {
        canControl && snapshot.ownedPID != nil && !snapshot.restarting && snapshot.portOpen
    }
}

/// 只在本地服务就绪后打开页面。连续选择时保留最后一个目标，停止/退出可取消。
final class ConsolePageNavigator {
    private let isReady: () -> Bool
    private let start: () -> Void
    private let open: (URL) -> Void
    private let failed: (URL) -> Void
    private let timeout: TimeInterval
    private let pollInterval: TimeInterval
    private var pending: URL?
    private var deadline = Date.distantPast
    private var work: DispatchWorkItem?

    init(
        timeout: TimeInterval = 30,
        pollInterval: TimeInterval = 0.25,
        isReady: @escaping () -> Bool,
        start: @escaping () -> Void,
        open: @escaping (URL) -> Void,
        failed: @escaping (URL) -> Void
    ) {
        self.timeout = timeout
        self.pollInterval = pollInterval
        self.isReady = isReady
        self.start = start
        self.open = open
        self.failed = failed
    }

    func request(_ url: URL) {
        cancel()
        if isReady() {
            open(url)
            return
        }
        pending = url
        deadline = Date().addingTimeInterval(timeout)
        start()
        check()
    }

    func cancel() {
        work?.cancel()
        work = nil
        pending = nil
    }

    private func check() {
        guard let url = pending else { return }
        if isReady() {
            cancel()
            open(url)
        } else if Date() >= deadline {
            cancel()
            failed(url)
        } else {
            let next = DispatchWorkItem { [weak self] in self?.check() }
            work = next
            DispatchQueue.main.asyncAfter(deadline: .now() + pollInterval, execute: next)
        }
    }

    deinit { work?.cancel() }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private var consoleController: ConsoleController!
    private var guardController: GuardController!
    private var refreshTimer: Timer?
    private var signalSources: [DispatchSourceSignal] = []
    private var isTerminating = false
    private var pendingControlURLs: [URL] = []
    private var controlTokenValue = ""
    private var openPageURL: (URL) -> Void = { NSWorkspace.shared.open($0) }
    private lazy var pageNavigator = ConsolePageNavigator(
        isReady: { [weak self] in
            guard let snapshot = self?.consoleController?.snapshot() else { return false }
            return snapshot.portOpen && !snapshot.stopping && !snapshot.restarting
        },
        start: { [weak self] in
            guard let controller = self?.consoleController else { return }
            let snapshot = controller.snapshot()
            if !snapshot.running && !snapshot.restarting {
                controller.start(manual: true)
            }
        },
        open: { [weak self] url in self?.openPageURL(url) },
        failed: { [weak self] url in self?.showPageOpenFailure(url) }
    )

    private var consoleStatusItem: NSMenuItem!
    private var consolePowerItem: NSMenuItem!
    private var restartConsoleItem: NSMenuItem!
    private var pageItems: [NSMenuItem] = []
    private var guardStatusItem: NSMenuItem!
    private var guardPowerItem: NSMenuItem!
    private var openProjectItem: NSMenuItem!
    private var logItems: [NSMenuItem] = []
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
        statusItem.menu = buildMenu()
        refreshUI()
        consoleController.startWatchdog()

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

        if let page = ConsolePage.allCases.first(where: { $0.command == command }) {
            openPage(page)
            return
        }
        switch command {
        case "start-console": startConsoleAction()
        case "stop-console": stopConsoleAction()
        case "restart-console": restartConsoleAction()
        case "start-guard": startGuardAction()
        case "stop-guard": stopGuardAction()
        case "open-project": openProjectAction()
        case "open-log": openLogAction()
        case "open-console-log": openConsoleLogAction()
        case "open-log-folder": openLogFolderAction()
        case "quit": quitAction()
        case "dump-menu": dumpMenuSnapshot(reason: "URL 请求")
        default:
            ConsoleTextLog.line("忽略未知的菜单控制命令：\(rawCommand)")
        }
    }

    private func dumpMenuSnapshot(reason: String) {
        guard let menu = statusItem.menu else { return }
        var lines = ["\n[菜单快照] \(Clock.display()) \(reason)"]
        func appendItems(_ menu: NSMenu, indent: String = "") {
            for item in menu.items {
                if item.isSeparatorItem {
                    lines.append(indent + "---")
                } else {
                    lines.append(indent + "[\(item.isEnabled ? "启用" : "禁用")] \(item.title)")
                    if let submenu = item.submenu { appendItems(submenu, indent: indent + "  ") }
                }
            }
        }
        refreshUI()
        appendItems(menu)
        lines.append("")
        FileIO.append(lines.joined(separator: "\n") + "\n", to: RuntimePaths.consoleLog)
    }

    func applicationWillTerminate(_ notification: Notification) {
        isTerminating = true
        refreshTimer?.invalidate()
        refreshTimer = nil
        pageNavigator.cancel()
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
        enabled: Bool = true,
        key: String = "",
        symbol: String? = nil,
        identifier: String? = nil,
        toolTip: String? = nil
    ) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = action == nil ? nil : self
        item.isEnabled = enabled
        item.toolTip = toolTip
        if let identifier { item.identifier = NSUserInterfaceItemIdentifier(identifier) }
        if let symbol {
            item.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
            item.image?.size = NSSize(width: 16, height: 16)
        }
        return item
    }

    private func makeMenu(_ title: String) -> NSMenu {
        let menu = NSMenu(title: title)
        menu.autoenablesItems = false
        menu.delegate = self
        return menu
    }

    private func makePageItem(_ page: ConsolePage, key: String = "") -> NSMenuItem {
        let item = makeItem(
            page.title, action: #selector(openPageAction(_:)), key: key,
            symbol: page.symbol, identifier: page.command,
            toolTip: "控制台未启动时会先自动启动，再打开此页面。"
        )
        item.representedObject = page.rawValue
        pageItems.append(item)
        return item
    }

    private func buildMenu() -> NSMenu {
        pageItems.removeAll()
        logItems.removeAll()
        let menu = makeMenu("多智能体协作台")

        // 日常路径：看任务 → 发布 → 查运行；进程维护放进子菜单。
        menu.addItem(makePageItem(.dashboard, key: "o"))
        menu.addItem(makePageItem(.newTask, key: "n"))
        menu.addItem(makePageItem(.runs, key: "r"))
        menu.addItem(.separator())
        consoleStatusItem = makeItem("控制台：未启动", enabled: false)
        guardStatusItem = makeItem("文件守卫：已暂停", enabled: false,
                                   toolTip: "记录任务文件变化；暂停不会停止任务执行。")
        menu.addItem(consoleStatusItem)
        menu.addItem(guardStatusItem)
        menu.addItem(.separator())

        let management = makeMenu("项目管理")
        [ConsolePage.roles, .agents, .archive].forEach { management.addItem(makePageItem($0)) }
        management.addItem(.separator())
        openProjectItem = makeItem("打开项目文件夹", action: #selector(openProjectAction), symbol: "folder")
        management.addItem(openProjectItem)
        let managementItem = makeItem("项目管理", symbol: "slider.horizontal.3")
        managementItem.submenu = management
        menu.addItem(managementItem)

        let maintenance = makeMenu("运行与维护")
        maintenance.addItem(makePageItem(.dispatch))
        maintenance.addItem(.separator())
        consolePowerItem = makeItem(
            "启动控制台", action: #selector(toggleConsoleAction), symbol: "power",
            identifier: "toggle-console",
            toolTip: "停止控制台会中断正在执行的任务，并关闭自动重启。"
        )
        restartConsoleItem = makeItem(
            "重启控制台", action: #selector(restartConsoleAction), symbol: "arrow.clockwise",
            identifier: "restart-console", toolTip: "先停止当前任务与控制台，再重新启动服务。"
        )
        guardPowerItem = makeItem(
            "恢复文件守卫", action: #selector(toggleGuardAction), symbol: "eye",
            identifier: "toggle-guard", toolTip: "只影响文件变化记录，不会停止或启动任务。"
        )
        logItems = [
            makeItem("打开控制台日志", action: #selector(openConsoleLogAction), symbol: "doc.text"),
            makeItem("打开文件守卫日志", action: #selector(openLogAction), symbol: "doc.text"),
            makeItem("打开日志文件夹", action: #selector(openLogFolderAction), symbol: "folder"),
        ]
        [
            consolePowerItem,
            restartConsoleItem,
            NSMenuItem.separator(),
            guardPowerItem,
            NSMenuItem.separator(),
        ].forEach { maintenance.addItem($0) }
        logItems.forEach { maintenance.addItem($0) }
        let maintenanceItem = makeItem("运行与维护", symbol: "gearshape")
        maintenanceItem.submenu = maintenance
        menu.addItem(maintenanceItem)
        menu.addItem(.separator())

        quitItem = makeItem(
            "退出协作菜单栏", action: #selector(quitAction), key: "q",
            toolTip: "退出并停止本应用启动的控制台及文件守卫。外部启动的控制台继续运行。"
        )
        menu.addItem(quitItem)
        return menu
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
        applyMenuState(consoleSnapshot, guardEnabled: guardActive, guardPhase: guardController.phase)

        let consoleOn = consoleSnapshot.running
        let dot: String
        let dotColor: NSColor
        if consoleOn && consoleSnapshot.portOpen && !consoleSnapshot.restarting && !consoleSnapshot.stopping && guardActive {
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
        let lastRestart = consoleSnapshot.lastRestartAt.map {
            "，最近 " + Clock.display($0)
        } ?? ""
        statusItem.button?.toolTip = [consoleStatusItem.title + lastRestart, guardStatusItem.title].joined(separator: "\n")
    }

    private func applyMenuState(
        _ snapshot: ConsoleController.Snapshot,
        guardEnabled: Bool,
        guardPhase: GuardController.Phase
    ) {
        let state = ConsoleMenuState(snapshot: snapshot, terminating: isTerminating)
        consoleStatusItem.title = state.status
        if !guardEnabled {
            guardStatusItem.title = "文件守卫：已暂停"
        } else {
            switch guardPhase {
            case .watching: guardStatusItem.title = "文件守卫：正在监测"
            case .acking: guardStatusItem.title = "文件守卫：正在记录变更"
            case .waiting: guardStatusItem.title = "文件守卫：等待重新监测"
            case .idle: guardStatusItem.title = "文件守卫：准备监测"
            }
        }
        consolePowerItem.title = state.powerTitle
        consolePowerItem.isEnabled = state.canControl
        if snapshot.external {
            consolePowerItem.toolTip = "当前服务由外部启动，请在原启动位置停止。"
        } else if snapshot.ownedPID != nil {
            consolePowerItem.toolTip = "停止会中断当前任务，并关闭控制台自动重启。"
        } else {
            consolePowerItem.toolTip = "网页入口会自动启动控制台，也可以在此手动管理。"
        }
        restartConsoleItem.isEnabled = state.canRestart
        guardPowerItem.title = guardEnabled ? "暂停文件守卫" : "恢复文件守卫"
        guardPowerItem.image = NSImage(systemSymbolName: guardEnabled ? "pause.circle" : "play.circle", accessibilityDescription: nil)
        guardPowerItem.image?.size = NSSize(width: 16, height: 16)
        guardPowerItem.isEnabled = !isTerminating
        pageItems.forEach { $0.isEnabled = state.canNavigate }
        openProjectItem.isEnabled = !isTerminating
        logItems.forEach { $0.isEnabled = !isTerminating }
        quitItem.isEnabled = !isTerminating
    }

    @objc private func startConsoleAction() {
        consoleController.start(manual: true)
    }

    @objc private func stopConsoleAction() {
        pageNavigator.cancel()
        consoleController.stop()
    }

    @objc private func restartConsoleAction() {
        pageNavigator.cancel()
        consoleController.restart()
    }

    @objc private func toggleConsoleAction() {
        let snapshot = consoleController.snapshot()
        guard ConsoleMenuState(snapshot: snapshot, terminating: isTerminating).canControl else { return }
        if snapshot.ownedPID != nil || snapshot.restarting || snapshot.wantsToRun {
            stopConsoleAction()
        } else {
            startConsoleAction()
        }
    }

    @objc private func openPageAction(_ sender: NSMenuItem) {
        guard let name = sender.representedObject as? String, let page = ConsolePage(rawValue: name) else { return }
        openPage(page)
    }

    private func openPage(_ page: ConsolePage) {
        let state = ConsoleMenuState(snapshot: consoleController.snapshot(), terminating: isTerminating)
        guard state.canNavigate else { return }
        pageNavigator.request(page.url())
    }

    private func showPageOpenFailure(_ url: URL) {
        guard !isTerminating else { return }
        ConsoleTextLog.line("打开页面超时：控制台在 30 秒内未就绪（\(url.path)）。")
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "控制台尚未就绪"
        alert.informativeText = "等待 30 秒后仍无法连接本地控制台。可查看控制台日志排查，或稍后从菜单重新打开页面。"
        alert.addButton(withTitle: "查看控制台日志")
        alert.addButton(withTitle: "稍后再试")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn { openConsoleLogAction() }
    }

    @objc private func startGuardAction() {
        guardController.start(reason: "菜单操作")
    }

    @objc private func stopGuardAction() {
        guardController.stop(reason: "菜单操作")
    }

    @objc private func toggleGuardAction() {
        if guardController.enabled { stopGuardAction() } else { startGuardAction() }
    }

    @objc private func openProjectAction() {
        NSWorkspace.shared.open(RuntimePaths.projectRoot)
    }

    @objc private func openLogAction() {
        openLog(RuntimePaths.journalLog)
    }

    @objc private func openConsoleLogAction() {
        openLog(RuntimePaths.consoleLog)
    }

    private func openLog(_ url: URL) {
        NSWorkspace.shared.open(FileManager.default.fileExists(atPath: url.path) ? url : RuntimePaths.dispatchDir)
    }

    @objc private func openLogFolderAction() {
        NSWorkspace.shared.open(RuntimePaths.dispatchDir)
    }

    @objc private func quitAction() {
        isTerminating = true
        pageNavigator.cancel()
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

// 原生菜单与页面等待逻辑自检；不启动控制台/守卫，不访问项目状态文件。
extension AppDelegate {
    static func runMenuSelfTest() -> Int32 {
        _ = NSApplication.shared
        let app = AppDelegate()
        let menu = app.buildMenu()
        var checks = 0
        var failures: [String] = []
        func check(_ condition: Bool, _ message: String) {
            checks += 1
            if !condition { failures.append(message) }
        }
        func snapshot(
            pid: pid_t? = nil, port: Bool = false, restarting: Bool = false,
            stopping: Bool = false, wants: Bool = false, restartCount: Int = 0
        ) -> ConsoleController.Snapshot {
            ConsoleController.Snapshot(
                ownedPID: pid, portOpen: port, restarting: restarting, stopping: stopping,
                restartCount: restartCount, lastRestartAt: nil, healthFailures: 0, wantsToRun: wants
            )
        }
        func items(_ menu: NSMenu) -> [[String: Any]] {
            menu.items.map { item in
                if item.isSeparatorItem { return ["separator": true] }
                var row: [String: Any] = ["title": item.title, "enabled": item.isEnabled]
                if !item.keyEquivalent.isEmpty { row["shortcut"] = "⌘" + item.keyEquivalent.uppercased() }
                if let id = item.identifier { row["id"] = id.rawValue }
                if let submenu = item.submenu { row["items"] = items(submenu) }
                return row
            }
        }
        func spin(_ seconds: TimeInterval) {
            let until = Date().addingTimeInterval(seconds)
            while Date() < until {
                _ = RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.005))
            }
        }

        check(Array(menu.items.prefix(3)).map(\.title) == ["打开任务看板", "发布新任务…", "查看运行记录"], "常用入口顺序")
        check(menu.items.filter { $0.submenu != nil }.map(\.title) == ["项目管理", "运行与维护"], "管理与维护分组")
        check(app.pageItems.count == ConsolePage.allCases.count, "七个页面入口完整")
        check(Set(app.pageItems.compactMap { $0.identifier?.rawValue }).count == 7, "页面入口唯一")
        for page in ConsolePage.allCases {
            let item = app.pageItems.first { $0.identifier?.rawValue == page.command }
            check(item?.representedObject as? String == page.rawValue && item?.action != nil, "页面动作：\(page.rawValue)")
            check(page.url(port: 18789).path == page.path && page.url(port: 18789).port == 18789, "页面地址：\(page.rawValue)")
        }
        check(menu.items.prefix(3).map(\.keyEquivalent) == ["o", "n", "r"] && app.quitItem.keyEquivalent == "q", "菜单快捷键")

        let states: [(String, ConsoleController.Snapshot, String, String, Bool, Bool)] = [
            ("stopped", snapshot(), "未启动", "启动控制台", true, false),
            ("starting", snapshot(pid: 123, wants: true), "正在启动", "停止控制台", true, false),
            ("ready", snapshot(pid: 123, port: true, wants: true), "就绪", "停止控制台", true, true),
            ("external", snapshot(port: true), "外部服务", "停止控制台（由外部启动）", false, false),
            ("restarting", snapshot(restarting: true, wants: true), "正在重启", "取消控制台启动", true, false),
            ("stopping", snapshot(pid: 123, port: true, stopping: true), "正在停止", "正在停止控制台…", false, false),
        ]
        var stateMenus: [String: Any] = [:]
        for (name, value, status, power, canControl, canRestart) in states {
            app.applyMenuState(value, guardEnabled: true, guardPhase: .watching)
            check(app.consoleStatusItem.title.contains(status), "\(name) 状态文案")
            check(app.consolePowerItem.title == power && app.consolePowerItem.isEnabled == canControl, "\(name) 启停状态")
            check(app.restartConsoleItem.isEnabled == canRestart, "\(name) 重启可用性")
            check(app.pageItems.allSatisfy { $0.isEnabled == !value.stopping }, "\(name) 页面可用性")
            check(app.guardPowerItem.title == "暂停文件守卫", "\(name) 暂停守卫")
            stateMenus[name] = items(menu)
        }
        app.applyMenuState(snapshot(), guardEnabled: false, guardPhase: .idle)
        check(app.guardPowerItem.title == "恢复文件守卫" && app.guardStatusItem.title.contains("已暂停"), "恢复守卫")
        app.applyMenuState(snapshot(), guardEnabled: true, guardPhase: .acking)
        check(app.guardStatusItem.title.contains("记录变更"), "守卫记录状态")
        app.applyMenuState(snapshot(), guardEnabled: true, guardPhase: .waiting)
        check(app.guardStatusItem.title.contains("等待重新监测"), "守卫重试状态")
        app.isTerminating = true
        app.applyMenuState(snapshot(pid: 123, port: true), guardEnabled: true, guardPhase: .watching)
        check(app.pageItems.allSatisfy { !$0.isEnabled } && !app.quitItem.isEnabled
              && !app.consolePowerItem.isEnabled && !app.guardPowerItem.isEnabled, "退出时禁止新动作")
        app.isTerminating = false

        let first = ConsolePage.dashboard.url(port: 18789)
        let last = ConsolePage.newTask.url(port: 18789)
        do {
            var opened: [URL] = []
            var starts = 0
            let nav = ConsolePageNavigator(isReady: { true }, start: { starts += 1 }, open: { opened.append($0) }, failed: { _ in })
            nav.request(first)
            check(opened == [first] && starts == 0, "已就绪直接打开，不启动新实例")
        }
        do {
            var ready = false
            var opened: [URL] = []
            var starts = 0
            let nav = ConsolePageNavigator(pollInterval: 0.01, isReady: { ready }, start: { starts += 1 }, open: { opened.append($0) }, failed: { _ in })
            nav.request(first)
            check(opened.isEmpty && starts == 1, "未就绪先启动，不提前打开网页")
            ready = true
            spin(0.05)
            check(opened == [first], "就绪后只打开一次")
            spin(0.03)
            check(opened == [first], "不会重复打开网页")
        }
        do {
            var ready = false
            var opened: [URL] = []
            let nav = ConsolePageNavigator(pollInterval: 0.01, isReady: { ready }, start: {}, open: { opened.append($0) }, failed: { _ in })
            nav.request(first)
            nav.request(last)
            ready = true
            spin(0.05)
            check(opened == [last], "连续选择只打开最后一个目标")
            ready = false
            nav.request(first)
            nav.cancel()
            ready = true
            spin(0.05)
            check(opened == [last], "停止或退出后取消待打开页面")
        }
        do {
            var opened: [URL] = []
            var failed: [URL] = []
            let nav = ConsolePageNavigator(timeout: 0.02, pollInterval: 0.01, isReady: { false }, start: {}, open: { opened.append($0) }, failed: { failed.append($0) })
            nav.request(first)
            spin(0.07)
            check(opened.isEmpty && failed == [first], "超时只报告一次，不打开故障页面")
        }
        let report: [String: Any] = [
            "result": failures.isEmpty ? "PASS" : "FAIL", "checks": checks,
            "failures": failures, "menus": stateMenus,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]),
           let json = String(data: data, encoding: .utf8) { print(json) }
        return failures.isEmpty ? 0 : 1
    }
}

// -----------------------------------------------------------------------------
// 入口
// -----------------------------------------------------------------------------

if CommandLine.arguments.contains("--watchdog-selftest") {
    exit(WatchdogSelfTest.run())
}
if CommandLine.arguments.contains("--menu-selftest") {
    exit(AppDelegate.runMenuSelfTest())
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
