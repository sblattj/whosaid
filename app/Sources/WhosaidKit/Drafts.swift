import Foundation

/// One open outbox draft from `whosaid_drafts` (list or show): who it is to, its state and its pages.
public struct DraftRow: Sendable, Equatable, Identifiable {
  public let id: String
  public let to: String
  public let topic: String
  /// drafting, verified, stale, staged, or "verified, changed since staged"; a sent draft with a
  /// correction is "sent, edit drafting", "sent, edit verified" or "sent, edit staged" (#209).
  public let state: String
  public let pages: [String]
  public let mentions: [String]
  public let updatedAt: String
  public let owner: String
  /// Other open drafts to the same person; stage refuses until they are merged.
  public let rivals: Int
  /// The message (draft.md), from action=show only.
  public let text: String?
  /// Gates verify would still refuse, from action=show only.
  public let gate: [String]

  public init?(_ v: JSONValue) {
    guard let id = v["id"]?.string, let to = v["to"]?.string else { return nil }
    self.id = id
    self.to = to
    topic = v["topic"]?.string ?? ""
    state = v["state"]?.string ?? "drafting"
    pages = (v["pages"]?.array ?? []).compactMap(\.string)
    mentions = (v["mentions"]?.array ?? []).compactMap(\.string)
    updatedAt = v["updated_at"]?.string ?? ""
    owner = v["owner_line"]?.string ?? ""
    rivals = v["rivals"]?.int ?? 0
    text = v["text"]?.string
    gate = (v["gate"]?.array ?? []).compactMap { $0.string }
  }

  /// Verified and not staged with this content: the Stage button is live.
  public var stageable: Bool { (state.hasPrefix("verified") && rivals == 0) || state == "sent, edit verified" }
  public var staged: Bool { state == "staged" || state == "sent, edit staged" }
  /// A correction of a message already sent: Stage loads it into Teams' Edit box (edit=true); the user saves.
  public var isEdit: Bool { state.hasPrefix("sent, edit") }

  public var stateLabel: String {
    switch state {
    case "verified": "ready to stage"
    case "verified, changed since staged": "ready to stage again"
    case "staged": "staged, send it in Teams"
    case "stale": "edited after verify"
    case "sent, edit drafting": "correction, verify it"
    case "sent, edit verified": "correction ready to stage"
    case "sent, edit staged": "edit staged, save it in Teams"
    default: state
    }
  }
}

/// What one whosaid_draft_stage call did, as lines for the view.
public struct StageOutcome: Sendable, Equatable {
  public let ok: Bool
  public let staged: [String]
  public let lines: [String]

  public init(_ v: JSONValue) {
    var lines: [String] = []
    if v["ok"]?.bool == false {
      ok = false
      staged = []
      lines.append(v["error"]?.string ?? "stage failed")
      self.lines = lines
      return
    }
    staged = (v["staged"]?.array ?? []).compactMap { $0["id"]?.string }
    let edit = v["edit"]?.bool == true
    for s in v["staged"]?.array ?? [] {
      lines.append(edit ? "Edit loaded on your message to \(s["to"]?.string ?? "?"): check it in Teams and save it."
                        : "Staged for \(s["to"]?.string ?? "?"): open Teams, read it, press send.")
    }
    for w in v["warnings"]?.array ?? [] { if let w = w.string { lines.append("Warning: \(w)") } }
    for s in v["skipped"]?.array ?? [] { lines.append("Skipped: \(s["why"]?.string ?? "")") }
    let failed = v["failed"]?.array ?? []
    for s in failed { lines.append("Not staged: \(s["why"]?.string ?? "")") }
    if let n = v["note"]?.string, !n.isEmpty { lines.append(n) }
    if let h = v["history_note"]?.string, !h.isEmpty { lines.append(h) }
    ok = failed.isEmpty
    self.lines = lines.isEmpty ? ["Nothing to stage."] : lines
  }
}

public enum DraftsFolder {
  /// WHOSAID_DRAFTS_DIR, else ~/.drafts (lib/drafts.py root()).
  public static func url(_ env: [String: String] = ProcessInfo.processInfo.environment) -> URL {
    if let d = env["WHOSAID_DRAFTS_DIR"], !d.isEmpty { return URL(fileURLWithPath: (d as NSString).expandingTildeInPath) }
    return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".drafts")
  }
}
