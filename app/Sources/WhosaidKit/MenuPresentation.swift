import Foundation

/// What the menu bar item and its menu show for one `whosaid app-state` snapshot.
/// The state rules live in app-state (one copy, shared with the SwiftBar plugin);
/// this only turns its menubar.state into a glyph, a title and which rows appear.
public struct MenuPresentation: Equatable, Sendable {
  public var symbol = "waveform"
  /// Text next to the glyph: the elapsed time while recording, else nil.
  public var title: String?
  public var headline = "starting"
  public var recording = false
  public var saving = false
  public var live = false
  /// stop-now and pause need a loop that reads control files (P1 `controls`).
  public var canStopNow = false
  public var canPause = false
  public var paused = false
  public var pausedUntil: Date?
  public var ingesting: String?
  public var unnamedVoices = 0

  public init(_ s: AppState?, now: Date = Date()) {
    guard let s else { return }
    let ar = s.raw["autorecord"]
    let since = Self.date(ar?["since"]?.string)
    let state = s.menu
    recording = state == .recording || state == .recordingLive
    saving = ar?["state"]?.string == "saving"
    live = state == .recordingLive
    paused = state == .paused
    pausedUntil = Self.date(ar?["paused_until"]?.string)
    canStopNow = recording && !saving && s.controlsAvailable
    canPause = s.controlsAvailable && !paused
    unnamedVoices = s.unnamedVoices
    ingesting = state == .ingesting ? (s.raw["watch"]?["detail"]?["msg"]?.string ?? "") : nil
    title = recording && !saving ? since.map { Self.elapsed(now.timeIntervalSince($0)) } : nil
    switch state {
    case .warn: symbol = "exclamationmark.triangle.fill"; headline = "Needs a look: " + Self.tail(s.headline)
    case .agentNotLoaded: symbol = "exclamationmark.triangle"; headline = s.headline.prefix(1).uppercased() + s.headline.dropFirst()
    case .recordingLive: symbol = "record.circle.fill"; headline = "Recording, live transcript on"
    case .recording: symbol = "record.circle.fill"; headline = saving ? "Saving the recording" : "Recording a call"
    case .paused:
      symbol = "pause.circle"
      headline = "Autorecord paused" + (pausedUntil.map { " until " + Self.clock($0) } ?? "")
    case .ingesting: symbol = "gearshape.2"; headline = "Transcribing the last call"
    case .waiting: symbol = "hourglass"; headline = "Waiting for the recording to settle"
    case .done: symbol = "checkmark.circle"; headline = "Just transcribed"
    case .idle, .unknown: symbol = "waveform"; headline = "Idle: autorecord is waiting for a call"
    }
  }

  /// 12:41 under an hour, 1:02:41 after.
  public static func elapsed(_ secs: TimeInterval) -> String {
    let t = max(0, Int(secs))
    let (h, m, s) = (t / 3600, t % 3600 / 60, t % 60)
    return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
  }

  /// RFC 3339 with a colon offset (app-state), with or without fractional seconds.
  public static func date(_ s: String?) -> Date? {
    guard let s else { return nil }
    let f = ISO8601DateFormatter()
    if let d = f.date(from: s) { return d }
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f.date(from: s)
  }

  static func clock(_ d: Date) -> String {
    let f = DateFormatter()
    f.dateFormat = "h:mm a"
    return f.string(from: d)
  }

  static func tail(_ headline: String) -> String {
    var t = headline.hasPrefix("needs a look: ") ? String(headline.dropFirst(14)) : headline
    if t.hasPrefix("! ") { t.removeFirst(2) }
    return t
  }
}
