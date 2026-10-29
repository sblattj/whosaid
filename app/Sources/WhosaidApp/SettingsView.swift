import ServiceManagement
import SwiftUI
import WhosaidKit

struct SettingsView: View {
  @Environment(AppModel.self) var model

  var body: some View {
    Form {
      Section("Folders") {
        PathRow(label: "Workspace", value: model.paths.workspace ?? "not set") { model.setWorkspace($0) }
        PathRow(label: "Recordings", value: model.paths.recordings) { model.setRecordings($0) }
      }
      Section("whosaid") {
        PathRow(label: "whosaid command", value: model.paths.whosaid, files: true) { model.setWhosaid($0) }
        LabeledContent("Autorecord", value: model.state?.autorecordState ?? "unknown")
        LabeledContent("Watcher", value: (model.state?.watcherInstalled ?? false) ? "installed" : "not installed")
        LabeledContent("Library", value: model.health.summary)
      }
      Section("App") {
        LoginItemToggle()
      }
    }
    .formStyle(.grouped)
    .frame(width: 520)
    .padding()
  }
}

struct PathRow: View {
  let label: String
  let value: String
  var files = false
  let pick: (String) -> Void

  var body: some View {
    LabeledContent(label) {
      HStack {
        Text(value).lineLimit(1).truncationMode(.middle).foregroundStyle(.secondary).textSelection(.enabled)
        Button("Choose…") {
          let panel = NSOpenPanel()
          panel.canChooseDirectories = !files
          panel.canChooseFiles = files
          if panel.runModal() == .OK, let url = panel.url { pick(url.path) }
        }
      }
    }
  }
}

/// Open at login, through SMAppService (macOS 13+). Off until the user turns it on.
struct LoginItemToggle: View {
  @State private var on = SMAppService.mainApp.status == .enabled
  @State private var error: String?

  var body: some View {
    Toggle("Open at login", isOn: $on)
      .onChange(of: on) { _, want in
        do {
          if want { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
          error = nil
        } catch {
          self.error = error.localizedDescription
          on = SMAppService.mainApp.status == .enabled
        }
      }
    if let error { Text(error).font(.caption).foregroundStyle(.red) }
  }
}
