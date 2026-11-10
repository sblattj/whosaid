import SwiftUI
import WebKit
import WhosaidKit

extension AppModel {
  /// The drafts outbox needs no workspace: a client of its own when the workspace one isn't up.
  var draftsClient: MCPClient {
    if let mcp { return mcp }
    let c = MCPClient(whosaid: URL(fileURLWithPath: paths.whosaid), workspace: paths.workspace)
    mcp = c
    return c
  }

  func loadDrafts() async {
    guard FileManager.default.isExecutableFile(atPath: paths.whosaid) else { draftsError = "whosaid not found at \(paths.whosaid)"; return }
    do {
      let r = try await draftsClient.call("whosaid_drafts", ["action": .string("list")], timeout: 30)
      if r["ok"]?.bool == false { draftsError = r["error"]?.string ?? "drafts failed"; return }
      let rows = (r["drafts"]?.array ?? []).compactMap(DraftRow.init)
      draftsError = nil
      if rows != drafts { drafts = rows.sorted { $0.updatedAt > $1.updatedAt } }
    } catch { draftsError = "whosaid is not answering: \(error)" }
  }

  func loadDraft(_ id: String) async {
    guard let r = try? await draftsClient.call("whosaid_drafts", ["action": .string("show"), "id": .string(id)], timeout: 30),
          let row = DraftRow(r) else { return }
    if draftDetail[id] != row { draftDetail[id] = row }
  }

  /// One draft (id) or every verified one in order (queue). The stager pastes into Teams and never sends.
  func stage(_ id: String?) async {
    staging = id ?? "*"
    defer { staging = nil }
    var args: [String: JSONValue] = id.map { ["id": .string($0)] } ?? ["all": .bool(true), "queue": .bool(true)]
    if let ws = paths.workspace { args["workspace"] = .string(ws) }
    let key = id ?? "*"
    do {
      // a stage drives the browser: up to ~5 minutes a draft
      let r = try await draftsClient.call("whosaid_draft_stage", args, timeout: id == nil ? 1800 : 330)
      stageOutcome[key] = StageOutcome(r)
    } catch {
      stageOutcome[key] = StageOutcome(.object(["ok": .bool(false), "error": .string("whosaid is not answering: \(error)")]))
    }
    await loadDrafts()
    if let id { await loadDraft(id) }
  }

  /// Keeps the sidebar badge current: a new draft folder or a stage (its lock) changes ~/.drafts.
  func watchDrafts() {
    let dir = DraftsFolder.url().path
    Task {
      let w = await Task.detached {
        FolderWatcher(path: dir) { Task { @MainActor in await AppModelLocator.model?.loadDrafts() } }
      }.value
      if draftsWatcher == nil { draftsWatcher = w } else { w?.cancel() }
      while true {
        await loadDrafts()
        try? await Task.sleep(for: .seconds(30))
      }
    }
  }
}

/// Drafts: the outbox's open drafts, each with its message and attached pages, and Stage in Teams.
struct DraftsView: View {
  @Environment(AppModel.self) var model
  @State private var pick: String?

  var body: some View {
    HSplitView {
      list.frame(minWidth: 260, idealWidth: 300, maxWidth: 380)
      detail.frame(minWidth: 480, maxWidth: .infinity)
    }
    .task {
      while !Task.isCancelled {
        await model.loadDrafts()
        if pick == nil || !model.drafts.contains(where: { $0.id == pick }) { pick = model.drafts.first?.id }
        if let pick { await model.loadDraft(pick) }
        try? await Task.sleep(for: .seconds(5))
      }
    }
  }

  var list: some View {
    VStack(alignment: .leading, spacing: 0) {
      HStack {
        Text("\(model.drafts.filter(\.stageable).count) ready").font(.callout).foregroundStyle(.secondary)
        Spacer()
        Button {
          Task { await model.stage(nil) }
        } label: { Label("Stage all ready", systemImage: "tray.and.arrow.up") }
          .disabled(model.staging != nil || !model.drafts.contains(where: \.stageable))
          .help("Stage every verified draft into its Teams chat, oldest first. Nothing is sent.")
      }
      .padding(10)
      if let out = model.stageOutcome["*"] { OutcomeLines(outcome: out).padding(.horizontal, 10).padding(.bottom, 8) }
      Divider()
      List(model.drafts, selection: $pick) { d in
        VStack(alignment: .leading, spacing: 3) {
          Text(d.to).font(.headline)
          Text(d.topic).font(.callout).lineLimit(2)
          StateChip(row: d)
        }
        .padding(.vertical, 3)
        .tag(d.id)
      }
      if let err = model.draftsError { Text(err).foregroundStyle(.orange).padding(10) }
      if model.drafts.isEmpty && model.draftsError == nil {
        Text("No open drafts").foregroundStyle(.secondary).frame(maxWidth: .infinity).padding(.top, 30)
      }
    }
  }

