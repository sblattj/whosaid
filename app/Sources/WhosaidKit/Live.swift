import Foundation

/// One line of a live transcript (`<into>/.live/<stem>.jsonl`, written by `whosaid live`).
public struct LiveEntry: Sendable, Equatable, Identifiable {
  public let t: Double
  public let clock: String
  /// "me" (your mic) or "them" (the meeting app).
  public let who: String
  public let text: String
  public var id: String { "\(t)|\(who)" }

  public init?(_ v: JSONValue) {
    guard let t = v["t"]?.double, let text = v["text"]?.string else { return nil }
    self.t = t
    self.text = text
    clock = v["clock"]?.string ?? Transcript.clock(t)
    who = v["who"]?.string ?? "them"
  }
}

public struct LiveTopic: Sendable, Equatable, Identifiable {
  public let id: String
  public let label: String
  public let rank: Int
  public let visits: Int
  public let share: Double
  public let isNew: Bool
  public let firstT: Double
  public let lastT: Double
  public let terms: [String]
  public let speakingSeconds: Double

  init?(_ v: JSONValue) {
    guard let id = v["id"]?.string, let label = v["label"]?.string else { return nil }
    self.id = id
    self.label = label
    rank = v["rank"]?.int ?? 0
    visits = v["visits"]?.int ?? 1
    share = v["share"]?.double ?? 0
    isNew = v["new"]?.bool ?? false
    firstT = v["firstT"]?.double ?? 0
    lastT = v["lastT"]?.double ?? 0
    terms = (v["terms"]?.array ?? []).compactMap(\.string)
    speakingSeconds = (v["speakingMs"]?.double ?? 0) / 1000
  }
}

public struct LiveSummary: Sendable, Equatable, Identifiable {
  public let from: Double
  public let to: Double
  public let text: String
  public let bullets: [String]
  public var id: Double { from }

  init?(_ v: JSONValue) {
    guard let from = v["from"]?.double else { return nil }
    self.from = from
    to = v["to"]?.double ?? from
    text = v["text"]?.string ?? ""
    bullets = (v["bullets"]?.array ?? []).compactMap(\.string)
  }
}

public struct NextStep: Sendable, Equatable, Identifiable {
  public let id: String
  /// "agreed" or "suggested".
  public let kind: String
  public let who: String
  public let t: Double
  public let clock: String
  public let text: String

  init?(_ v: JSONValue) {
    guard let text = v["text"]?.string else { return nil }
    self.text = text
    t = v["t"]?.double ?? 0
    id = v["id"]?.string ?? "\(t)"
    kind = v["kind"]?.string ?? "suggested"
    who = v["who"]?.string ?? "them"
    clock = v["clock"]?.string ?? Transcript.clock(t)
  }
}

/// `<stem>.insights.json` from `whosaid live insights`: topic buckets, wordgraph nodes,
/// 5-minute summaries and next steps. `json` is the file as read, for the wordgraph page.
public struct LiveInsights: Sendable, Equatable {
  public let topics: [LiveTopic]
  public let nodeCount: Int
  public let summaries: [LiveSummary]
  public let nextSteps: [NextStep]
  public let t: Double
  public let json: String
  /// The opt-in AI layer (`[live] ai = "claude"`): its model and recap, nil when off.
  public let aiModel: String?
  public let aiRecap: [String]

  public init?(data: Data) {
    guard let v = try? JSONValue.parse(data), v["version"] != nil else { return nil }
    topics = (v["topics"]?.array ?? []).compactMap(LiveTopic.init).sorted { $0.rank < $1.rank }
    nodeCount = v["nodes"]?.array?.count ?? 0
    summaries = (v["summaries"]?.array ?? []).compactMap(LiveSummary.init)
    nextSteps = (v["nextSteps"]?.array ?? []).compactMap(NextStep.init)
    t = v["t"]?.double ?? 0
    json = String(decoding: data, as: UTF8.self)
    aiModel = v["ai"]?["model"]?.string
    aiRecap = (v["ai"]?["recap"]?.array ?? []).compactMap(\.string)
  }

  public var agreed: [NextStep] { nextSteps.filter { $0.kind == "agreed" } }
  public var suggested: [NextStep] { nextSteps.filter { $0.kind != "agreed" } }
}

/// The newest live session under a recordings folder, read off the main thread.
public struct LiveSession: Sendable, Equatable {
  public let jsonl: URL
  public let started: Date?
  public let ended: Bool
  public let updated: Date?
  public let entries: [LiveEntry]
  public let insights: LiveInsights?

  /// Seconds after `updated` within which an unfinished session counts as live (live.py's ACTIVE_WINDOW).
  public static let activeWindow: TimeInterval = 90
  public func active(now: Date = Date()) -> Bool {
    !ended && (updated.map { now.timeIntervalSince($0) < Self.activeWindow } ?? false)
  }

  public var insightsURL: URL { Self.insightsURL(jsonl) }
  public static func insightsURL(_ jsonl: URL) -> URL {
    jsonl.deletingPathExtension().appendingPathExtension("insights.json")
  }

  /// `<into>/.live/*.jsonl`, newest (by name, which is the start stamp) last.
  public static func newest(into: String) -> URL? {
    let dir = URL(fileURLWithPath: into).appendingPathComponent(".live")
    let names = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
    return names.filter { $0.hasSuffix(".jsonl") }.sorted().last.map { dir.appendingPathComponent($0) }
  }

  /// A cheap change key: sizes and mtimes of the session and its insights.
  public static func stamp(_ jsonl: URL) -> String {
    func one(_ u: URL) -> String {
      let a = try? FileManager.default.attributesOfItem(atPath: u.path)
      return "\(a?[.size] ?? 0)@\((a?[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0)"
    }
    return jsonl.path + "|" + one(jsonl) + "|" + one(insightsURL(jsonl))
  }

  public init?(jsonl: URL) {
    guard let text = try? String(contentsOf: jsonl, encoding: .utf8) else { return nil }
    var meta: JSONValue?
    var entries: [LiveEntry] = []
    for line in text.split(separator: "\n") where !line.isEmpty {
      guard let v = try? JSONValue.parse(Data(line.utf8)) else { continue }
      if let m = v["meta"] { meta = m } else if let e = LiveEntry(v) { entries.append(e) }
    }
    self.jsonl = jsonl
    started = meta?["started"]?.double.map { Date(timeIntervalSince1970: $0) }
    updated = meta?["updated"]?.double.map { Date(timeIntervalSince1970: $0) }
    ended = meta?["ended"]?.bool ?? false
    self.entries = entries.sorted { $0.t < $1.t }
    insights = (try? Data(contentsOf: Self.insightsURL(jsonl))).flatMap(LiveInsights.init(data:))
  }

  /// Lines matching any word of the question, best first (the panel's no-engine answer).
  public func search(_ question: String, limit: Int = 5) -> [LiveEntry] {
    let words = Set(question.lowercased().split { !$0.isLetter && !$0.isNumber }.map(String.init).filter { $0.count > 2 })
    guard !words.isEmpty else { return [] }
    let scored = entries.compactMap { e -> (Int, LiveEntry)? in
      let have = Set(e.text.lowercased().split { !$0.isLetter && !$0.isNumber }.map(String.init))
      let n = words.intersection(have).count
      return n > 0 ? (n, e) : nil
    }
    return scored.sorted { $0.0 != $1.0 ? $0.0 > $1.0 : $0.1.t > $1.1.t }.prefix(limit).map(\.1)
  }
}
