import AVFoundation
import Foundation

/// One diarized voice of a meeting, read from its `.diarization.json` sidecar.
public struct SpeakerCluster: Sendable, Equatable, Identifiable {
  public let cluster: String
  public let name: String
  public let role: String?
  public let turns: Int
  public let seconds: Double
  /// The closest registry voice that did not clear the match threshold, for an unnamed cluster.
  public let hint: Hint?
  public var id: String { cluster }
  public var unnamed: Bool { Transcript.isUnnamed(name) }

  public struct Hint: Sendable, Equatable {
    public let name: String
    public let similarity: Double
    public let threshold: Double?
  }
}

public enum Speakers {
  public static let roles = ["self", "boss", "peer", "report", "external"]

  /// A name `whosaid relabel` accepts.
  public static func validName(_ s: String) -> Bool { s.wholeMatch(of: /[A-Za-z0-9_-]+/) != nil }

  public static func clusters(sidecar: JSONValue) -> [SpeakerCluster] {
    let names = sidecar["names"]
    let roles = sidecar["roles"]
    var turns: [String: Int] = [:]
    var secs: [String: Double] = [:]
    var order: [String] = []
    for seg in sidecar["segments"]?.array ?? [] {
      guard let sp = seg["speaker"]?.string else { continue }
      if turns[sp] == nil { order.append(sp) }
      turns[sp, default: 0] += 1
      secs[sp, default: 0] += max(0, (seg["end"]?.double ?? 0) - (seg["start"]?.double ?? 0))
    }
    // Near misses: matched=false entries name the best registry candidate and its score.
    var hints: [String: SpeakerCluster.Hint] = [:]
    for m in sidecar["registry_matches"]?.array ?? [] where m["matched"]?.bool == false {
      guard let c = m["cluster"]?.string, let n = m["name"]?.string, !n.isEmpty,
            let sim = m["similarity"]?.double else { continue }
      if (hints[c]?.similarity ?? -1) < sim {
        hints[c] = .init(name: n, similarity: sim, threshold: m["threshold"]?.double)
      }
    }
    return order.map { c in
      let name = names?[c]?.string ?? c
      let unnamed = Transcript.isUnnamed(name)
      return SpeakerCluster(cluster: c, name: name, role: roles?[name]?.string, turns: turns[c] ?? 0,
                            seconds: secs[c] ?? 0, hint: unnamed ? hints[c] : nil)
    }
    .sorted { $0.seconds > $1.seconds }
  }
}

extension MeetingFolder {
  public func sidecarURL() -> URL? { first(".diarization.json") }

  public func speakerClusters() -> [SpeakerCluster] {
    guard let f = sidecarURL(), let data = try? Data(contentsOf: f), let v = try? JSONValue.parse(data) else { return [] }
    return Speakers.clusters(sidecar: v)
  }
}

/// The app's only audio playback. It is created lazily and plays only from a clip
/// button's action (D6): nothing plays on selection, hover or load. `created` counts
/// players so a test can show that browsing makes none.
@MainActor public final class ClipPlayer {
  public static let shared = ClipPlayer()
  public private(set) static var created = 0
  private var player: AVAudioPlayer?

  public func play(_ url: URL) {
    player?.stop()
    player = try? AVAudioPlayer(contentsOf: url)
    if player != nil { Self.created += 1 }
    player?.play()
  }

  public func stop() { player?.stop() }
}
