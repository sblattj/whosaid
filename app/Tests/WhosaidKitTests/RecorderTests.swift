import Foundation
import Testing
@testable import WhosaidKit

private func state(_ autorecord: String, watch: Bool = true) throws -> AppState {
  let json = #"{"menubar": {"state": "idle"}, "watch": {"installed": \#(watch)}, "autorecord": \#(autorecord)}"#
  return AppState(raw: try JSONDecoder().decode(JSONValue.self, from: Data(json.utf8)))
}

@Test func recorderWithLiveOffOffersTheInstallCommand() throws {
  let r = RecorderPresentation(try state(#"""
    {"installed": true, "running": true, "state": "waiting", "mode": "app", "app": "com.microsoft.teams2",
     "label": "teams", "into": "/tmp/rec dir", "live": false,
     "last_saved": {"path": "/tmp/rec dir/20261029T150000Z-teams.m4a", "at": "2026-10-29T15:30:00-04:00"},
     "last_error": {"rc": 5, "kind": "permission", "at": "2026-10-29T16:00:00-04:00"}}
    """#))
  #expect(r.installed && r.running && r.state == "waiting" && !r.live && r.watcherInstalled)
  #expect(r.source == "Teams (com.microsoft.teams2)")
  #expect(r.lastSaved == "20261029T150000Z-teams.m4a" && r.lastSavedAt?.hasPrefix("2026-10-29T15:30") == true)
  #expect(r.lastError == "No Screen & System Audio Recording permission (exit 5)")
  #expect(r.command == "whosaid autorecord install --app com.microsoft.teams2 --into '/tmp/rec dir' --label teams --live")
}

@Test func recorderWithLiveOnNeedsNoCommand() throws {
  let r = RecorderPresentation(try state(#"{"installed": true, "running": true, "state": "recording", "mode": "browser", "into": "/r", "live": true}"#, watch: false))
  #expect(r.live && r.command == nil && r.source == "Meetings in the browser" && !r.watcherInstalled)
  let none = RecorderPresentation(try state(#"{"installed": false, "running": false, "state": "not-installed", "into": "/r"}"#))
  #expect(!none.installed && none.command == "whosaid autorecord install --app com.microsoft.teams2 --into /r --live")
  #expect(RecorderPresentation(nil) == RecorderPresentation(nil) && RecorderPresentation(nil).command == nil)
}

@Test func recorderLogTailDropsTheDate() throws {
  let dir = FileManager.default.temporaryDirectory.appendingPathComponent("whosaid-rec-\(UUID().uuidString)")
  try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: dir) }
  #expect(RecorderPresentation.logTail(into: dir.path).isEmpty)
  let lines = (1...20).map { String(format: "2026-10-29 10:00:%02d autorecord: line %d", $0, $0) }
  try (lines.joined(separator: "\n") + "\n").write(to: dir.appendingPathComponent(".autorecord.log"), atomically: true, encoding: .utf8)
  let tail = RecorderPresentation.logTail(into: dir.path, lines: 3)
  #expect(tail == ["10:00:18 autorecord: line 18", "10:00:19 autorecord: line 19", "10:00:20 autorecord: line 20"])
}
