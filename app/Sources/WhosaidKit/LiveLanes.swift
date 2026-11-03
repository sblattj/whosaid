import Foundation

/// The Live now page's five lanes, and which of them the user hid. Stored as one
/// comma-separated `live.lanes.hidden` default, so the page and the View menu share it.
public enum LiveLane: String, CaseIterable, Sendable {
  case transcript, topics, wordgraph, summaries, nextSteps

  public var title: String {
    switch self {
    case .transcript: "Transcript"
    case .topics: "Topics"
    case .wordgraph: "Wordgraph"
    case .summaries: "Summaries"
    case .nextSteps: "Next steps"
    }
  }

  /// Hidden lanes from the stored string; unknown names are ignored, and a string that
  /// would hide every lane hides none.
  public static func hidden(_ stored: String) -> Set<LiveLane> {
    let set = Set(stored.split(separator: ",").compactMap { LiveLane(rawValue: String($0)) })
    return set.count == allCases.count ? [] : set
  }

  /// The stored string after showing or hiding `lane`. The last visible lane stays shown.
  public static func toggled(_ lane: LiveLane, in stored: String) -> String {
    var set = hidden(stored)
    if set.contains(lane) { set.remove(lane) } else if set.count < allCases.count - 1 { set.insert(lane) }
    return allCases.filter(set.contains).map(\.rawValue).joined(separator: ",")
  }
}
