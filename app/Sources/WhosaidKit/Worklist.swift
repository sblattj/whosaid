import Foundation

/// One row of `whosaid_worklist` or `whosaid_items`: a commitment (CM-NNN) or an
/// action item (AI-NNN) with its status, rank and where it was said.
public struct TrackedItem: Sendable, Equatable, Identifiable {
  public let id: String
  public let text: String
  public let status: String
  public let owner: String
  /// P1 / P2 / P3 from the worklist; nil for a plain items row.
  public let tier: String?
  public let why: [String]
  public let kind: String?
  /// The section of the meeting's action items ("Asks from the team"), for items rows.
  public let section: String?
  /// The meeting it was said in and the second, when the evidence carries one.
  public let meeting: String?
  public let t: Double?

  public static let doneStatuses: Set<String> = ["delivered", "superseded", "done"]
  public var isDone: Bool { Self.doneStatuses.contains(status) }

  public init?(_ v: JSONValue) {
    guard let id = (v["id"] ?? v["item_id"])?.string, let text = v["text"]?.string else { return nil }
    self.id = id
    self.text = text
    status = v["status"]?.string ?? "open"
    owner = v["owner"]?.string ?? ""
    let tier = v["tier"]?.string ?? ""
    self.tier = tier.isEmpty ? nil : tier
    why = (v["why"]?.array ?? []).compactMap(\.string)
    kind = v["kind"]?.string
    section = v["type"]?.string
    // The first piece of evidence that names a meeting wins; else where it was first seen.
    let ev = (v["acceptance_evidence"]?.array ?? []) + (v["evidence"]?.array ?? [])
    let located = ev.first { $0["meeting"]?.string != nil && $0["t_sec"]?.double != nil }
      ?? ev.first { $0["meeting"]?.string != nil }
    meeting = located?["meeting"]?.string ?? v["first_seen"]?.string
    t = located?["t_sec"]?.double
  }

  /// "explicit_commitment" -> "explicit commitment".
  public var kindLabel: String? { kind.map { $0.replacingOccurrences(of: "_", with: " ") } }
}

/// One `whosaid_tracking_suggestions` candidate: a later statement that may show an item is done.
public struct CompletionSuggestion: Sendable, Equatable, Identifiable {
  public let id: String
  public let itemId: String
  public let status: String
  public let quote: String
  public let speaker: String?
  public let reason: String
  public let meeting: String?
  public let t: Double?

  public init?(_ v: JSONValue) {
    guard let item = v["item_id"]?.string else { return nil }
    let ev = v["evidence"]
    itemId = item
    id = v["id"]?.string ?? item
    status = v["status"]?.string ?? "delivered"
    quote = ev?["quote"]?.string ?? ""
    speaker = ev?["speaker"]?.string
    reason = v["reason"]?.string ?? ""
    meeting = ev?["meeting"]?.string
    t = ev?["t_sec"]?.double
  }

  /// The manual evidence `whosaid_set_status` records when the user accepts this suggestion.
  public var note: String {
    var n = "Marked done in the whosaid app after reviewing: \"\(quote)\""
    if let speaker { n += " (\(speaker)" + (meeting.map { ", \($0)" } ?? "") + ")" }
    return n
  }
}

public enum ItemFilter: String, CaseIterable, Identifiable, Sendable {
  case open = "Open", done = "Done", all = "All"
  public var id: String { rawValue }
  public func keeps(_ i: TrackedItem) -> Bool {
    switch self {
    case .open: !i.isDone
    case .done: i.isDone
    case .all: true
    }
  }
}
