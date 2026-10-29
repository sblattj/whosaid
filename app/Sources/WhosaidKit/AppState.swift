import Foundation

/// The composed menu bar state from `whosaid app-state` (P1).
public enum MenuState: String, Sendable, CaseIterable {
  case warn, agentNotLoaded = "agent-not-loaded", recordingLive = "recording-live", recording, paused,
       ingesting, waiting, done, idle, unknown
}

public struct AppState: Sendable, Equatable {
  public let raw: JSONValue
  public init(raw: JSONValue) { self.raw = raw }

  public var menu: MenuState { MenuState(rawValue: raw["menubar"]?["state"]?.string ?? "") ?? .unknown }
  public var headline: String { raw["menubar"]?["headline"]?.string ?? "" }
  public var recording: Bool { menu == .recording || menu == .recordingLive }
  public var elapsed: Int? { raw["autorecord"]?["elapsed"]?.int }
  public var autorecordState: String { raw["autorecord"]?["state"]?.string ?? "unknown" }
  public var controlsAvailable: Bool { raw["autorecord"]?["controls"]?.bool ?? false }
  public var pausedUntil: String? { raw["autorecord"]?["paused_until"]?.string }
  public var unnamedVoices: Int { raw["unnamed"]?["voices"]?.int ?? 0 }
  public var liveActive: Bool { raw["live"]?["active"]?.bool ?? false }
  public var lastIngestFolder: String? { raw["last_ingest"]?["folder"]?.string }
  public var watcherInstalled: Bool { raw["watch"]?["installed"]?.bool ?? false }
  public var autorecordInstalled: Bool { raw["autorecord"]?["installed"]?.bool ?? false }
}

/// One long-lived `whosaid app-state --watch N` child (D9); yields a state per JSON line.
public final class AppStateStream: @unchecked Sendable {
  let paths: WhosaidPaths
  let interval: Double
  var process: Process?

  public init(paths: WhosaidPaths, interval: Double = 2) {
    self.paths = paths
    self.interval = interval
  }

  public func states() -> AsyncStream<AppState> {
    AsyncStream { cont in
      let p = Process()
      p.executableURL = URL(fileURLWithPath: paths.whosaid)
      var args = ["app-state", "--watch", String(interval), "--into", paths.recordings]
      if let ws = paths.workspace { args += ["--workspace", ws] }
      p.arguments = args
      p.environment = WhosaidPaths.childEnvironment()
      let out = Pipe()
      p.standardOutput = out
      p.standardError = FileHandle.nullDevice
      p.standardInput = FileHandle.nullDevice
      let buffer = LineBuffer()
      out.fileHandleForReading.readabilityHandler = { h in
        let chunk = h.availableData
        if chunk.isEmpty { h.readabilityHandler = nil; return }
        for line in buffer.feed(chunk) {
          if let v = try? JSONValue.parse(line) { cont.yield(AppState(raw: v)) }
        }
      }
      p.terminationHandler = { _ in cont.finish() }
      cont.onTermination = { _ in if p.isRunning { p.terminate() } }
      do { try p.run(); process = p } catch { cont.finish() }
    }
  }

  public func stop() { process?.terminate() }
}

final class LineBuffer: @unchecked Sendable {
  var data = Data()
  let lock = NSLock()
  func feed(_ chunk: Data) -> [Data] {
    lock.lock(); defer { lock.unlock() }
    data.append(chunk)
    var lines: [Data] = []
    while let nl = data.firstIndex(of: 0x0A) {
      lines.append(Data(data[data.startIndex..<nl]))
      data = Data(data[data.index(after: nl)...])
    }
    return lines
  }
}
