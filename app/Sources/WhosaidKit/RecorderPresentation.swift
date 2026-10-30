import Foundation

/// The Recorder page for one `whosaid app-state` snapshot: what autorecord is doing,
/// how it is set up, and the one command that changes the setup (the app never
/// installs launch agents itself).
public struct RecorderPresentation: Equatable, Sendable {
  public var installed = false
  public var running = false
  public var state = "unknown"
  /// "Teams (com.microsoft.teams2)" or "Meetings in the browser".
  public var source = ""
  public var into = ""
  public var live = false
  public var lastSaved: String?
  public var lastSavedAt: String?
  public var lastError: String?
  public var watcherInstalled = false
  /// Shown when live is off (or autorecord is not installed): the command that turns it on.
  public var command: String?

  public init(_ s: AppState?) {
    guard let s, let ar = s.raw["autorecord"] else { return }
    installed = ar["installed"]?.bool ?? false
    running = ar["running"]?.bool ?? false
    state = ar["state"]?.string ?? "unknown"
    into = ar["into"]?.string ?? ""
    live = ar["live"]?.bool ?? false
    watcherInstalled = s.watcherInstalled
    let app = ar["app"]?.string
    let browser = ar["mode"]?.string == "browser"
    source = browser ? "Meetings in the browser" : app.map { "\(MenuPresentation.appName($0)) (\($0))" } ?? "Not set up"
    if let saved = ar["last_saved"]?["path"]?.string {
      lastSaved = URL(fileURLWithPath: saved).lastPathComponent
      lastSavedAt = ar["last_saved"]?["at"]?.string
    }
    if let e = ar["last_error"], let rc = e["rc"]?.int {
      lastError = (e["kind"]?.string == "permission" ? "No Screen & System Audio Recording permission" : "The recorder failed")
        + " (exit \(rc))"
    }
    if !installed || !live {
      var args = ["whosaid", "autorecord", "install"]
      if browser { args.append("--browser") } else { args += ["--app", app ?? "com.microsoft.teams2"] }
      if !into.isEmpty { args += ["--into", Self.tilde(into)] }
      if let label = ar["label"]?.string { args += ["--label", label] }
      args.append("--live")
      command = args.map(Self.quote).joined(separator: " ")
    }
  }

  /// The last `n` lines of `<into>/.autorecord.log`, without the timestamp's date.
  public static func logTail(into: String, lines n: Int = 12) -> [String] {
    let url = URL(fileURLWithPath: into).appendingPathComponent(".autorecord.log")
    guard let h = try? FileHandle(forReadingFrom: url) else { return [] }
    defer { try? h.close() }
    let size = (try? h.seekToEnd()) ?? 0
    try? h.seek(toOffset: size > 16_384 ? size - 16_384 : 0)
    let text = String(decoding: h.readDataToEndOfFile(), as: UTF8.self)
    return text.split(separator: "\n").suffix(n).map { line in
      // "2026-10-29 10:00:01 text" -> "10:00:01 text"
      line.count > 11 && line.dropFirst(4).first == "-" ? String(line.dropFirst(11)) : String(line)
    }
  }

  static func tilde(_ path: String) -> String {
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    return path.hasPrefix(home + "/") ? "~" + path.dropFirst(home.count) : path
  }

  static func quote(_ s: String) -> String {
    s.allSatisfy({ $0.isLetter || $0.isNumber || "-_./~:=".contains($0) }) ? s : "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
  }
}
