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
  let live = LiveModel()

  // Library (P4)
  var meetings: [MeetingSummary] = []
  var meetingsLoaded = false
  var selectedMeeting: String?
  var meetingTab: MeetingTab = .transcript
  var folderInfo: [String: FolderInfo] = [:]
  /// Bumped after a relabel: meeting views reload their files on it.
  var revision = 0
  /// "folder|cluster" -> the generated clip, made on the first click of its play button.
  var clips: [String: URL] = [:]
  var worklist: [TrackedItem] = []
  var items: [TrackedItem] = []
  var suggestions: [CompletionSuggestion] = []
  var trackingError: String?
  /// A source link opened a meeting at this second: the transcript scrolls to it.
  var focus: (folder: String, t: Double)?
  var people: [JSONValue] = []
  var peopleLoaded = false
  var searchOpen = false
  var searchQuery = ""
  var searching = false
  var searchHits: [JSONValue] = []
  var searchPick: Int?
  var searchContext: [JSONValue] = []
  var searchError: String?

  init(paths: WhosaidPaths = .resolve()) {
    self.paths = paths
    AppModelLocator.model = self
  }

  func start() {
    if let fixture = ProcessInfo.processInfo.environment["WHOSAID_APP_SELFTEST_STATE"].flatMap({ FileManager.default.contents(atPath: $0) }),
       let v = try? JSONValue.parse(fixture) {
      apply(AppState(raw: v))  // self-test: a fixture snapshot instead of the live stream
      live.attach(paths: paths)
      Task { await refreshHealth() }
      return
    }
    restartStream()
    watchWorkspace()
    live.attach(paths: paths)
    Task { await refreshHealth() }
  }

  /// A new meeting folder or a fresh index shows up without a relaunch.
  /// Opened off the main thread: the first open of a protected folder (~/Desktop) waits
  /// on the macOS privacy prompt, and the window must stay responsive meanwhile.
  func watchWorkspace() {
    watcher?.cancel()
    watcher = nil
    guard let ws = paths.workspace else { return }
    Task {
      let w = await Task.detached {
        FolderWatcher(path: ws) { Task { @MainActor in await AppModelLocator.model?.refreshHealth() } }
      }.value
      if paths.workspace == ws { watcher = w } else { w?.cancel() }
    }
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
    live.syncFollower(callLive: st.liveActive)
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

  func loadMeetings(force: Bool = false) async {
    guard let ws = paths.workspace, let mcp, force || !meetingsLoaded else { return }
    let r = try? await mcp.call("whosaid_meetings", ["workspace": .string(ws), "limit": .number(500), "compact": .bool(false)],
                                timeout: 60)
    meetings = (r?["meetings"]?.array ?? []).compactMap(MeetingSummary.init)
    meetingsLoaded = true
  }

  /// Speakers, unnamed voices and items per list row, read from the folder once.
  func loadFolderInfo(_ folder: String) {
    guard folderInfo[folder] == nil, let ws = paths.workspace else { return }
    folderInfo[folder] = FolderInfo(speakers: 0, unnamed: 0, items: 0)
    let mf = MeetingFolder(workspace: ws, folder: folder)
    Task {
      let info = await Task.detached { () -> FolderInfo in
        let speakers = Transcript.speakers(mf.turns())
        return FolderInfo(speakers: speakers.count, unnamed: speakers.filter(Transcript.isUnnamed).count,
                          items: mf.items().count, chatName: mf.chatName())
      }.value
      folderInfo[folder] = info
    }
  }

  /// A chat's own name once its folder is read, else the summary's title.
  func title(_ m: MeetingSummary) -> String { folderInfo[m.folder]?.chatName ?? m.title }

  func loadPeople() async {
    guard let ws = paths.workspace, let mcp, !peopleLoaded else { return }
    let r = try? await mcp.call("whosaid_person", ["workspace": .string(ws)], timeout: 60)
    people = (r?["people"]?.array ?? []).sorted { ($0["turns"]?.int ?? 0) > ($1["turns"]?.int ?? 0) }
    peopleLoaded = true
  }

  func person(_ name: String) async -> JSONValue? {
    guard let ws = paths.workspace, let mcp else { return nil }
    return try? await mcp.call("whosaid_person", ["workspace": .string(ws), "name": .string(name)], timeout: 60)
  }

  func runSearch() async {
    let q = searchQuery.trimmingCharacters(in: .whitespaces)
    guard !q.isEmpty, let ws = paths.workspace, let mcp else { return }
    searching = true
    defer { searching = false }
    searchError = nil
    do {
      let r = try await mcp.call("whosaid_search", ["workspace": .string(ws), "query": .string(q), "k": .number(25)], timeout: 60)
      searchHits = r["hits"]?.array ?? []
      if r["ok"]?.bool == false { searchError = r["error"]?.string ?? "search failed" }
      searchPick = searchHits.isEmpty ? nil : 0
    } catch {
      searchError = "search failed: \(error)"
    }
  }

  func loadContext() async {
    guard let i = searchPick, i < searchHits.count, let ws = paths.workspace, let mcp,
          let meeting = searchHits[i]["meeting"]?.string, let at = searchHits[i]["t_str"]?.string else {
      searchContext = []; return
    }
    let r = try? await mcp.call("whosaid_context", ["workspace": .string(ws), "meeting": .string(meeting), "at": .string(at)],
                                timeout: 30)
    searchContext = r?["turns"]?.array ?? []
  }

  private var cli: CLIRunner { CLIRunner(whosaid: paths.whosaid) }

  /// Names one voice of a meeting: `whosaid_relabel` (registry + transcript), then
  /// `whosaid index` so People, search and the unnamed count catch up. Returns the
  /// error text, or nil on success. force saves past the low-similarity guard.
  func relabel(folder: String, cluster: String, name: String, role: String?, force: Bool = false) async -> String? {
    guard let ws = paths.workspace, let mcp,
          let side = MeetingFolder(workspace: ws, folder: folder).sidecarURL() else { return "No sidecar for this meeting" }
    var args: [String: JSONValue] = ["base": .string(side.path), "assignments": .object([cluster: .string(name)])]
    if let role { args["roles"] = .object([name: .string(role)]) }
    if force { args["force"] = .bool(true) }
    let r: JSONValue
    do { r = try await mcp.call("whosaid_relabel", args, timeout: 120) } catch { return "relabel failed: \(error)" }
    if r["ok"]?.bool == false { return r["error"]?.string ?? "relabel failed" }
    let extra = ProcessInfo.processInfo.environment["WHOSAID_APP_INDEX_ARGS"].map { $0.split(separator: " ").map(String.init) } ?? []
    let ix = try? await cli.run(["index", ws] + extra, timeout: 900)
    folderInfo[folder] = nil
    peopleLoaded = false
    revision += 1
    await loadPeople()
    await refreshHealth()
    return ix?.status == 0 ? nil : "Saved; the search index did not refresh: " + (ix?.stderr.split(separator: "\n").last.map(String.init) ?? "no answer")
  }

  /// The clip for one voice, made with `whosaid_samples` into the clips folder (outside
  /// the workspace). Only a clip button calls this, right before it plays.
  func clip(folder: String, cluster: String) async -> URL? {
    let key = folder + "|" + cluster
    if let u = clips[key], FileManager.default.fileExists(atPath: u.path) { return u }
    guard let ws = paths.workspace, let mcp,
          let side = MeetingFolder(workspace: ws, folder: folder).sidecarURL() else { return nil }
    let out = WhosaidPaths.clipsDir.appendingPathComponent(folder)
    try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
    let r = try? await mcp.call("whosaid_samples", ["base": .string(side.path), "outdir": .string(out.path)], timeout: 120)
    for s in r?["samples"]?.array ?? [] {
      if let c = s["cluster"]?.string, let path = s["path"]?.string { clips[folder + "|" + c] = URL(fileURLWithPath: path) }
    }
    return clips[key]
  }

  /// My commitments (the ranked worklist for the workspace owner) and its suggestions.
  func loadWorklist() async {
    guard let ws = paths.workspace, let mcp else { return }
    trackingError = nil
    do {
      let r = try await mcp.call("whosaid_worklist", ["owner": .string("me"), "limit": .number(500), "workspace": .string(ws)], timeout: 90)
      if r["ok"]?.bool == false { trackingError = r["error"]?.string ?? "worklist failed" }
      worklist = (r["items"]?.array ?? []).compactMap(TrackedItem.init)
      let s = try await mcp.call("whosaid_tracking_suggestions", ["owner": .string("me"), "workspace": .string(ws)], timeout: 90)
      suggestions = (s["items"]?.array ?? []).compactMap(CompletionSuggestion.init)
    } catch { trackingError = "whosaid is not answering: \(error)" }
  }

  func loadItems() async {
    guard let ws = paths.workspace, let mcp else { return }
    let r = try? await mcp.call("whosaid_items", ["limit": .number(500), "workspace": .string(ws)], timeout: 90)
    items = (r?["items"]?.array ?? []).compactMap(TrackedItem.init)
  }

  /// The P1 write path: one curated status event in the tracking ledger. Only an explicit
  /// Mark done button calls this; suggestions never change anything on their own.
  func markDone(_ id: String, note: String) async -> String? {
    guard let ws = paths.workspace, let mcp else { return "No workspace" }
    let r: JSONValue
    do {
      r = try await mcp.call("whosaid_set_status", ["item_id": .string(id), "status": .string("done"),
                                                     "note": .string(note), "workspace": .string(ws)], timeout: 60)
    } catch { return "Mark done failed: \(error)" }
    if r["ok"]?.bool == false { return r["error"]?.string ?? "Mark done failed" }
    await loadWorklist()
    await loadItems()
    return nil
  }

  /// Opens the meeting an item came from, at the moment it was said.
  func openSource(meeting: String, t: Double?) {
    let chat = meetings.first { $0.folder == meeting }?.isChat ?? false
    selection = chat ? .chats : .meetings
    selectedMeeting = meeting
    meetingTab = .transcript
    focus = t.map { (meeting, $0) }
  }

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

/// One row of "Last meetings": a title (see MeetingSummary.title) and the folder's date.
struct MeetingRow: Identifiable, Equatable {
  let summary: MeetingSummary
  var folder: String { summary.folder }
  var id: String { folder }
  var title: String { summary.title }

  init?(_ v: JSONValue) {
    guard let m = MeetingSummary(v) else { return nil }
    summary = m
  }

  /// 2026-10-28-1400 -> "Oct 28 14:00"
  var when: String {
    let parts = folder.split(separator: "-", maxSplits: 4).map(String.init)
    guard parts.count >= 4, parts[3].count == 4 else { return "" }
    let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    let m = (Int(parts[1]) ?? 1) - 1
    return "\(months[max(0, min(11, m))]) \(Int(parts[2]) ?? 0) \(parts[3].prefix(2)):\(parts[3].suffix(2))"
  }
}

struct FolderInfo: Equatable {
  let speakers: Int
  let unnamed: Int
  let items: Int
  var chatName: String? = nil
}
