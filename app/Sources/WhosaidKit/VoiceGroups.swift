import Foundation

/// The same unnamed voice across meetings, from `whosaid_voice_groups`: named once, it is
/// named in every meeting of the group.
public struct VoiceGroup: Sendable, Equatable, Identifiable {
  public let id: String
  public let meetings: Int
  public let seconds: Double
  public let first: String?
  public let last: String?
  public let recent: Bool
  public let hint: Hint?
  public let members: [Member]

  public struct Hint: Sendable, Equatable {
    public let name: String
    public let similarity: Double
    public let role: String?
  }

  public struct Member: Sendable, Equatable, Identifiable {
    public let id: String
    public let folder: String
    public let cluster: String
    public let seconds: Double
  }

  public init?(_ v: JSONValue) {
    guard let id = v["id"]?.string else { return nil }
    self.id = id
    meetings = v["meetings"]?.int ?? 0
    seconds = v["seconds"]?.double ?? 0
    first = v["first"]?.string
    last = v["last"]?.string
    recent = v["recent"]?.bool ?? false
    hint = v["hint"].flatMap { h in
      h["name"]?.string.map { Hint(name: $0, similarity: h["similarity"]?.double ?? 0, role: h["role"]?.string) }
    }
    members = (v["members"]?.array ?? []).compactMap { m in
      guard let id = m["id"]?.string, let f = m["folder"]?.string, let c = m["cluster"]?.string else { return nil }
      return Member(id: id, folder: f, cluster: c, seconds: m["seconds"]?.double ?? 0)
    }
  }

  /// The member to play: the one with the most talk (the list comes largest first).
  public var loudest: Member? { members.first }

  /// "3 meetings · 6 min · last 2026-03-10"
  public var summary: String {
    let mins = Int((seconds / 60).rounded())
    var s = "\(meetings) meeting\(meetings == 1 ? "" : "s") · \(mins < 1 ? "<1" : String(mins)) min"
    if let last { s += " · last \(last)" }
    return s
  }

  public static func list(_ reply: JSONValue) -> [VoiceGroup] {
    (reply["groups"]?.array ?? []).compactMap(VoiceGroup.init)
  }
}
