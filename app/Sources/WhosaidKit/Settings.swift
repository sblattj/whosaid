import Foundation

/// Where whosaid and the user's folders are. D8: WHOSAID_BIN > saved setting > ~/code/whosaid/whosaid.
public struct WhosaidPaths: Sendable, Equatable {
  public var whosaid: String
  public var workspace: String?
  public var recordings: String

  /// The env for whosaid child processes: the app's own, minus WHOSAID_* set to "".
  /// The self-test blanks them (D21), and a blank WHOSAID_SPEAKER_DB would make the
  /// diarizer use the current folder as the voice registry.
  public static func childEnvironment(_ base: [String: String] = ProcessInfo.processInfo.environment) -> [String: String] {
    base.filter { !($0.key.hasPrefix("WHOSAID_") && $0.value.isEmpty) }
  }

  /// Voice clips for the play buttons: WHOSAID_APP_CLIPS_DIR, else ~/Library/Caches/whosaid/clips.
  public static var clipsDir: URL {
    if let d = ProcessInfo.processInfo.environment["WHOSAID_APP_CLIPS_DIR"], !d.isEmpty { return URL(fileURLWithPath: d) }
    return FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("whosaid/clips")
  }

  public static let defaultsKeys = (whosaid: "whosaidPath", workspace: "workspacePath", recordings: "recordingsPath")

  public init(whosaid: String, workspace: String?, recordings: String) {
    self.whosaid = whosaid
    self.workspace = workspace
    self.recordings = recordings
  }

  public static func resolve(env: [String: String] = ProcessInfo.processInfo.environment,
                             defaults: UserDefaults = .standard,
                             home: String = NSHomeDirectory()) -> WhosaidPaths {
    let bin = env["WHOSAID_BIN"].nonEmpty ?? defaults.string(forKey: defaultsKeys.whosaid).nonEmpty
      ?? "\(home)/code/whosaid/whosaid"
    let ws = env["WHOSAID_WORKSPACE"].nonEmpty ?? defaults.string(forKey: defaultsKeys.workspace).nonEmpty
    let rec = env["WHOSAID_RECORDINGS"].nonEmpty ?? defaults.string(forKey: defaultsKeys.recordings).nonEmpty
      ?? "\(home)/Recordings"
    return WhosaidPaths(whosaid: bin, workspace: ws, recordings: rec)
  }
}

extension Optional where Wrapped == String {
  var nonEmpty: String? {
    guard let s = self?.trimmingCharacters(in: .whitespaces), !s.isEmpty else { return nil }
    return (s as NSString).expandingTildeInPath
  }
}
