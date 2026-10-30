import SwiftUI
import WhosaidKit

/// The menu bar panel (design screen 5): what is happening now, the recording
/// controls, the last meetings and the shortcuts into the main window.
struct MenuContent: View {
  @Environment(AppModel.self) var model
  @Environment(\.openWindow) var openWindow

  var body: some View {
    let m = model.menu
    VStack(alignment: .leading, spacing: 2) {
      HStack(spacing: 6) {
        Image(systemName: m.symbol)
          .foregroundStyle(m.recording ? Color.red : m.symbol.hasPrefix("exclamationmark") ? .orange : .secondary)
        Text(m.headline).font(.headline).lineLimit(2)
        Spacer()
        if let t = m.title { Text(t).monospacedDigit().foregroundStyle(.secondary) }
      }
      .padding(.horizontal, 8).padding(.vertical, 6)
      if let note = model.actionNote {
        Text(note).font(.caption).foregroundStyle(.secondary).padding(.horizontal, 8)
      }

      if m.recording {
        SectionLabel("Recording")
        let app = model.state?.raw["autorecord"]?["app"]?.string
        MenuRow(symbol: "record.circle", title: app.map { "Call in \(MenuPresentation.appName($0))" } ?? "Call",
                detail: m.title, tint: .red) {}
          .disabled(true)
        MenuRow(symbol: "text.bubble", title: "Open live panel", shortcut: "⌘L") { openWindow(id: "live-panel"); NSApp.activate() }
          .keyboardShortcut("l")
        MenuRow(symbol: "stop.circle", title: m.saving ? "Saving…" : "Stop and save now") {
          Task { await model.stopNow() }
        }
        .disabled(!m.canStopNow)
      }

      SectionLabel("Last meetings")
      if let ing = m.ingesting {
        MenuRow(symbol: "gearshape.2", title: ing.isEmpty ? "Transcribing" : ing, detail: "transcribing") { show(.meetings) }
      }
      if model.recent.isEmpty {
        Text(emptyRecent).font(.callout).foregroundStyle(.secondary).padding(.horizontal, 8).padding(.vertical, 4)
      }
      ForEach(model.recent) { row in
        MenuRow(symbol: "checkmark.circle", title: row.title, detail: row.when) { show(.meetings) }
      }
      if m.unnamedVoices > 0 {
        MenuRow(symbol: "person.crop.circle.badge.questionmark",
                title: "Name \(m.unnamedVoices) unknown voice\(m.unnamedVoices == 1 ? "" : "s")…") { show(.unnamed) }
      }

      Divider().padding(.vertical, 4)
      MenuRow(symbol: "magnifyingglass", title: "Search meetings…", shortcut: "⌘K") { show(.meetings) }
        .keyboardShortcut("k")
      MenuRow(symbol: "macwindow", title: "Open whosaid") { show(model.selection ?? .meetings) }
      if m.paused {
        MenuRow(symbol: "play.circle", title: "Resume autorecord") { Task { await model.resume() } }
      } else {
        MenuRow(symbol: "pause.circle", title: "Pause autorecord for 1 hour") { Task { await model.pause(minutes: 60) } }
          .disabled(!m.canPause)
      }
      SettingsLink {
        MenuRowLabel(symbol: "gearshape", title: "Settings…", shortcut: "⌘,")
      }
      .buttonStyle(MenuRowStyle())
      MenuRow(symbol: "power", title: "Quit whosaid", shortcut: "⌘Q") { NSApp.terminate(nil) }
        .keyboardShortcut("q")
    }
    .padding(6)
    .frame(width: 320)
  }

  var emptyRecent: String {
    switch model.health {
    case .ready: "No meetings yet"
    case .notConfigured: "Choose a workspace in Settings"
    default: "Library not ready"
    }
  }

  func show(_ item: SidebarItem) {
    model.selection = item
    openWindow(id: "main")
    NSApp.activate()
  }

}

struct SectionLabel: View {
  let text: String
  init(_ text: String) { self.text = text }
  var body: some View {
    Text(text.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
      .padding(.horizontal, 8).padding(.top, 8).padding(.bottom, 2)
  }
}

struct MenuRow: View {
  let symbol: String
  let title: String
  var detail: String?
  var shortcut: String?
  var tint: Color = .primary
  let action: () -> Void

  var body: some View {
    Button(action: action) {
      MenuRowLabel(symbol: symbol, title: title, detail: detail, shortcut: shortcut, tint: tint)
    }
    .buttonStyle(MenuRowStyle())
  }
}

struct MenuRowLabel: View {
  let symbol: String
  let title: String
  var detail: String?
  var shortcut: String?
  var tint: Color = .primary

  var body: some View {
    HStack(spacing: 8) {
      Image(systemName: symbol).frame(width: 18).foregroundStyle(tint == .primary ? .secondary : tint)
      Text(title).lineLimit(1).truncationMode(.middle)
      Spacer(minLength: 8)
      if let detail { Text(detail).foregroundStyle(.secondary).monospacedDigit() }
      if let shortcut { Text(shortcut).foregroundStyle(.tertiary) }
    }
    .contentShape(Rectangle())
  }
}

/// A full-width row that highlights on hover, like a menu item.
struct MenuRowStyle: ButtonStyle {
  func makeBody(configuration: Configuration) -> some View {
    HoverRow(configuration: configuration)
  }

  struct HoverRow: View {
    let configuration: Configuration
    @State private var hover = false
    @Environment(\.isEnabled) private var enabled
    var body: some View {
      configuration.label
        .padding(.horizontal, 8).padding(.vertical, 4)
        .background(RoundedRectangle(cornerRadius: 5).fill(hover && enabled ? Color.accentColor.opacity(0.25) : .clear))
        .opacity(enabled ? 1 : 0.45)
        .onHover { hover = $0 }
    }
  }
}
