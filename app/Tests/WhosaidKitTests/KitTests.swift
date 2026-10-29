import Foundation
import Testing
@testable import WhosaidKit

@Test func menuStateDecodes() throws {
  let v = try JSONValue.parse(Data(#"{"menubar":{"state":"recording-live","headline":"recording, live transcript on"},"autorecord":{"elapsed":42,"controls":true},"unnamed":{"voices":3}}"#.utf8))
  let s = AppState(raw: v)
  #expect(s.menu == .recordingLive)
  #expect(s.recording)
  #expect(s.elapsed == 42)
  #expect(s.controlsAvailable)
  #expect(s.unnamedVoices == 3)
}

@Test func unknownStateIsUnknown() throws {
  let s = AppState(raw: try JSONValue.parse(Data(#"{"menubar":{"state":"new-thing"}}"#.utf8)))
  #expect(s.menu == .unknown)
  #expect(!s.recording)
}

@Test func everyAppStateValueMaps() {
  for raw in ["warn", "agent-not-loaded", "recording-live", "recording", "paused", "ingesting", "waiting", "done", "idle"] {
    #expect(MenuState(rawValue: raw) != nil, "\(raw) must map")
  }
}

@Test func lineBufferSplitsAcrossChunks() {
  let b = LineBuffer()
  #expect(b.feed(Data("{\"a\":".utf8)).isEmpty)
  let lines = b.feed(Data("1}\n{\"b\":2}\n{\"c\"".utf8))
  #expect(lines.map { String(decoding: $0, as: UTF8.self) } == [#"{"a":1}"#, #"{"b":2}"#])
  #expect(b.feed(Data(":3}\n".utf8)).count == 1)
}

@Test func folderWatcherFiresOnNewEntry() async throws {
  let dir = FileManager.default.temporaryDirectory.appendingPathComponent("whosaid-fw-\(UUID().uuidString)")
  try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: dir) }
  let hits = Counter()
  let w = try #require(FolderWatcher(path: dir.path, debounce: 0.1) { hits.bump() })
  try Data("x".utf8).write(to: dir.appendingPathComponent("2026-10-29-1000"))
  for _ in 0..<40 where hits.value == 0 { try await Task.sleep(nanoseconds: 50_000_000) }
  #expect(hits.value == 1)
  w.cancel()
}

@Test func pathsResolveOrder() {
  let d = UserDefaults(suiteName: "whosaid-test-\(UUID().uuidString)")!
  d.set("/saved/whosaid", forKey: WhosaidPaths.defaultsKeys.whosaid)
  #expect(WhosaidPaths.resolve(env: [:], defaults: d, home: "/h").whosaid == "/saved/whosaid")
  #expect(WhosaidPaths.resolve(env: ["WHOSAID_BIN": "/env/whosaid"], defaults: d, home: "/h").whosaid == "/env/whosaid")
  let e = WhosaidPaths.resolve(env: [:], defaults: UserDefaults(suiteName: "whosaid-empty-\(UUID().uuidString)")!, home: "/h")
  #expect(e.whosaid == "/h/code/whosaid/whosaid" && e.workspace == nil && e.recordings == "/h/Recordings")
}

final class Counter: @unchecked Sendable {
  private let lock = NSLock()
  private var n = 0
  var value: Int { lock.lock(); defer { lock.unlock() }; return n }
  func bump() { lock.lock(); n += 1; lock.unlock() }
}
