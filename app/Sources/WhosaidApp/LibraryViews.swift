import Charts
import SwiftUI
import WhosaidKit

/// Stable per-speaker colors: the same name gets the same color in every view.
enum SpeakerColor {
  static let palette: [Color] = [.blue, .orange, .green, .purple, .pink, .teal, .yellow, .indigo, .mint, .brown]
  static func of(_ speaker: String) -> Color {
    if Transcript.isUnnamed(speaker) { return .gray }
    let h = speaker.unicodeScalars.reduce(UInt32(5381)) { ($0 &* 33) &+ $1.value }
    return palette[Int(h % UInt32(palette.count))]
  }
}

// MARK: - Meetings and chats

struct MeetingsView: View {
  @Environment(AppModel.self) var model
  let chats: Bool

  var rows: [MeetingSummary] { model.meetings.filter { $0.isChat == chats } }

  var body: some View {
    @Bindable var model = model
    HStack(spacing: 0) {
      List(rows, selection: $model.selectedMeeting) { m in
        MeetingListRow(meeting: m).tag(m.folder)
      }
      .frame(width: 300)
      Divider()
      .overlay {
        if rows.isEmpty && model.meetingsLoaded {
          Text(chats ? "No chats imported" : "No meetings yet").foregroundStyle(.secondary)
        }
      }
      Group {
        if let f = model.selectedMeeting, let m = model.meetings.first(where: { $0.folder == f }) {
          MeetingDetailView(meeting: m).id(m.folder)
        } else {
          Text("Pick a meeting").foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
        }
      }
      .frame(maxWidth: .infinity)
    }
    .task(id: chats) {
      await model.loadMeetings()
      if model.selectedMeeting.map({ f in rows.contains { $0.folder == f } }) != true {
        model.selectedMeeting = rows.first?.folder
      }
    }
  }
}

struct MeetingListRow: View {
  @Environment(AppModel.self) var model
  let meeting: MeetingSummary

  var body: some View {
    let info = model.folderInfo[meeting.folder]
    VStack(alignment: .leading, spacing: 3) {
      HStack {
        Image(systemName: meeting.isChat ? "bubble.left.and.bubble.right" : "waveform").foregroundStyle(.secondary)
        Text(model.title(meeting)).font(.body.weight(.medium)).lineLimit(1)
        Spacer()
        if let len = meeting.length { Text(len).font(.caption).foregroundStyle(.secondary).monospacedDigit() }
      }
      HStack(spacing: 8) {
        Text(LibraryFormat.date(meeting)).font(.caption).foregroundStyle(.secondary)
        if let info {
          Label("\(info.speakers)", systemImage: "person.2").font(.caption).foregroundStyle(.secondary)
          if info.unnamed > 0 {
            Label("\(info.unnamed)", systemImage: "questionmark.circle").font(.caption).foregroundStyle(.orange)
          }
          if info.items > 0 { Label("\(info.items)", systemImage: "checklist").font(.caption).foregroundStyle(.secondary) }
        }
      }
      .labelStyle(.titleAndIcon)
    }
    .padding(.vertical, 2)
    .task { model.loadFolderInfo(meeting.folder) }
  }
}

enum LibraryFormat {
  static func date(_ m: MeetingSummary) -> String {
    if let d = m.created { return d.formatted(.dateTime.month(.abbreviated).day().hour().minute()) }
    let p = m.folder.split(separator: "-")
    return p.count >= 3 ? "\(p[0])-\(p[1])-\(p[2])" : ""
  }
}

enum MeetingTab: String, CaseIterable, Identifiable {
  case transcript = "Transcript", summary = "Summary", items = "Action items", speakers = "Speakers"
  var id: String { rawValue }
}

struct MeetingDetailView: View {
  @Environment(AppModel.self) var model
  let meeting: MeetingSummary
  @State private var turns: [Turn] = []
  @State private var items: [FolderItem] = []
  @State private var cards: String?

  var talk: [(speaker: String, seconds: Double)] { Transcript.talkTime(turns, end: meeting.duration) }

