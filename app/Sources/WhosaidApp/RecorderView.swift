import SwiftUI
import WhosaidKit

/// Recorder: what autorecord is doing, its setup, the recent log and the same
/// stop / pause / resume controls as the menu bar. Turning live on is a command to
/// copy, never something the app installs.
struct RecorderView: View {
  @Environment(AppModel.self) var model
  @State private var log: [String] = []
  @State private var copied = false

  var body: some View {
    let r = RecorderPresentation(model.state)
    let m = model.menu
    ScrollView {
      VStack(alignment: .leading, spacing: 16) {
        HStack(spacing: 10) {
          Image(systemName: m.symbol).font(.title2).foregroundStyle(m.recording ? .red : .secondary)
          VStack(alignment: .leading, spacing: 2) {
            Text(m.headline).font(.title3.weight(.semibold))
            if let t = m.title { Text(t).monospacedDigit().foregroundStyle(.secondary) }
          }
          Spacer()
          if m.recording {
            Button(m.saving ? "Saving…" : "Stop and save now") { Task { await model.stopNow() } }.disabled(!m.canStopNow)
          }
          if m.paused {
            Button("Resume") { Task { await model.resume() } }
          } else {
            Button("Pause for 1 hour") { Task { await model.pause(minutes: 60) } }.disabled(!m.canPause)
          }
        }
        if let note = model.actionNote { Text(note).font(.callout).foregroundStyle(.secondary) }

        Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 16, verticalSpacing: 8) {
          row("Autorecord", r.installed ? (r.running ? "On, \(r.state)" : "Installed, not running") : "Not installed",
              ok: r.installed && r.running)
          row("Records", r.source)
          row("Into", r.into)
          row("Live transcript", r.live ? "On with every call" : "Off", ok: r.live)
          row("Transcribe after", r.watcherInstalled ? "On (whosaid watch)" : "Off", ok: r.watcherInstalled)
          if let saved = r.lastSaved { row("Last saved", saved + (r.lastSavedAt.map { "  ·  " + $0.prefix(16).replacingOccurrences(of: "T", with: " ") } ?? "")) }
          if let err = r.lastError { row("Last error", err, ok: false) }
        }

        if let cmd = r.command {
          VStack(alignment: .leading, spacing: 6) {
            Text(r.installed ? "Turn on the live transcript for every call" : "Record every call").font(.headline)
            Text("Run this in a terminal. It keeps the recorder's permission; the app never installs agents itself.")
              .font(.callout).foregroundStyle(.secondary)
            HStack {
              Text(cmd).font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                .padding(8).frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
              Button(copied ? "Copied" : "Copy") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(cmd, forType: .string)
                copied = true
              }
            }
          }
        }

        VStack(alignment: .leading, spacing: 4) {
          Text("Recent log").font(.headline)
          if log.isEmpty { Text("No log yet").foregroundStyle(.secondary) }
          ForEach(Array(log.enumerated()), id: \.offset) { _, line in
            Text(line).font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary)
              .lineLimit(2).textSelection(.enabled)
          }
        }
      }
      .padding(20)
      .frame(maxWidth: 820, alignment: .leading)
    }
    .task(id: model.state?.raw["autorecord"]) {  // re-read the log with each app-state snapshot
      let into = r.into.isEmpty ? model.paths.recordings : r.into
      log = await Task.detached { RecorderPresentation.logTail(into: into) }.value
    }
  }

  @ViewBuilder func row(_ label: String, _ value: String, ok: Bool? = nil) -> some View {
    GridRow {
      Text(label).foregroundStyle(.secondary)
      HStack(spacing: 6) {
        if let ok { Circle().fill(ok ? .green : .orange).frame(width: 7, height: 7) }
        Text(value).textSelection(.enabled)
      }
    }
  }
}