  @ViewBuilder var detail: some View {
    if let pick, let row = model.draftDetail[pick] ?? model.drafts.first(where: { $0.id == pick }) {
      DraftDetail(row: row)
    } else {
      EmptyState(symbol: "paperplane", title: "Drafts", message: "Pick a draft to preview it.", action: nil)
    }
  }
}

struct StateChip: View {
  let row: DraftRow
  var color: Color { row.stageable ? .green : row.staged ? .blue : .orange }
  var body: some View {
    Text(row.rivals > 0 ? "\(row.rivals + 1) drafts to this person, merge first" : row.stateLabel)
      .font(.caption.weight(.semibold)).foregroundStyle(color)
      .padding(.horizontal, 6).padding(.vertical, 2)
      .background(color.opacity(0.15), in: Capsule())
  }
}

struct OutcomeLines: View {
  let outcome: StageOutcome
  var body: some View {
    VStack(alignment: .leading, spacing: 2) {
      ForEach(Array(outcome.lines.enumerated()), id: \.offset) { _, l in
        Text(l).font(.callout).foregroundStyle(outcome.ok ? Color.secondary : .orange).textSelection(.enabled)
      }
    }
  }
}

struct DraftDetail: View {
  @Environment(AppModel.self) var model
  let row: DraftRow
  @State private var page = 0

  var body: some View {
    VStack(alignment: .leading, spacing: 10) {
      HStack(alignment: .firstTextBaseline) {
        VStack(alignment: .leading, spacing: 2) {
          Text("To \(row.to)").font(.title3.weight(.semibold))
          if !row.mentions.isEmpty { Text("@" + row.mentions.joined(separator: ", @")).font(.callout).foregroundStyle(.secondary) }
        }
        Spacer()
        StateChip(row: row)
        Button {
          Task { await model.stage(row.id) }
        } label: {
          if model.staging == row.id || model.staging == "*" { ProgressView().controlSize(.small) }
          else { Label(row.staged ? "Staged" : "Stage in Teams", systemImage: "paperplane.fill") }
        }
        .buttonStyle(.borderedProminent)
        .disabled(!row.stageable || model.staging != nil)
        .help("Paste this message and its pages into the Teams chat and focus it. You press send in Teams.")
      }
      if let out = model.stageOutcome[row.id] { OutcomeLines(outcome: out) }
      if !row.gate.isEmpty {
        Text("Gates: " + row.gate.joined(separator: "; ")).font(.callout).foregroundStyle(.orange)
      }
      GroupBox {
        ScrollView {
          Text(markdown(row.text ?? "(loading)")).textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading).padding(4)
        }
        .frame(minHeight: 60, maxHeight: 180)
      } label: { Text("Message").font(.caption).foregroundStyle(.secondary) }
      if !row.pages.isEmpty {
        if row.pages.count > 1 {
          Picker("", selection: $page) {
            ForEach(Array(row.pages.enumerated()), id: \.offset) { i, p in Text((p as NSString).lastPathComponent).tag(i) }
          }
          .pickerStyle(.segmented).labelsHidden()
        } else {
          Text((row.pages[0] as NSString).lastPathComponent).font(.caption).foregroundStyle(.secondary)
        }
        PagePreview(path: row.pages[min(page, row.pages.count - 1)])
          .clipShape(RoundedRectangle(cornerRadius: 6))
          .overlay(RoundedRectangle(cornerRadius: 6).stroke(.separator))
      } else {
        Spacer()
      }
    }
    .padding(16)
    .onChange(of: row.id) { page = 0 }
  }

  func markdown(_ s: String) -> AttributedString {
    (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(s)
  }
}

/// An attached page, read from disk: only file: loads, every other navigation is cancelled (D14).
struct PagePreview: NSViewRepresentable {
  let path: String

  func makeCoordinator() -> LocalOnlyNavigation { LocalOnlyNavigation() }

  func makeNSView(context: Context) -> WKWebView {
    let web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
    web.navigationDelegate = context.coordinator
    load(web)
    return web
  }

  func updateNSView(_ web: WKWebView, context: Context) {
    if web.url?.path != path { load(web) }
  }

  func load(_ web: WKWebView) {
    let u = URL(fileURLWithPath: path)
    guard FileManager.default.fileExists(atPath: path) else {
      web.loadHTMLString("<p style='font:13px -apple-system'>Page not found: \(path)</p>", baseURL: nil)
      return
    }
    web.loadFileURL(u, allowingReadAccessTo: u.deletingLastPathComponent())
  }
}