  var body: some View {
    @Bindable var model = model
    VStack(alignment: .leading, spacing: 0) {
      VStack(alignment: .leading, spacing: 6) {
        Text(model.title(meeting)).font(.title2.weight(.semibold)).lineLimit(1)
        HStack(spacing: 12) {
          Text(LibraryFormat.date(meeting))
          if let len = meeting.length { Text(len) }
          if let src = meeting.sourceName, !src.isEmpty { Text(src).lineLimit(1) }
          Text("\(Transcript.speakers(turns).count) speakers")
        }
        .font(.callout).foregroundStyle(.secondary)
        TalkTimeBar(talk: talk).frame(height: 14)
        Picker("", selection: $model.meetingTab) { ForEach(MeetingTab.allCases) { Text($0.rawValue).tag($0) } }
          .pickerStyle(.segmented).labelsHidden().frame(maxWidth: 460)
      }
      .padding(16)
      Divider()
      switch model.meetingTab {
      case .transcript: TranscriptList(turns: turns)
      case .summary: SummaryPane(meeting: meeting, talk: talk, turns: turns, items: items)
      case .items: ItemsPane(items: items)
      case .speakers: SpeakersPane(talk: talk, cards: cards)
      }
    }
    .task {
      guard let ws = model.paths.workspace else { return }
      let folder = MeetingFolder(workspace: ws, folder: meeting.folder)
      let loaded = await Task.detached { (folder.turns(), folder.items(), folder.speakerCards()) }.value
      (turns, items, cards) = loaded
    }
  }
}

/// One stacked bar: each speaker's share of the talk time, in their color.
struct TalkTimeBar: View {
  let talk: [(speaker: String, seconds: Double)]
  var body: some View {
    Chart(Array(talk.enumerated()), id: \.offset) { _, row in
      BarMark(x: .value("Talk time", row.seconds), y: .value("", "talk"))
        .foregroundStyle(SpeakerColor.of(row.speaker))
    }
    .chartXAxis(.hidden).chartYAxis(.hidden).chartLegend(.hidden)
    .clipShape(.rect(cornerRadius: 4))
    .accessibilityLabel(talk.map { "\($0.speaker) \(Int($0.seconds)) seconds" }.joined(separator: ", "))
  }
}

struct TranscriptList: View {
  let turns: [Turn]
  var body: some View {
    if turns.isEmpty {
      Text("No speaker-labeled transcript in this folder").foregroundStyle(.secondary)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    } else {
      ScrollView {
        LazyVStack(alignment: .leading, spacing: 10) {
          ForEach(turns) { turn in
            HStack(alignment: .top, spacing: 10) {
              Text(turn.clock).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                .frame(width: 54, alignment: .trailing).padding(.top, 1)
              VStack(alignment: .leading, spacing: 2) {
                Text(turn.speaker).font(.caption.weight(.semibold)).foregroundStyle(SpeakerColor.of(turn.speaker))
                Text(turn.text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
              }
              .padding(.leading, 8)
              .overlay(alignment: .leading) {
                RoundedRectangle(cornerRadius: 2).fill(SpeakerColor.of(turn.speaker)).frame(width: 3)
              }
            }
          }
        }
        .padding(16)
      }
    }
  }
}

struct SummaryPane: View {
  let meeting: MeetingSummary
  let talk: [(speaker: String, seconds: Double)]
  let turns: [Turn]
  let items: [FolderItem]

  var body: some View {
    ScrollView {
      VStack(alignment: .leading, spacing: 16) {
        Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
          GridRow { Text("Turns").foregroundStyle(.secondary); Text("\(turns.count)") }
          GridRow { Text("Speakers").foregroundStyle(.secondary); Text("\(talk.count)") }
          GridRow { Text("Unnamed").foregroundStyle(.secondary); Text("\(talk.filter { Transcript.isUnnamed($0.speaker) }.count)") }
          GridRow { Text("Action items").foregroundStyle(.secondary); Text("\(items.count)") }
          GridRow { Text("Kind").foregroundStyle(.secondary); Text(meeting.kind ?? "audio") }
        }
        Text("Talk time").font(.headline)
        Chart(Array(talk.enumerated()), id: \.offset) { _, row in
          BarMark(x: .value("Minutes", row.seconds / 60), y: .value("Speaker", row.speaker))
            .foregroundStyle(SpeakerColor.of(row.speaker))
            .annotation(position: .trailing) { Text(Transcript.clock(row.seconds)).font(.caption).foregroundStyle(.secondary) }
        }
        .chartLegend(.hidden)
        .frame(height: CGFloat(max(1, talk.count)) * 32 + 30)
      }
      .padding(16)
    }
  }
}

