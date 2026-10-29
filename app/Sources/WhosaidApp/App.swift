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
      MenuBarLabel(menu: model.menu)
    }
    .menuBarExtraStyle(.window)
    Settings { SettingsView().environment(model) }
  }
}

struct MenuBarLabel: View {
  let menu: MenuPresentation
  var body: some View {
    HStack(spacing: 3) {
      Image(systemName: menu.symbol)
      if let t = menu.title { Text(t).monospacedDigit() }
      if menu.live { Image(systemName: "pencil.line") }
    }
    .accessibilityLabel(menu.headline)
  }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
  func applicationDidFinishLaunching(_ n: Notification) {
    guard let out = ProcessInfo.processInfo.environment["WHOSAID_APP_SELFTEST"] else { return }
    Task { @MainActor in await SelfTest.run(out: out) }
  }
}
