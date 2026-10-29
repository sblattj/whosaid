import Foundation

/// One speaker turn from a `<base>.speakers.txt` line: `[HH:MM:SS] Name: text`.
public struct Turn: Sendable, Equatable, Identifiable {
  public let index: Int
  public let t: Double
  public let speaker: String
  public let text: String
  public var id: Int { index }
  public var unnamed: Bool { Transcript.isUnnamed(speaker) }
  public var clock: String { Transcript.clock(t) }
}

public enum Transcript {
  nonisolated(unsafe) static let line = /^\[(\d+):(\d{2}):(\d{2}(?:\.\d+)?)\] ([^:]+): (.*)$/

  public static func parse(_ text: String) -> [Turn] {
    var out: [Turn] = []
    for raw in text.split(separator: "\n", omittingEmptySubsequences: true) {
      guard let m = try? line.wholeMatch(in: String(raw)) else { continue }
      let h: Double = Double(m.1) ?? 0
      let mins: Double = Double(m.2) ?? 0
      let secs: Double = Double(m.3) ?? 0
      let t = h * 3600 + mins * 60 + secs
      out.append(Turn(index: out.count, t: t, speaker: String(m.4), text: String(m.5)))
    }
    return out
  }

  public static func isUnnamed(_ speaker: String) -> Bool { speaker.wholeMatch(of: /SPEAKER_\d+/) != nil }

  /// Seconds each speaker held the floor: a turn lasts until the next one starts. The
  /// last one gets what its words take to say (0.4 s each, at least 5 s), capped at `end`,
  /// so a quiet tail of the recording is not credited to whoever spoke last.
  public static func talkTime(_ turns: [Turn], end: Double? = nil) -> [(speaker: String, seconds: Double)] {
    var total: [String: Double] = [:]
    var order: [String] = []
    for (i, turn) in turns.enumerated() {
      let said = max(5, Double(turn.text.split(separator: " ").count) * 0.4)
      let next = i + 1 < turns.count ? turns[i + 1].t : min(turn.t + said, max(end ?? .infinity, turn.t))
      if total[turn.speaker] == nil { order.append(turn.speaker) }
      total[turn.speaker, default: 0] += max(0, next - turn.t)
    }
    return order.map { ($0, total[$0] ?? 0) }.sorted { $0.seconds > $1.seconds }
  }

  public static func speakers(_ turns: [Turn]) -> [String] {
    var seen: [String] = []
    for t in turns where !seen.contains(t.speaker) { seen.append(t.speaker) }
    return seen
  }

  public static func clock(_ secs: Double) -> String {
    let s = Int(secs)
    return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s % 3600 / 60, s % 60)
      : String(format: "%02d:%02d", s / 60, s % 60)
  }
}

/// A row of `whosaid_meetings`.
public struct MeetingSummary: Sendable, Equatable, Identifiable, Hashable {
  public let folder: String
  public let created: Date?
  public let duration: Double?
  public let kind: String?
  public let segments: Int
  public let sourceName: String?
  public var id: String { folder }

  public init?(_ v: JSONValue) {
    guard let f = v["folder"]?.string else { return nil }
    folder = f
    created = v["created"]?.string.flatMap { ISO8601DateFormatter().date(from: $0) } ?? Self.folderDate(f)
    duration = v["duration_s"]?.double
    kind = v["kind"]?.string
    segments = v["segments"]?.int ?? 0
    sourceName = v["source_name"]?.string
  }

  /// 2026-10-28-1400[-...] as local time: the folder name is the meeting's local start.
  public static func folderDate(_ folder: String) -> Date? {
    let p = folder.split(separator: "-").map(String.init)
    guard p.count >= 4, p[3].count == 4, let y = Int(p[0]), let mo = Int(p[1]), let d = Int(p[2]),
          let hm = Int(p[3]) else { return nil }
    return Calendar.current.date(from: DateComponents(year: y, month: mo, day: d, hour: hm / 100, minute: hm % 100))
  }

  public var isChat: Bool { kind == "teams-chat" || kind == "outlook-mail" }

  /// Real folders are YYYY-MM-DD-HHMM with no title, so a call is named by what
  /// recorded it; a folder that does carry a title part (2026-10-28-1400-planning) keeps it.
  /// Chats show their chat_name instead (MeetingFolder.chatName) where the sidecar has one.
  public var title: String {
    let parts = folder.split(separator: "-", maxSplits: 4).map(String.init)
    if parts.count == 5, parts[0].count == 4, Int(parts[0]) != nil, Int(parts[4]) == nil {
      return parts[4].replacingOccurrences(of: "-", with: " ").replacingOccurrences(of: "_", with: " ")
    }
    switch kind {
    case "teams-chat": return "Teams chat"
    case "outlook-mail": return "Email"
    default: return (sourceName ?? "").lowercased().contains("teams") ? "Teams call" : "Recording"
    }
  }

  public var length: String? {
    guard let d = duration, d > 0 else { return nil }
    let m = Int(d / 60)
    return m >= 60 ? "\(m / 60)h \(String(format: "%02d", m % 60))m" : "\(max(1, m))m"
  }
}

/// An item from a meeting folder's action-items.json.
public struct FolderItem: Sendable, Equatable, Identifiable {
  public let line: Int?
  public let owner: String
  public let text: String
  public let section: String
  public var id: String { "\(section)|\(line ?? -1)|\(text)" }
}

/// The files of one meeting folder, read directly (the transcript is not in the MCP surface).
public struct MeetingFolder: Sendable {
  public let url: URL

  public init(workspace: String, folder: String) {
    url = URL(fileURLWithPath: workspace).appendingPathComponent(folder)
  }

  func first(_ suffix: String) -> URL? {
    let names = (try? FileManager.default.contentsOfDirectory(atPath: url.path)) ?? []
    return names.filter { $0.hasSuffix(suffix) && !$0.hasPrefix(".") }.sorted().first.map { url.appendingPathComponent($0) }
  }

  public func turns() -> [Turn] {
    guard let f = first(".speakers.txt"), let s = try? String(contentsOf: f, encoding: .utf8) else { return [] }
    return Transcript.parse(s)
  }

  public func items() -> [FolderItem] {
    guard let data = try? Data(contentsOf: url.appendingPathComponent("action-items.json")),
          let v = try? JSONValue.parse(data) else { return [] }
    return (v["items"]?.array ?? []).compactMap { i in
      guard let text = i["text"]?.string else { return nil }
      return FolderItem(line: i["line"]?.int, owner: i["owner"]?.string ?? "", text: text,
                        section: i["section"]?.string ?? "Items")
    }
  }

  /// The Teams chat name a scraped chat's sidecar carries, if any.
  public func chatName() -> String? {
    guard let f = first(".diarization.json"), let data = try? Data(contentsOf: f),
          let v = try? JSONValue.parse(data), let name = v["source"]?["chat_name"]?.string,
          !name.isEmpty else { return nil }
    return name
  }

  public func speakerCards() -> String? {
    first(".speaker-cards.txt").flatMap { try? String(contentsOf: $0, encoding: .utf8) }
  }
}