struct ItemsPane: View {
  let items: [FolderItem]
  var body: some View {
    if items.isEmpty {
      Text("No action items for this meeting").foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
    } else {
      List {
        ForEach(Array(Dictionary(grouping: items, by: \.section).keys).sorted(), id: \.self) { section in
          Section(section) {
            ForEach(items.filter { $0.section == section }) { item in
              VStack(alignment: .leading, spacing: 2) {
                Text(item.text)
                Text(item.owner).font(.caption).foregroundStyle(SpeakerColor.of(item.owner))
              }
            }
          }
        }
      }
    }
  }
}

struct SpeakersPane: View {
  let talk: [(speaker: String, seconds: Double)]
  let cards: String?
  var body: some View {
    List {
      Section("Who spoke") {
        ForEach(talk, id: \.speaker) { row in
          HStack {
            Circle().fill(SpeakerColor.of(row.speaker)).frame(width: 10, height: 10)
            Text(row.speaker)
            if Transcript.isUnnamed(row.speaker) { Text("unnamed").font(.caption).foregroundStyle(.orange) }
            Spacer()
            Text(Transcript.clock(row.seconds)).monospacedDigit().foregroundStyle(.secondary)
          }
        }
      }
      if let cards {
        Section("Speaker cards") { Text(cards).font(.callout.monospaced()).textSelection(.enabled) }
      }
    }
  }
}

// MARK: - People

struct PeopleView: View {
  @Environment(AppModel.self) var model
  let unnamedOnly: Bool
  @State private var selected: String?
  @State private var detail: JSONValue?

  var rows: [JSONValue] {
    model.people.filter { ($0["named"]?.bool ?? true) != unnamedOnly }
  }

  var body: some View {
    HStack(spacing: 0) {
      List(rows.compactMap { $0["name"]?.string }, id: \.self, selection: $selected) { name in
        let p = rows.first { $0["name"]?.string == name }
        HStack {
          Circle().fill(SpeakerColor.of(name)).frame(width: 10, height: 10)
          Text(name)
          Spacer()
          Text("\(p?["meetings"]?.int ?? 0) mtg").font(.caption).foregroundStyle(.secondary)
        }
      }
      .frame(width: 280)
      Divider()
      .overlay { if rows.isEmpty && model.peopleLoaded { Text(unnamedOnly ? "Every voice has a name" : "Nobody yet").foregroundStyle(.secondary) } }
      PersonDetail(name: selected, row: rows.first { $0["name"]?.string == selected }, detail: detail)
        .frame(maxWidth: .infinity)
    }
    .task { await model.loadPeople() }
    .onChange(of: model.people) { if selected == nil { selected = rows.first?["name"]?.string } }
    .onAppear { if selected == nil { selected = rows.first?["name"]?.string } }
    .task(id: selected) {
      detail = nil
      if let selected, !unnamedOnly { detail = await model.person(selected) }
    }
  }
}

struct PersonDetail: View {
  let name: String?
  let row: JSONValue?
  let detail: JSONValue?

