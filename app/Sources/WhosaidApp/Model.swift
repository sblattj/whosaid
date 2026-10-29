import SwiftUI
import WhosaidKit

enum SidebarItem: String, CaseIterable, Identifiable, Hashable {
  case meetings, live, actionItems, commitments, topics, chats, people, unnamed, recorder
  var id: String { rawValue }
  var title: String {
    switch self {
    case .meetings: "Meetings"
    case .live: "Live now"
    case .actionItems: "Action items"
    case .commitments: "My commitments"
    case .topics: "Topics"
    case .chats: "Chats"
    case .people: "People"
    case .unnamed: "Unnamed voices"
    case .recorder: "Recorder"
    }
  }
  var symbol: String {
    switch self {
    case .meetings: "rectangle.stack"
    case .live: "dot.radiowaves.left.and.right"
    case .actionItems: "checklist"
    case .commitments: "hand.raised"
    case .topics: "circle.hexagongrid"
    case .chats: "bubble.left.and.bubble.right"
    case .people: "person.2"
    case .unnamed: "person.crop.circle.badge.questionmark"
    case .recorder: "record.circle"
    }
  }
  static let library: [SidebarItem] = [.meetings, .live, .actionItems, .commitments, .topics, .chats]
  static let peopleGroup: [SidebarItem] = [.people, .unnamed]
  static let recorderGroup: [SidebarItem] = [.recorder]
}

/// What the main window knows about the workspace (G8 empty states).
enum WorkspaceHealth: Equatable {
  case unknown, notConfigured, missing(String), noIndex(String), ready(meetings: Int), error(String)

  var summary: String {
    switch self {
    case .unknown: "checking"
    case .notConfigured: "no workspace set"
    case .missing: "workspace folder missing"
    case .noIndex: "not indexed yet"
    case .ready(let n): "\(n) meeting folder(s)"
    case .error: "whosaid did not answer"
    }
  }
}

@MainActor @Observable
final class AppModel {
  var paths: WhosaidPaths
  var selection: SidebarItem? = .meetings
  var state: AppState?
  var health: WorkspaceHealth = .unknown
  var mcp: MCPClient?
  private var stream: AppStateStream?
  private var streamTask: Task<Void, Never>?
  private var watcher: FolderWatcher?

  init(paths: WhosaidPaths = .resolve()) {
    self.paths = paths
    AppModelLocator.model = self
  }

  func start() {
    restartStream()
    watchWorkspace()
    Task { await refreshHealth() }
  }

  /// A new meeting folder or a fresh index shows up without a relaunch.
  func watchWorkspace() {
    watcher?.cancel()
    watcher = paths.workspace.flatMap { FolderWatcher(path: $0) { Task { @MainActor in await AppModelLocator.model?.refreshHealth() } } }
  }

  func restartStream() {
    streamTask?.cancel()
    stream?.stop()
    guard FileManager.default.isExecutableFile(atPath: paths.whosaid) else { return }
    let s = AppStateStream(paths: paths)
    stream = s
    streamTask = Task { [weak self] in
      for await st in s.states() { self?.state = st }
    }
  }

  func refreshHealth() async {
    guard let ws = paths.workspace else { health = .notConfigured; return }
    var isDir: ObjCBool = false
    guard FileManager.default.fileExists(atPath: ws, isDirectory: &isDir), isDir.boolValue else { health = .missing(ws); return }
    guard FileManager.default.isExecutableFile(atPath: paths.whosaid) else { health = .error("whosaid not found at \(paths.whosaid)"); return }
    let client = mcp ?? MCPClient(whosaid: URL(fileURLWithPath: paths.whosaid), workspace: ws)
    mcp = client
    do {
      let r = try await client.call("whosaid_workspace_status", ["workspace": .string(ws)])
      let meetings = r["meeting_folders"]?.int ?? 0
      if r["index_present"]?.bool == false || r["search_error"] != nil {
        health = .noIndex("whosaid index \(ws)")
      } else {
        health = .ready(meetings: meetings)
      }
    } catch {
      health = .error("the whosaid MCP server did not answer: \(error)")
    }
  }

  func setWorkspace(_ path: String?) {
    paths.workspace = path
    UserDefaults.standard.set(path, forKey: WhosaidPaths.defaultsKeys.workspace)
    Task { await mcp?.stop() }
    mcp = nil
    restartStream()
    watchWorkspace()
    Task { await refreshHealth() }
  }

  func setWhosaid(_ path: String) {
    paths.whosaid = path
    UserDefaults.standard.set(path, forKey: WhosaidPaths.defaultsKeys.whosaid)
    Task { await mcp?.stop() }
    mcp = nil
    restartStream()
    Task { await refreshHealth() }
  }

  func setRecordings(_ path: String) {
    paths.recordings = path
    UserDefaults.standard.set(path, forKey: WhosaidPaths.defaultsKeys.recordings)
    restartStream()
  }
}
