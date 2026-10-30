import Foundation
import Testing
import WebKit
@testable import WhosaidKit

private func liveDir() throws -> URL {
  let into = FileManager.default.temporaryDirectory.appendingPathComponent("whosaid-live-\(UUID().uuidString)")
  try FileManager.default.createDirectory(at: into.appendingPathComponent(".live"), withIntermediateDirectories: true)
  return into
}

@Test func liveSessionReadsMetaEntriesAndInsights() throws {
  let into = try liveDir()
  defer { try? FileManager.default.removeItem(at: into) }
  let now = Date().timeIntervalSince1970
  let jl = into.appendingPathComponent(".live/20261029T090000.jsonl")
  try (#"{"meta": {"started": 1793000000, "ended": false, "updated": \#(now)}}"# + "\n"
       + #"{"t": 12.5, "clock": "09:00:12", "who": "them", "text": "The parser drops the last row."}"# + "\n"
       + #"{"t": 3.0, "clock": "09:00:03", "who": "me", "text": "Morning, let's start with the parser."}"# + "\n")
    .write(to: jl, atomically: true, encoding: .utf8)
  try FileManager.default.createFile(atPath: into.appendingPathComponent(".live/20261028T090000.jsonl").path, contents: Data())
  #expect(LiveSession.newest(into: into.path) == jl)  // the newest stamp wins
  var s = try #require(LiveSession(jsonl: jl))
  #expect(s.entries.map(\.who) == ["me", "them"] && s.active() && s.insights == nil)
  #expect(!s.active(now: Date().addingTimeInterval(120)))  // stale after the active window
  #expect(s.search("which row does the parser drop").first?.t == 12.5)

  let before = LiveSession.stamp(jl)
  try #"""
  {"version": 1, "t": 12.5, "topics": [
     {"id": "release-2", "label": "Release", "rank": 2, "visits": 1, "share": 0.4, "new": true, "terms": ["notes"]},
     {"id": "parser-1", "label": "Parser", "rank": 1, "visits": 2, "share": 0.6, "speakingMs": 9000, "terms": ["row", "regex"]}],
   "nodes": [{"id": "parser-1"}, {"id": "parser-1/row"}],
   "summaries": [{"from": 0, "to": 300, "text": "Parser bug.", "bullets": ["The parser drops the last row."]}],
   "nextSteps": [{"id": "ns-1", "kind": "agreed", "who": "me", "t": 3, "text": "I'll send the fixture"},
                 {"id": "ns-2", "kind": "suggested", "who": "them", "t": 12, "text": "Maybe add a test"}]}
  """#.write(to: s.insightsURL, atomically: true, encoding: .utf8)
  #expect(LiveSession.stamp(jl) != before)  // a new insights file changes the key
  s = try #require(LiveSession(jsonl: jl))
  let i = try #require(s.insights)
  #expect(i.topics.map(\.id) == ["parser-1", "release-2"] && i.topics[0].visits == 2 && i.topics[1].isNew)
  #expect(i.topics[0].speakingSeconds == 9 && i.nodeCount == 2 && i.summaries.first?.bullets.count == 1)
  #expect(i.agreed.map(\.id) == ["ns-1"] && i.suggested.map(\.id) == ["ns-2"] && i.json.contains("parser-1/row"))
}

@Test func endedSessionIsNotActive() throws {
  let into = try liveDir()
  defer { try? FileManager.default.removeItem(at: into) }
  let jl = into.appendingPathComponent(".live/20261029T100000.jsonl")
  try #"{"meta": {"started": 1, "ended": true, "updated": \#(Date().timeIntervalSince1970)}}"#.write(to: jl, atomically: true, encoding: .utf8)
  let s = try #require(LiveSession(jsonl: jl))
  #expect(s.ended && !s.active() && s.entries.isEmpty)
  #expect(LiveSession.newest(into: "/nonexistent-\(UUID().uuidString)") == nil)
}

@Test func localWebAllowsOnlyFileAndAbout() {
  #expect(LocalWeb.allows(URL(fileURLWithPath: "/tmp/index.html")) && LocalWeb.allows(URL(string: "about:blank")))
  #expect(LocalWeb.allows(nil))
  for u in ["http://127.0.0.1:1/", "https://example.com/", "ws://localhost/", "data:text/html,hi", "javascript:alert(1)"] {
    #expect(!LocalWeb.allows(URL(string: u)), "\(u) must be blocked")
  }
}

/// G4: a file page that tries to leave for the network is stopped by the delegate.
/// (Not port 1: WebKit refuses its "bad ports" itself, before any policy check.)
@MainActor @Test func webViewNavigationToHttpIsCancelled() async throws {
  let dir = try liveDir()
  defer { try? FileManager.default.removeItem(at: dir) }
  let page = dir.appendingPathComponent("index.html")
  try "<script>setTimeout(function () { location.href = 'http://127.0.0.1:9419/probe' }, 50)</script>"
    .write(to: page, atomically: true, encoding: .utf8)
  let nav = LocalOnlyNavigation()
  let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 100, height: 100))
  web.navigationDelegate = nav
  let blocked: URL? = await withCheckedContinuation { cont in
    var done = false
    nav.onBlock = { u in if !done { done = true; cont.resume(returning: u) } }
    web.loadFileURL(page, allowingReadAccessTo: dir)
    Task { @MainActor in
      try? await Task.sleep(for: .seconds(10))
      if !done { done = true; cont.resume(returning: nil) }
    }
  }
  #expect(blocked?.absoluteString == "http://127.0.0.1:9419/probe")
  #expect(nav.blocked.count == 1 && web.url?.isFileURL == true)
}