  var body: some View {
    if let name {
      ScrollView {
        VStack(alignment: .leading, spacing: 12) {
          HStack {
            Circle().fill(SpeakerColor.of(name)).frame(width: 14, height: 14)
            Text(name).font(.title2.weight(.semibold))
          }
          HStack(spacing: 16) {
            Text("\(row?["turns"]?.int ?? 0) turns")
            Text("\(row?["meetings"]?.int ?? 0) meetings")
          }
          .foregroundStyle(.secondary)
          if Transcript.isUnnamed(name) {
            Text("This voice has no name yet. Naming arrives with the speakers view.").foregroundStyle(.secondary)
          }
          ItemList(title: "Owns", items: detail?["person"]?["owned_items"]?.array ?? detail?["person"]?["owned"]?.array ?? [])
          ItemList(title: "Asked for", items: detail?["person"]?["requested_items"]?.array ?? detail?["person"]?["requested"]?.array ?? [])
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
      }
    } else {
      Text("Pick a person").foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
    }
  }
}

struct ItemList: View {
  let title: String
  let items: [JSONValue]
  var body: some View {
    VStack(alignment: .leading, spacing: 6) {
      Text("\(title) (\(items.count))").font(.headline)
      if items.isEmpty { Text("Nothing recorded").foregroundStyle(.secondary) }
      ForEach(Array(items.prefix(50).enumerated()), id: \.offset) { _, it in
        HStack(alignment: .firstTextBaseline) {
          Text(it["id"]?.string ?? "•").font(.caption.monospaced()).foregroundStyle(.secondary)
          Text(it["text"]?.string ?? it["title"]?.string ?? "")
        }
      }
    }
  }
}

// MARK: - Search (⌘K)

struct SearchOverlay: View {
  @Environment(AppModel.self) var model
  @FocusState private var focused: Bool

  var body: some View {
    @Bindable var model = model
    VStack(alignment: .leading, spacing: 0) {
      HStack {
        Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
        TextField("Search meetings", text: $model.searchQuery)
          .textFieldStyle(.plain).font(.title3).focused($focused)
          .onSubmit { Task { await model.runSearch() } }
        if model.searching { ProgressView().controlSize(.small) }
        Button { model.searchOpen = false } label: { Image(systemName: "xmark.circle.fill") }
          .buttonStyle(.plain).foregroundStyle(.secondary).keyboardShortcut(.cancelAction)
      }
      .padding(12)
      Divider()
      HStack(alignment: .top, spacing: 0) {
        List(Array(model.searchHits.enumerated()), id: \.offset, selection: $model.searchPick) { i, hit in
          VStack(alignment: .leading, spacing: 2) {
            HStack {
              Text(hit["speaker"]?.string ?? "").font(.caption.weight(.semibold))
                .foregroundStyle(SpeakerColor.of(hit["speaker"]?.string ?? ""))
              Spacer()
              Text(hit["t_str"]?.string ?? "").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
            Text(hit["text"]?.string ?? "").lineLimit(2)
            Text(hit["meeting"]?.string ?? "").font(.caption).foregroundStyle(.secondary).lineLimit(1)
          }
          .tag(i)
        }
        .frame(width: 340)
        Divider()
        ContextPane(turns: model.searchContext, at: model.searchPick.flatMap { i in
          i < model.searchHits.count ? model.searchHits[i]["t_sec"]?.double : nil })
      }
      .frame(height: 380)
      if let err = model.searchError {
        Text(err).font(.caption).foregroundStyle(.red).padding(8)
      }
    }
    .frame(width: 820)
    .background(.regularMaterial, in: .rect(cornerRadius: 12))
    .shadow(radius: 20)
    .onAppear { focused = true }
    .onChange(of: model.searchPick) { Task { await model.loadContext() } }
  }
}

/// The minute around a search hit (whosaid_context), the hit's own turn marked.
struct ContextPane: View {
  let turns: [JSONValue]
  let at: Double?
  var body: some View {
    if turns.isEmpty {
      Text("Pick a result to read the minute around it").foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
    } else {
      List(Array(turns.enumerated()), id: \.offset) { _, t in
        let speaker = t["speaker"]?.string ?? ""
        let hit = at != nil && t["t_sec"]?.double == at
        VStack(alignment: .leading, spacing: 2) {
          HStack {
            Text(speaker).font(.caption.weight(.semibold)).foregroundStyle(SpeakerColor.of(speaker))
            Text(t["t_str"]?.string ?? "").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
          }
          Text(t["text"]?.string ?? "").textSelection(.enabled)
        }
        .listRowBackground(hit ? Color.accentColor.opacity(0.18) : Color.clear)
      }
      .listStyle(.plain)
    }
  }
}
