import SwiftUI
import AppKit

/// D15: WHOSAID_APP_SELFTEST=<path> lists windows, snapshots the main window once
/// per sidebar item (WHOSAID_APP_SELFTEST_ITEMS=meetings,live,... to limit), writes
/// <path> with one line per check, and quits. No screen capture permission needed.
@MainActor enum SelfTest {
  static func run(out: String) async {
    var lines: [String] = []
    try? await Task.sleep(for: .seconds(2))
    let wins = NSApp.windows.filter { $0.isVisible && !$0.className.contains("StatusBar") }
    lines.append("windows \(NSApp.windows.map { "\($0.className)(\($0.isVisible ? "visible" : "hidden"))" }.joined(separator: " "))")
    lines.append("statusItem \(NSApp.windows.contains { $0.className.contains("StatusBar") })")
    guard let main = wins.first(where: { $0.title == "whosaid" || $0.identifier?.rawValue.contains("main") == true }) ?? wins.first,
          let model = AppModelLocator.model else {
      lines.append("FAIL no main window or model"); finish(out, lines); return
    }
    NSApp.activate()
    main.makeKeyAndOrderFront(nil)
    for _ in 0..<40 where model.health == .unknown { try? await Task.sleep(for: .milliseconds(250)) }
    lines.append("health \(model.health)")
    let wanted = ProcessInfo.processInfo.environment["WHOSAID_APP_SELFTEST_ITEMS"]?.split(separator: ",").map(String.init)
    for item in SidebarItem.allCases where wanted == nil || wanted!.contains(item.rawValue) {
      model.selection = item
      try? await Task.sleep(for: .milliseconds(600))
      let ok = await snapshot(main, to: "\(out).\(item.rawValue).png")
      lines.append("shot \(item.rawValue) \(ok ? "ok" : "MISSING")")
    }
    finish(out, lines)
  }

  /// Asks the harness (bin/selftest.sh, run from a terminal) to capture this one
  /// window with `screencapture -l`: the app itself never needs a capture grant.
  /// Writes "<png path> <window number>" to <out>.req and waits up to 8 s for the PNG.
  static func snapshot(_ w: NSWindow, to path: String) async -> Bool {
    let req = (ProcessInfo.processInfo.environment["WHOSAID_APP_SELFTEST"] ?? "") + ".req"
    try? "\(path) \(w.windowNumber)\n".write(toFile: req, atomically: true, encoding: .utf8)
    for _ in 0..<80 {
      if FileManager.default.fileExists(atPath: path) { return true }
      try? await Task.sleep(for: .milliseconds(100))
    }
    return false
  }

  static func finish(_ out: String, _ lines: [String]) {
    try? lines.joined(separator: "\n").appending("\n").write(toFile: out, atomically: true, encoding: .utf8)
    NSApp.terminate(nil)
  }
}

@MainActor enum AppModelLocator { static weak var model: AppModel? }
