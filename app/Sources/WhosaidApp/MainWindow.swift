import SwiftUI
import WhosaidKit

struct MainWindow: View {
  @Environment(AppModel.self) var model

  var body: some View {
    @Bindable var model = model
    NavigationSplitView {
      List(selection: $model.selection) {
        RecorderBadge()
        Section("Library") { ForEach(SidebarItem.library) { row($0) } }
        Section("People") { ForEach(SidebarItem.peopleGroup) { row($0) } }
        Section("Recorder") { ForEach(SidebarItem.recorderGroup) { row($0) } }
      }
      .navigationSplitViewColumnWidth(min: 200, ideal: 230)
    } detail: {
      DetailView(item: model.selection ?? .meetings)
    }
    .frame(minWidth: 1040, minHeight: 600)
    .toolbar {
      Button { model.searchOpen.toggle() } label: { Label("Search", systemImage: "magnifyingglass") }
        .keyboardShortcut("k").help("Search meetings (⌘K)")
    }
    .overlay(alignment: .top) {
      if model.searchOpen, case .ready = model.health { SearchOverlay().padding(.top, 40) }
    }
  }

  @ViewBuilder func row(_ item: SidebarItem) -> some View {
    Label(item.title, systemImage: item.symbol)
      .badge(badge(item))
      .tag(item)
  }

  func badge(_ item: SidebarItem) -> Int {
    switch item {
    case .unnamed: model.state?.unnamedVoices ?? 0
    default: 0
    }
  }
}

/// "● Recording 12:41" at the top of the sidebar while a call is recorded.
struct RecorderBadge: View {
  @Environment(AppModel.self) var model
  var body: some View {
    if let st = model.state, st.recording {
      HStack(spacing: 6) {
        Circle().fill(.red).frame(width: 8, height: 8)
        Text("Recording").font(.callout.weight(.semibold))
        if let e = st.elapsed { Text(Duration.seconds(e).formatted(.time(pattern: .minuteSecond))).monospacedDigit().foregroundStyle(.secondary) }
      }
    }
  }
}

struct DetailView: View {
  @Environment(AppModel.self) var model
  let item: SidebarItem

  var body: some View {
    Group {
      switch model.health {
      case .unknown:
        ProgressView("Reading the workspace")
      case .notConfigured:
        EmptyState(symbol: "folder.badge.questionmark", title: "Choose your meetings folder",
                   message: "whosaid keeps transcripts in a workspace folder. Pick it in Settings (the folder `whosaid watch` ingests into).",
                   action: ("Open Settings", { NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil) }))
      case .missing(let ws):
        EmptyState(symbol: "folder.badge.minus", title: "Workspace folder not found",
                   message: "\(ws) does not exist. Pick another folder in Settings.", action: nil)
      case .noIndex(let cmd):
        EmptyState(symbol: "magnifyingglass", title: "No meetings indexed yet",
                   message: "Transcripts appear here once the workspace is indexed. Run: \(cmd)", action: nil)
      case .error(let msg):
        EmptyState(symbol: "exclamationmark.triangle", title: "whosaid is not answering", message: msg, action: nil)
      case .ready:
        switch item {
        case .meetings: MeetingsView(chats: false)
        case .chats: MeetingsView(chats: true)
        case .people: PeopleView(unnamedOnly: false)
        case .unnamed: PeopleView(unnamedOnly: true)
        default: placeholder
        }
      }
    }
    .navigationTitle(item.title)
  }

  @ViewBuilder var placeholder: some View {
    EmptyState(symbol: item.symbol, title: item.title, message: "Coming in a later phase of this build.", action: nil)
  }
}

struct EmptyState: View {
  let symbol: String
  let title: String
  let message: String
  let action: (String, () -> Void)?

  var body: some View {
    VStack(spacing: 12) {
      Image(systemName: symbol).font(.system(size: 40)).foregroundStyle(.secondary)
      Text(title).font(.title2.weight(.semibold))
      Text(message).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 420).textSelection(.enabled)
      if let action { Button(action.0, action: action.1) }
    }
    .padding(40)
    .frame(maxWidth: .infinity, maxHeight: .infinity)
  }
}
