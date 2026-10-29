// autorecord_launcher.swift: the executable inside "Whosaid Call Recorder.app",
// built by `whosaid autorecord install --app` (GitHub issue #122).
//
// macOS grants Screen & System Audio Recording and the microphone to an app,
// and the processes it starts inherit that grant. This launcher only runs the
// command it was opened with (open -a App --args <whosaid> autorecord run ...)
// and waits for it, so it never needs rebuilding when the configuration
// changes: the grant is keyed on this binary's code hash, and a rebuild would
// drop it. Keep it generic; mode, paths and flags travel as launch arguments.
//
// SIGTERM / SIGINT (logout, `autorecord uninstall`) are forwarded to the child
// as SIGINT, which makes an in-progress recording finish and save.

import Foundation

let agentPath = [
    "\(NSHomeDirectory())/.local/bin", "\(NSHomeDirectory())/bin",
    "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
].joined(separator: ":")

let argv = Array(CommandLine.arguments.dropFirst())
guard let exe = argv.first else {
    FileHandle.standardError.write("usage: <app> --args <whosaid> autorecord run ...\n".data(using: .utf8)!)
    exit(2)
}

let child = Process()
child.executableURL = URL(fileURLWithPath: exe)
child.arguments = Array(argv.dropFirst())
var env = ProcessInfo.processInfo.environment
env["PATH"] = agentPath
child.environment = env
child.standardInput = FileHandle.nullDevice

signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
var sources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT] {
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { if child.isRunning { kill(child.processIdentifier, SIGINT) } }
    src.resume()
    sources.append(src)
}

child.terminationHandler = { p in exit(p.terminationStatus) }
do {
    try child.run()
} catch {
    FileHandle.standardError.write("cannot start \(exe): \(error.localizedDescription)\n".data(using: .utf8)!)
    exit(1)
}
dispatchMain()
