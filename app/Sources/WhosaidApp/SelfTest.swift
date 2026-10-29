import SwiftUI
import AppKit
import WhosaidKit

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
    if ProcessInfo.processInfo.environment["WHOSAID_APP_SELFTEST_MENU"] != nil {
      await menuShots(model: model, out: out, lines: &lines)
      finish(out, lines); return
    }
    let env = ProcessInfo.processInfo.environment
    let wanted = env["WHOSAID_APP_SELFTEST_ITEMS"]?.split(separator: ",").map(String.init)
    for item in SidebarItem.allCases where wanted == nil || wanted!.contains(item.rawValue) {
      model.selection = item
      try? await Task.sleep(for: .milliseconds(item == .meetings || item == .people ? 1500 : 600))
      let ok = await snapshot(main, to: "\(out).\(item.rawValue).png")
      lines.append("shot \(item.rawValue) \(ok ? "ok" : "MISSING")")
      if item == .meetings, let pick = env["WHOSAID_APP_SELFTEST_TABS"] {
        // A value other than 1 picks the meeting whose folder contains it.
        if pick != "1", let m = model.meetings.first(where: { $0.folder.contains(pick) }) {
          model.selectedMeeting = m.folder
          try? await Task.sleep(for: .milliseconds(700))
        }
        for tab in MeetingTab.allCases.dropFirst() {
          model.meetingTab = tab
          try? await Task.sleep(for: .milliseconds(700))
          let name = "meetings-" + tab.rawValue.lowercased().replacingOccurrences(of: " ", with: "-")
          lines.append("shot \(name) \(await snapshot(main, to: "\(out).\(name).png") ? "ok" : "MISSING")")
        }
        model.meetingTab = .transcript
      }
    }
    if let spec = env["WHOSAID_APP_SELFTEST_RELABEL"] {
      await relabelShots(spec, model: model, main: main, out: out, lines: &lines)
    }
    if env["WHOSAID_APP_SELFTEST_MARKDONE"] != nil {
      await markDoneShots(model: model, main: main, out: out, lines: &lines)
    }
    if let q = env["WHOSAID_APP_SELFTEST_SEARCH"] {
      model.selection = .meetings
      model.searchOpen = true
      model.searchQuery = q
      await model.runSearch()
      await model.loadContext()
      try? await Task.sleep(for: .milliseconds(900))
      lines.append("search \(model.searchHits.count) hit(s), context \(model.searchContext.count) turn(s)")
      lines.append("shot search \(await snapshot(main, to: "\(out).search.png") ? "ok" : "MISSING")")
    }
    // G3: browsing every page above must not have made a single audio player.
    lines.append("audio players \(ClipPlayer.created)")
    finish(out, lines)
  }

  /// WHOSAID_APP_SELFTEST_RELABEL=<folder part>|SPEAKER_NN|Name|role names one voice
  /// through the Save path and shoots the speakers tab before and after plus the
  /// unnamed page. It refuses unless WHOSAID_SPEAKER_DB names a throwaway registry:
  /// a fixture voiceprint must never land in the real one.
  static func relabelShots(_ spec: String, model: AppModel, main: NSWindow, out: String, lines: inout [String]) async {
    let parts = spec.split(separator: "|").map(String.init)
    let db = ProcessInfo.processInfo.environment["WHOSAID_SPEAKER_DB"] ?? ""
    let real = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".config/whosaid/speakers.json").path
    guard parts.count >= 3, !db.isEmpty, URL(fileURLWithPath: db).standardized.path != real,
          FileManager.default.fileExists(atPath: db) else {
      lines.append("relabel REFUSED (needs folder|cluster|name and WHOSAID_SPEAKER_DB naming an existing non-default registry)")
      return
    }
    guard let m = model.meetings.first(where: { $0.folder.contains(parts[0]) }) else {
      lines.append("relabel no meeting matches \(parts[0])"); return
    }
    model.selection = .meetings
    model.selectedMeeting = m.folder
    model.meetingTab = .speakers
    try? await Task.sleep(for: .milliseconds(1200))
    lines.append("shot speakers-before \(await snapshot(main, to: "\(out).speakers-before.png") ? "ok" : "MISSING")")
    let err = await model.relabel(folder: m.folder, cluster: parts[1], name: parts[2], role: parts.count > 3 ? parts[3] : nil)
    lines.append("relabel \(parts[1])=\(parts[2]) \(err ?? "ok")")
    try? await Task.sleep(for: .milliseconds(1200))
    lines.append("shot speakers-after \(await snapshot(main, to: "\(out).speakers-after.png") ? "ok" : "MISSING")")
    model.meetingTab = .transcript
    try? await Task.sleep(for: .milliseconds(700))
    lines.append("shot transcript-after \(await snapshot(main, to: "\(out).transcript-after.png") ? "ok" : "MISSING")")
    model.selection = .unnamed
    try? await Task.sleep(for: .milliseconds(1500))
    lines.append("unnamed after \(model.people.filter { $0["named"]?.bool == false }.count)")
    lines.append("shot unnamed-after \(await snapshot(main, to: "\(out).unnamed-after.png") ? "ok" : "MISSING")")
  }

  /// WHOSAID_APP_SELFTEST_MARKDONE=1 accepts the first suggested completion through the
  /// Mark done path (whosaid_set_status), opens its source, and shoots before and after.
  /// It writes the workspace's tracking ledger, so it refuses outside an app fixture.
  static func markDoneShots(model: AppModel, main: NSWindow, out: String, lines: inout [String]) async {
    guard let ws = model.paths.workspace, ws.contains("whosaid-app-fixture") else {
      lines.append("markdone REFUSED (only on an app/fixture.sh workspace)"); return
    }
    model.selection = .commitments
    try? await Task.sleep(for: .milliseconds(2500))
    lines.append("worklist \(model.worklist.count) item(s), \(model.worklist.filter(\.isDone).count) done, suggestions \(model.suggestions.count)")
    lines.append("shot commitments-before \(await snapshot(main, to: "\(out).commitments-before.png") ? "ok" : "MISSING")")
    guard let s = model.suggestions.first else { lines.append("markdone no suggestion"); return }
    model.openSource(meeting: s.meeting ?? "", t: s.t)
    try? await Task.sleep(for: .milliseconds(1500))
    lines.append("shot source \(await snapshot(main, to: "\(out).source.png") ? "ok" : "MISSING")")
    let err = await model.markDone(s.itemId, note: s.note)
    lines.append("markdone \(s.itemId) \(err ?? "ok")")
    model.selection = .commitments
    try? await Task.sleep(for: .milliseconds(2000))
    lines.append("worklist after \(model.worklist.filter(\.isDone).map(\.id)) done, suggestions \(model.suggestions.count)")
    lines.append("shot commitments-after \(await snapshot(main, to: "\(out).commitments-after.png") ? "ok" : "MISSING")")
    model.selection = .actionItems
    try? await Task.sleep(for: .milliseconds(1500))
    lines.append("items done \(model.items.filter(\.isDone).map(\.id))")
  }

  /// The menu panel (hosted in a plain window so it can be captured without a
  /// click) and the status item, for the snapshot the model was given.
  static func menuShots(model: AppModel, out: String, lines: inout [String]) async {
    lines.append("menubar state \(model.state?.menu.rawValue ?? "none") symbol \(model.menu.symbol) title \(model.menu.title ?? "-")")
    lines.append("headline \(model.menu.headline)")
    // The status item itself cannot be captured per window: its label is drawn above the panel.
    let host = NSHostingController(rootView: VStack(spacing: 0) {
      HStack { Spacer(); MenuBarLabel(menu: model.menu).padding(6).background(.quaternary, in: .rect(cornerRadius: 5)) }
        .padding(8)
      Divider()
      MenuContent()
    }.environment(model))
    let panel = NSWindow(contentViewController: host)
    panel.styleMask = [.titled]
    panel.title = "whosaid menu"
    panel.setContentSize(host.view.fittingSize)
    panel.center()
    NSApp.activate()
    panel.makeKeyAndOrderFront(nil)
    try? await Task.sleep(for: .milliseconds(900))
    lines.append("shot menu \(await snapshot(panel, to: "\(out).menu.png") ? "ok" : "MISSING")")
    panel.close()
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
