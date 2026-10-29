import Foundation

public struct CLIResult: Sendable {
  public let status: Int32
  public let stdout: String
  public let stderr: String
  public var json: JSONValue? { try? JSONValue.parse(Data(stdout.utf8)) }
}

/// Runs `whosaid <args>` once with a deadline (D2: the CLI's --json commands).
public struct CLIRunner: Sendable {
  public let whosaid: String
  public init(whosaid: String) { self.whosaid = whosaid }

  public func run(_ args: [String], timeout: Double = 30, env: [String: String] = [:]) async throws -> CLIResult {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: whosaid)
    p.arguments = args
    p.environment = WhosaidPaths.childEnvironment().merging(env) { $1 }
    let out = Pipe(), err = Pipe()
    p.standardOutput = out
    p.standardError = err
    p.standardInput = FileHandle.nullDevice
    try p.run()
    let killer = Task {
      try? await Task.sleep(for: .seconds(timeout))
      if p.isRunning { p.terminate() }
    }
    defer { killer.cancel() }
    async let o = Task.detached { out.fileHandleForReading.readDataToEndOfFile() }.value
    async let e = Task.detached { err.fileHandleForReading.readDataToEndOfFile() }.value
    let (od, ed) = await (o, e)
    p.waitUntilExit()
    if p.terminationReason == .uncaughtSignal && !killer.isCancelled { throw WhosaidError.timeout(args.first ?? "") }
    return CLIResult(status: p.terminationStatus, stdout: String(decoding: od, as: UTF8.self),
                     stderr: String(decoding: ed, as: UTF8.self))
  }
}
