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
    .defaultSize(width: 1280, height: 800)
    .commands { LivePanelCommand() }
    Window("Live", id: "live-panel") {
      LivePanel().environment(model)
    }
    .windowLevel(.floating)
    .windowResizability(.contentMinSize)
    .defaultSize(width: 340, height: 620)
    .defaultPosition(.trailing)
    MenuBarExtra {
      MenuContent().environment(model)
    } label: {
      MenuBarLabel(menu: model.menu)
    }
    .menuBarExtraStyle(.window)
    Settings { SettingsView().environment(model) }
  }
}

/// ⌘L opens the floating live panel from any window.
struct LivePanelCommand: Commands {
  @Environment(\.openWindow) var openWindow
  var body: some Commands {
    CommandGroup(after: .windowArrangement) {
      Button("Open Live Panel") { openWindow(id: "live-panel") }.keyboardShortcut("l")
    }
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
  func applicationWillTerminate(_ n: Notification) {
    MainActor.assumeIsolated { AppModelLocator.model?.live.stopFollower() }
  }

  func applicationDidFinishLaunching(_ n: Notification) {
    for name in [NSWindow.didBecomeKeyNotification, NSWindow.willCloseNotification,
                 NSWindow.didMiniaturizeNotification, NSWindow.didDeminiaturizeNotification] {
      NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { _ in
        // willClose fires while the window is still visible; look again next turn.
        DispatchQueue.main.async { MainActor.assumeIsolated { AppDelegate.syncDock() } }
      }
    }
    AppDelegate.syncDock()
    guard let out = ProcessInfo.processInfo.environment["WHOSAID_APP_SELFTEST"] else { return }
    Task { @MainActor in await SelfTest.run(out: out) }
  }

  /// Dock and ⌘Tab while a whosaid window is open; menu bar only otherwise.
  @MainActor static func syncDock() {
    let facts = NSApp.windows.map {
      WindowFacts(titled: $0.styleMask.contains(.titled), panel: $0 is NSPanel,
                  visible: $0.isVisible, miniaturized: $0.isMiniaturized)
    }
    let want: NSApplication.ActivationPolicy = DockPresence.shows(facts) ? .regular : .accessory
    guard NSApp.activationPolicy() != want else { return }
    NSApp.setActivationPolicy(want)
    if want == .regular { NSApp.activate() }
  }
}
