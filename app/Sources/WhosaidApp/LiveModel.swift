import Foundation
import AppKit
import WhosaidKit

/// The Live now workspace and the live panel: the newest `<recordings>/.live` session,
/// its insights, and the `whosaid live insights follow` child that keeps them fresh.
@MainActor @Observable
final class LiveModel {
  var session: LiveSession?
  var stamp = ""
  /// Wall time each entry count was first seen, for the self-test's latency check.
  var seenAt: [Int: Date] = [:]
  var fps: Double?
  var fpsNodes = 0
  var blockedNavigations = 0
  var webReady = false
  var selectedNode: String?
  var fullWordgraph = false
  var panelOpen = false
  var lanesVisible = false
  var asking = false
  var answer: LiveAnswer?
  private var timer: Timer?
  private var follower: Process?
  private var into = ""
  private var whosaid = ""

  var active: Bool { session?.active() ?? false }

  /// Poll once a second while someone looks (a lane, the panel) or a call is live.
  /// A stat per second; the session is re-read only when its size or mtime moves.
  func attach(paths: WhosaidPaths) {
    into = paths.recordings
    whosaid = paths.whosaid
    guard timer == nil else { return }
    timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
      Task { @MainActor in await AppModelLocator.model?.live.poll() }
    }
    Task { await poll() }
  }

  func poll() async {
    let dir = into
    let newStamp = await Task.detached { LiveSession.newest(into: dir).map(LiveSession.stamp) ?? "" }.value
    guard newStamp != stamp else { return }
    stamp = newStamp
    let read = await Task.detached { LiveSession.newest(into: dir).flatMap(LiveSession.init(jsonl:)) }.value
    session = read
    if let n = read?.entries.count, seenAt[n] == nil { seenAt[n] = Date() }
  }

  /// Keep `live insights follow` running while the newest session is live (or the
  /// self-test asks for it); stop it otherwise. One child at most.
  func syncFollower(callLive: Bool, force: Bool = false) {
    let want = force || callLive || active
    if want, follower?.isRunning != true, FileManager.default.isExecutableFile(atPath: whosaid) {
      let p = Process()
      p.executableURL = URL(fileURLWithPath: whosaid)
      p.arguments = ["live", "insights", "follow", "--into", into, "--poll", "1", "--max-hours", "12"]
      p.environment = WhosaidPaths.childEnvironment()
      p.standardOutput = FileHandle.nullDevice
      p.standardError = FileHandle.nullDevice
      p.standardInput = FileHandle.nullDevice
      if (try? p.run()) != nil { follower = p }
    } else if !want, let p = follower, p.isRunning {
      p.terminate()
      follower = nil
    }
  }

  func stopFollower() {
    if let p = follower, p.isRunning { p.terminate() }
    follower = nil
  }

  /// A finished session without insights gets them once (`live insights build`).
  func buildInsightsIfMissing() async {
    guard let s = session, s.insights == nil, !active else { return }
    _ = try? await CLIRunner(whosaid: whosaid).run(["live", "insights", "build", "--jsonl", s.jsonl.path], timeout: 30)
    stamp = ""
    await poll()
  }

  /// "Ask about this call": `whosaid live ask`, which searches the transcript unless the
  /// workspace opted into [summarizer] engine = "claude". No network from the app itself.
  func ask(_ question: String, workspace: String?) async {
    let q = question.trimmingCharacters(in: .whitespaces)
    guard !q.isEmpty else { return }
    asking = true
    defer { asking = false }
    var args = ["live", "ask", "--into", into, "--question", q, "--json"]
    if let workspace { args += ["--ws", workspace] }
    if let r = try? await CLIRunner(whosaid: whosaid).run(args, timeout: 120), let v = r.json, v["ok"]?.bool == true {
      answer = LiveAnswer(v, question: q)
    } else {
      // The CLI is missing or failed: fall back to the same search, in process.
      let hits = session?.search(q) ?? []
      answer = LiveAnswer(question: q, mode: "search", text: hits.isEmpty ? "Nothing in the live transcript matches." : "", matches: hits)
    }
  }
}

struct LiveAnswer: Equatable {
  let question: String
  let mode: String
  let text: String
  let matches: [LiveEntry]

  init(question: String, mode: String, text: String, matches: [LiveEntry]) {
    self.question = question; self.mode = mode; self.text = text; self.matches = matches
  }

  init(_ v: JSONValue, question: String) {
    self.question = question
    mode = v["mode"]?.string ?? "search"
    text = v["answer"]?.string ?? ""
    matches = (v["matches"]?.array ?? []).compactMap(LiveEntry.init)
  }
}
