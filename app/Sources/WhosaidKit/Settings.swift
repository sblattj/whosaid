import Foundation

/// Where whosaid and the user's folders are. D8: WHOSAID_BIN > saved setting > ~/code/whosaid/whosaid.
public struct WhosaidPaths: Sendable, Equatable {
  public var whosaid: String
  public var workspace: String?
  public var recordings: String

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
