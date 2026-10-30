import Foundation

/// One meeting a library topic came up in (newest first in the file).
public struct TopicMeeting: Equatable, Sendable, Identifiable {
  public var folder: String
  public var date: String?
  public var label: String
  public var speakingMs: Int
  /// Seconds into the meeting where the topic starts: the transcript opens there.
  public var firstT: Double
  public var id: String { folder }
}

public struct LibraryTopic: Equatable, Sendable, Identifiable {
  public var id: String
  public var label: String
  public var rank: Int
  public var meetingCount: Int
  public var speakingMs: Int
  public var terms: [String]
  public var firstDate: String?
  public var lastDate: String?
  public var meetings: [TopicMeeting]
  /// The topic as the file has it, for the wordgraph page.
  public var raw: JSONValue
}

/// `<ws>/_topics.json` from `whosaid topics build`: topics across meetings, ranked by
/// number of meetings, then the latest one, each with its own wordgraph nodes.
public struct TopicsLibrary: Equatable, Sendable {
  public static let fileName = "_topics.json"
  public var built: String?
  public var meetingCount = 0
  public var topics: [LibraryTopic] = []
  var nodes: [JSONValue] = []

  public init?(data: Data) {
    guard let v = try? JSONValue.parse(data), v["version"]?.int == 1, let ts = v["topics"]?.array else { return nil }
    built = v["built"]?.string
    meetingCount = v["meetings"]?.int ?? 0
    nodes = v["nodes"]?.array ?? []
    topics = ts.compactMap { t in
      guard let id = t["id"]?.string else { return nil }
      let ms = (t["meetings"]?.array ?? []).compactMap { m -> TopicMeeting? in
        guard let folder = m["folder"]?.string else { return nil }
        return TopicMeeting(folder: folder, date: m["date"]?.string, label: m["label"]?.string ?? "",
                            speakingMs: m["speakingMs"]?.int ?? 0, firstT: m["firstT"]?.double ?? 0)
      }
      return LibraryTopic(id: id, label: t["label"]?.string ?? id, rank: t["rank"]?.int ?? 0,
                          meetingCount: t["visits"]?.int ?? ms.count, speakingMs: t["speakingMs"]?.int ?? 0,
                          terms: (t["terms"]?.array ?? []).compactMap(\.string),
                          firstDate: t["firstDate"]?.string, lastDate: t["lastDate"]?.string, meetings: ms, raw: t)
    }.sorted { ($0.rank, $0.id) < ($1.rank, $1.id) }
  }

  public static func url(workspace: String) -> URL {
    URL(fileURLWithPath: workspace).appendingPathComponent(fileName)
  }

  public static func read(workspace: String) -> TopicsLibrary? {
    FileManager.default.contents(atPath: url(workspace: workspace).path).flatMap(TopicsLibrary.init(data:))
  }

  /// `{"topics": [topic], "nodes": [its nodes]}`: what the wordgraph page reads.
  public func graphJSON(for id: String) -> String? {
    guard let t = topics.first(where: { $0.id == id }) else { return nil }
    let mine = nodes.filter { $0["topicId"]?.string == id }
    let obj = JSONValue.object(["topics": .array([t.raw]), "nodes": .array(mine)])
    return (try? obj.data()).map { String(decoding: $0, as: UTF8.self) }
  }

  /// "2026-09-15T09:00" -> "Sep 15, 2026"; nil or junk -> "".
  public static func dateLabel(_ s: String?) -> String {
    guard let s, s.count >= 10 else { return "" }
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "yyyy-MM-dd"
    guard let d = f.date(from: String(s.prefix(10))) else { return "" }
    return d.formatted(.dateTime.month(.abbreviated).day().year())
  }

  /// "4:05" from milliseconds; an hour or more reads "1:02:05".
  public static func duration(_ ms: Int) -> String {
    let s = ms / 1000
    return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s % 3600 / 60, s % 60) : String(format: "%d:%02d", s / 60, s % 60)
  }
}
