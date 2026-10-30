import SwiftUI
import WhosaidKit

/// Topics: `<ws>/_topics.json` (`whosaid topics build`), ranked by meetings and recency.
/// A topic shows its wordgraph and the meetings it came up in; a meeting opens at the
/// transcript where the topic starts.
struct TopicsView: View {
  @Environment(AppModel.self) var model
  @State private var library: TopicsLibrary?
  @State private var loaded = false
  @State private var selected: String?
  @State private var building = false
  @State private var note: String?
  @State private var watcher: FolderWatcher?

  var body: some View {
    Group {
      if let ws = model.paths.workspace {
        if let lib = library, !lib.topics.isEmpty {
          content(lib, ws: ws)
        } else if loaded {
          EmptyState(symbol: "circle.hexagongrid", title: library == nil ? "No topics yet" : "No topics found",
                     message: (note.map { $0 + "\n\n" } ?? "")
                       + "Topics roll up what each meeting was about, across meetings. Build them here or run "
                       + "whosaid topics build \(ws)",
                     action: (building ? "Building…" : "Build topics", { Task { await rebuild(ws) } }))
        } else {
          Color.clear  // an empty Group would never run .task
        }
      } else {
        EmptyState(symbol: "folder.badge.questionmark", title: "No workspace set",
                   message: "Pick your meeting workspace in Settings to see its topics.", action: nil)
      }
    }
    .task(id: model.paths.workspace) {
      guard let ws = model.paths.workspace else { return }
      await reload(ws)
      // a rebuild from the terminal (or the watcher) shows up without a relaunch
      watcher?.cancel()  // the previous workspace's watcher
      let w = await Task.detached {
        FolderWatcher(path: ws) { Task { @MainActor in await AppModelLocator.model?.topicsChanged() } }
      }.value
      // the page closed (or the workspace moved on) while the folder opened
      if Task.isCancelled { w?.cancel() } else { watcher = w }
    }
    .onChange(of: model.topicsStamp) { _, _ in
      if let ws = model.paths.workspace { Task { await reload(ws) } }
    }
    .onDisappear { watcher?.cancel(); watcher = nil }
  }

  func content(_ lib: TopicsLibrary, ws: String) -> some View {
    HStack(spacing: 0) {
      VStack(spacing: 0) {
        List(lib.topics, selection: $selected) { t in TopicRow(topic: t).tag(t.id) }
        Divider()
        HStack {
          Text("\(lib.topics.count) topics from \(lib.meetingCount) meetings")
            .font(.caption).foregroundStyle(.secondary)
          Spacer()
          Button(building ? "Building…" : "Rebuild") { Task { await rebuild(ws) } }
            .controlSize(.small).disabled(building)
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        if let note { Text(note).font(.caption).foregroundStyle(.secondary).padding([.horizontal, .bottom], 12) }
      }
      .frame(width: 300)
      Divider()
      if let id = selected, let t = lib.topics.first(where: { $0.id == id }) {
        TopicDetail(topic: t, graph: lib.graphJSON(for: id)).id(id)
      } else {
        Text("Pick a topic").foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
      }
    }
  }

  func reload(_ ws: String) async {
    let lib = await Task.detached { TopicsLibrary.read(workspace: ws) }.value
    if lib != library { library = lib }
    loaded = true
    if selected.map({ id in lib?.topics.contains { $0.id == id } }) != true { selected = lib?.topics.first?.id }
  }

  func rebuild(_ ws: String) async {
    building = true
    note = await model.buildTopics()
    building = false
    await reload(ws)
  }
}

struct TopicRow: View {
  let topic: LibraryTopic

  var body: some View {
    VStack(alignment: .leading, spacing: 3) {
      HStack {
        Text(topic.label).font(.headline).lineLimit(1)
        Spacer()
        Text("\(topic.meetingCount)").font(.caption.monospacedDigit().weight(.semibold))
          .padding(.horizontal, 6).padding(.vertical, 1)
          .background(.quaternary, in: Capsule())
          .help("\(topic.meetingCount) meeting\(topic.meetingCount == 1 ? "" : "s")")
      }
      Text("Last " + TopicsLibrary.dateLabel(topic.lastDate)).font(.caption).foregroundStyle(.secondary)
      Text(topic.terms.prefix(5).joined(separator: " · ")).font(.caption).foregroundStyle(.tertiary).lineLimit(1)
    }
    .padding(.vertical, 3)
  }
}

struct TopicDetail: View {
  @Environment(AppModel.self) var model
  let topic: LibraryTopic
  let graph: String?

  var body: some View {
    VStack(alignment: .leading, spacing: 0) {
      VStack(alignment: .leading, spacing: 4) {
        Text(topic.label).font(.title2.weight(.semibold))
        Text(summary).foregroundStyle(.secondary)
        Text(topic.terms.joined(separator: " · ")).font(.callout).foregroundStyle(.tertiary).lineLimit(2)
      }
      .padding(16)
      Divider()
      WordgraphView(insightsJSON: graph, motion: "lively", maxDepth: -1)
        .frame(minHeight: 280, maxHeight: .infinity)
        .layoutPriority(1)  // the graph takes the spare height: fit() keeps ~200 pt of a narrow view for padding
        .background(Color(red: 0.063, green: 0.071, blue: 0.086))
      Divider()
      VStack(alignment: .leading, spacing: 6) {
        Text("Meetings").font(.headline)
        ScrollView {
          VStack(alignment: .leading, spacing: 2) {
            ForEach(topic.meetings) { m in
              Button { model.openSource(meeting: m.folder, t: m.firstT) } label: {
                HStack(spacing: 10) {
                  Text(TopicsLibrary.dateLabel(m.date).isEmpty ? m.folder : TopicsLibrary.dateLabel(m.date))
                    .frame(width: 110, alignment: .leading)
                  Text(m.label).lineLimit(1)
                  Spacer()
                  Text(TopicsLibrary.duration(m.speakingMs) + " talked").foregroundStyle(.secondary).monospacedDigit()
                  Label("at " + TopicsLibrary.duration(Int(m.firstT * 1000)), systemImage: "text.alignleft")
                    .foregroundStyle(Color.accentColor).monospacedDigit()
                }
                .contentShape(Rectangle())
                .padding(.vertical, 5).padding(.horizontal, 6)
              }
              .buttonStyle(.plain)
              .help("Open \(m.folder) at the transcript")
            }
          }
        }
        .frame(minHeight: 96, maxHeight: 132)
      }
      .padding(16)
    }
  }

  var summary: String {
    let n = topic.meetingCount
    var s = "\(n) meeting\(n == 1 ? "" : "s"), \(TopicsLibrary.duration(topic.speakingMs)) talked"
    let first = TopicsLibrary.dateLabel(topic.firstDate), last = TopicsLibrary.dateLabel(topic.lastDate)
    if !last.isEmpty { s += first == last ? ", \(last)" : ", \(first) to \(last)" }
    return s
  }
}
