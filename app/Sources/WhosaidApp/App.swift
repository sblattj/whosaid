import SwiftUI
import AppKit
import WhosaidKit

@main struct WhosaidApp: App {
  @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
  @State var model = AppModel()

  var body: some Scene {
    Window("whosaid", id: "main") {
      MainWindow().environment(model).task { model.start() }
    }
    .defaultLaunchBehavior(.presented)
    MenuBarExtra {
      MenuContent().environment(model)
    } label: {
      Image(systemName: model.state?.recording == true ? "record.circle.fill" : "waveform")
    }
    .menuBarExtraStyle(.window)
    Settings { SettingsView().environment(model) }
  }
}

struct MenuContent: View {
  @Environment(AppModel.self) var model
  @Environment(\.openWindow) var openWindow
  var body: some View {
    VStack(alignment: .leading, spacing: 8) {
      Text(model.state?.headline ?? "starting").font(.headline)
      Divider()
      Button("Open whosaid") { openWindow(id: "main"); NSApp.activate() }
      Button("Quit") { NSApp.terminate(nil) }
    }
    .padding(12)
    .frame(width: 280)
  }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
  func applicationDidFinishLaunching(_ n: Notification) {
    guard let out = ProcessInfo.processInfo.environment["WHOSAID_APP_SELFTEST"] else { return }
    Task { @MainActor in await SelfTest.run(out: out) }
  }
}
