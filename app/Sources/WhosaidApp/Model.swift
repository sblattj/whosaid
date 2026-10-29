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
  /// Ticks once a second while recording, for the elapsed time in the menu bar.
  var now = Date()
  private var ticker: Timer?
  var recent: [MeetingRow] = []
  private var recentKey: String?
  /// The last menu action's outcome (stop-now, pause), shown under the headline.
  var actionNote: String?
  var menu: MenuPresentation { MenuPresentation(state, now: now) }

  init(paths: WhosaidPaths = .resolve()) {
    self.paths = paths
    AppModelLocator.model = self
  }

  func start() {
    if let fixture = ProcessInfo.processInfo.environment["WHOSAID_APP_SELFTEST_STATE"].flatMap({ FileManager.default.contents(atPath: $0) }),
       let v = try? JSONValue.parse(fixture) {
      apply(AppState(raw: v))  // self-test: a fixture snapshot instead of the live stream
      Task { await refreshHealth() }
      return
    }
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
      for await st in s.states() { self?.apply(st) }
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
        await refreshRecent()
      }
    } catch {
      health = .error("the whosaid MCP server did not answer: \(error)")
    }
  }

  func apply(_ st: AppState) {
    state = st
    let rec = menu.recording
    if rec && ticker == nil {
      ticker = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
        Task { @MainActor in AppModelLocator.model?.now = Date() }
      }
    } else if !rec {
      ticker?.invalidate()
      ticker = nil
    }
    now = Date()
    let key = st.lastIngestFolder ?? ""
    if key != recentKey {
      recentKey = key
      Task { await refreshRecent() }
    }
  }

  /// The three newest meetings for the menu (whosaid_meetings, newest first).
  func refreshRecent() async {
    guard let ws = paths.workspace, case .ready = health, let mcp else { recent = []; return }
    let r = try? await mcp.call("whosaid_meetings", ["workspace": .string(ws), "limit": .number(3), "compact": .bool(true)])
    recent = (r?["meetings"]?.array ?? []).compactMap { MeetingRow($0) }
  }

  private var cli: CLIRunner { CLIRunner(whosaid: paths.whosaid) }

  func stopNow() async {
    actionNote = "Saving the call…"
    let r = try? await cli.run(["autorecord", "stop-now", "--into", paths.recordings, "--wait", "60"], timeout: 75)
    actionNote = r?.status == 0 ? "Saved " + ((r?.stdout ?? "").split(separator: "/").last.map(String.init) ?? "")
      : "Stop failed: " + (r?.stderr.split(separator: "\n").last.map(String.init) ?? "no answer")
  }

  func pause(minutes: Int) async {
    let r = try? await cli.run(["autorecord", "pause", "--minutes", String(minutes), "--into", paths.recordings])
    actionNote = r?.status == 0 ? nil : "Pause failed: " + (r?.stderr.split(separator: "\n").last.map(String.init) ?? "no answer")
  }

  func resume() async {
    let r = try? await cli.run(["autorecord", "resume", "--into", paths.recordings])
    actionNote = r?.status == 0 ? nil : "Resume failed"
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

/// One row of "Last meetings": the folder name split into a date and a title.
struct MeetingRow: Identifiable, Equatable {
  let folder: String
  var id: String { folder }

  init?(_ v: JSONValue) {
    guard let f = v["folder"]?.string else { return nil }
    folder = f
  }

  /// 2026-10-28-1400-planning -> ("planning", "Oct 28 14:00")
  var title: String {
    let parts = folder.split(separator: "-", maxSplits: 4).map(String.init)
    guard parts.count == 5, parts[0].count == 4 else { return folder }
    return parts[4].replacingOccurrences(of: "-", with: " ").replacingOccurrences(of: "_", with: " ")
  }

  var when: String {
    let parts = folder.split(separator: "-", maxSplits: 4).map(String.init)
    guard parts.count >= 4, parts[3].count == 4 else { return "" }
    let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    let m = (Int(parts[1]) ?? 1) - 1
    return "\(months[max(0, min(11, m))]) \(Int(parts[2]) ?? 0) \(parts[3].prefix(2)):\(parts[3].suffix(2))"
  }
}
