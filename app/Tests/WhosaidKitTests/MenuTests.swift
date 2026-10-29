import Foundation
import Testing
@testable import WhosaidKit

/// G6: one real `whosaid app-state` snapshot per menu bar state, built by
/// test/app_state_fixtures.py from fixture logs and state files.
enum StateFixtures {
  static let dir: URL? = {
    guard let bin = ProcessInfo.processInfo.environment["WHOSAID_TEST_BIN"] else { return nil }
    let repo = URL(fileURLWithPath: bin).deletingLastPathComponent()
    let out = FileManager.default.temporaryDirectory.appendingPathComponent("whosaid-menu-\(UUID().uuidString)")
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
    p.arguments = [repo.appendingPathComponent("test/app_state_fixtures.py").path, "--out", out.path]
    p.standardOutput = FileHandle.nullDevice
    try? p.run()
    p.waitUntilExit()
    return p.terminationStatus == 0 ? out : nil
  }()

  static func load(_ name: String) throws -> AppState {
    let dir = try #require(dir, "fixtures were not built")
    return AppState(raw: try JSONValue.parse(Data(contentsOf: dir.appendingPathComponent("\(name).json"))))
  }
}

@Suite(.enabled(if: ProcessInfo.processInfo.environment["WHOSAID_TEST_BIN"] != nil))
struct MenuStateFixtureTests {
  @Test(arguments: [
    ("idle", "waveform"), ("waiting", "hourglass"), ("ingesting", "gearshape.2"), ("done", "checkmark.circle"),
    ("warn", "exclamationmark.triangle.fill"), ("agent-not-loaded", "exclamationmark.triangle"),
    ("recording", "record.circle.fill"), ("recording-live", "record.circle.fill"), ("paused", "pause.circle"),
    ("saving", "record.circle.fill"),
  ])
  func glyph(name: String, symbol: String) throws {
    let p = MenuPresentation(try StateFixtures.load(name))
    #expect(p.symbol == symbol, "\(name)")
    #expect(!p.headline.isEmpty && !p.headline.contains("%"), "\(name): \(p.headline)")
  }

  @Test func recordingShowsElapsedAndStop() throws {
    let s = try StateFixtures.load("recording")
    let since = try #require(MenuPresentation.date(s.raw["autorecord"]?["since"]?.string))
    let p = MenuPresentation(s, now: since.addingTimeInterval(761))
    #expect(p.title == "12:41")
    #expect(p.recording && p.canStopNow && !p.live)
  }

  @Test func liveFlag() throws {
    let p = MenuPresentation(try StateFixtures.load("recording-live"))
    #expect(p.live && p.recording)
  }

  @Test func savingCannotStop() throws {
    let p = MenuPresentation(try StateFixtures.load("saving"))
    #expect(p.saving && !p.canStopNow && p.title == nil && p.headline == "Saving the recording")
  }

  @Test func pausedHasEndAndNoPause() throws {
    let p = MenuPresentation(try StateFixtures.load("paused"))
    #expect(p.paused && !p.canPause && p.pausedUntil != nil && p.headline.hasPrefix("Autorecord paused until "))
  }

  @Test func warnNamesTheProblem() throws {
    let p = MenuPresentation(try StateFixtures.load("warn"))
    #expect(p.headline == "Needs a look: ingest failed: a.m4a (exit 1)")
  }

  @Test func idleHasNoControlsWithoutALoop() throws {
    let p = MenuPresentation(try StateFixtures.load("idle"))
    #expect(!p.canStopNow && !p.canPause && p.title == nil)
  }
}

@Test func elapsedFormat() {
  #expect(MenuPresentation.elapsed(5) == "0:05")
  #expect(MenuPresentation.elapsed(761) == "12:41")
  #expect(MenuPresentation.elapsed(3761) == "1:02:41")
  #expect(MenuPresentation.elapsed(-3) == "0:00")
}

@Test func rfc3339WithColonOffset() {
  #expect(MenuPresentation.date("2026-10-29T15:00:00-07:00") != nil)
  #expect(MenuPresentation.date("2026-10-29T22:00:00Z") != nil)
  #expect(MenuPresentation.date("2026-10-29T15:00:00") == nil)
}

@Test func noSnapshotYet() {
  let p = MenuPresentation(nil)
  #expect(p.headline == "starting" && !p.recording)
}
