// record_app.swift: capture one app's audio (and optionally the mic) via ScreenCaptureKit.
//
// Built on first use by `whosaid record --app` (see ensure_record_app_helper in
// the whosaid script) and cached under ~/.cache/whosaid, keyed on this file's hash.
//
//   record-app --list
//   record-app --app <bundle-id|name> --out-dir DIR [--no-mic] [--max-seconds N]
//              [--wait-for-call] [--until-call-ends] [--idle-seconds N] [--mic-always]
//   record-app --call-state --app <bundle-id|name>
//   record-app --mute-state --app <bundle-id>   (read-only: what the mute gate sees)
//   record-app --selftest-mute                  (offline unit test of the mute gate)
//   record-app --teams-api-listen S             (read-only: Teams local API events for S s)
//
// Writes DIR/app.wav and, unless --no-mic, DIR/mic.wav: mono 16 kHz, both on
// the same timeline (sample 0 = the first buffer either leg delivered), padded
// to the same length so the caller can join them into one stereo file.
//
// ScreenCaptureKit only delivers app buffers while the app is producing sound,
// so each leg is placed by presentation timestamp and gaps are zero-filled;
// appending buffers back to back would drift the two legs apart on every pause.
// SCK also refuses to run audio-only, so a 2x2 video stream at 1 fps rides
// along and its frames are discarded. Ctrl-C / SIGTERM stops capture and
// closes the files (AVAudioFile finalizes the WAV header on close); exit 0.
// If macOS kills the stream mid-capture the files are closed the same way and
// the exit is 3 (5 if the Screen Recording grant was revoked). A second signal
// exits at once (130), and so does any signal before capture starts. If
// ScreenCaptureKit does not answer within 20 s at startup
// ($WHOSAID_RECORD_APP_STARTUP_TIMEOUT), the exit is 1: a hung helper would
// otherwise hold its connection and break every later capture from this binary.
//
// Call detection (--wait-for-call, --until-call-ends, --call-state) reads Core
// Audio's per-process objects (macOS 14+, no permission): an app is "in a call"
// while any of its processes, the bundle ID itself or a `<bundle>.*` helper,
// has an input (mic) stream running. Native Teams plays and records through
// com.microsoft.teams2.modulehost, which only the helper match finds.
//
// Meeting mute (#224): with the mic on, a Teams capture writes the mic leg as
// zeros while Teams' own mic button reads "Unmute ..." (see MuteWatcher). It
// needs an Accessibility grant for the responsible app (the terminal, or
// Whosaid Call Recorder under autorecord); without one, or for any other app,
// the mic is recorded and `mute: unknown (...)` is logged once. --mic-always
// skips the gate.

import AppKit
import AVFoundation
import CoreAudio
import CoreMedia
import Foundation
@preconcurrency import ScreenCaptureKit

let sampleRate = 16_000.0

func log(_ s: String) {
    FileHandle.standardError.write(("record-app: " + s + "\n").data(using: .utf8)!)
}

/// Every exit goes through here. exit() runs atexit handlers, and with a
/// ScreenCaptureKit connection open those can block forever; a helper stuck
/// that way keeps its connection, and macOS then interrupts every new capture
/// started from the same binary. The WAV files are closed before this runs.
func terminate(_ code: Int32) -> Never {
    fflush(stdout)
    _exit(code)
}

/// Kills the process if ScreenCaptureKit has not answered by the deadline, so
/// a hung call can never leave an orphan behind (see terminate).
func watchdog(_ seconds: Double, _ what: String, code: Int32 = 1) -> DispatchWorkItem {
    let item = DispatchWorkItem {
        log("\(what) got no answer from ScreenCaptureKit within \(Int(seconds))s; giving up.")
        if code != 0 {
            log("If this repeats, a stuck helper may hold the capture: pgrep -fl record-app, then kill it.")
        }
        terminate(code)
    }
    DispatchQueue.global().asyncAfter(deadline: .now() + seconds, execute: item)
    return item
}

func isPermissionError(_ error: Error) -> Bool {
    let ns = error as NSError
    if ns.domain == "com.apple.ScreenCaptureKit.SCStreamErrorDomain" && ns.code == -3801 { return true }
    let d = ns.localizedDescription.lowercased()
    return d.contains("not authorized") || d.contains("not granted") || d.contains("declined")
        || d.contains("permission")
}

func permissionExit() -> Never {
    permissionHelp()
    terminate(5)
}

func permissionHelp() {
    log("Screen & System Audio Recording permission is missing for your terminal app.")
    log("Fix: System Settings -> Privacy & Security -> Screen & System Audio Recording -> enable your terminal, then restart it.")
    log("Open it with: open \"x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture\"")
}

func shareableContent() async -> SCShareableContent {
    do {
        return try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    } catch {
        if isPermissionError(error) { permissionExit() }
        log("ScreenCaptureKit failed: \(error.localizedDescription)")
        terminate(1)
    }
}

/// One app per bundle ID, named, sorted by name.
func uniqueApps(_ content: SCShareableContent) -> [SCRunningApplication] {
    Dictionary(grouping: content.applications, by: { $0.bundleIdentifier })
        .compactMap { $0.value.first }
        .filter { !$0.applicationName.isEmpty && !$0.bundleIdentifier.isEmpty }
        .sorted { $0.applicationName.lowercased() < $1.applicationName.lowercased() }
}

/// Exact bundle ID first, then case-insensitive app name, then a unique
/// case-insensitive substring of either.
func resolveApp(_ query: String, in apps: [SCRunningApplication]) -> SCRunningApplication? {
    if let a = apps.first(where: { $0.bundleIdentifier == query }) { return a }
    let q = query.lowercased()
    if let a = apps.first(where: { $0.applicationName.lowercased() == q }) { return a }
    let partial = apps.filter {
        $0.applicationName.lowercased().contains(q) || $0.bundleIdentifier.lowercased().contains(q)
    }
    if partial.count == 1 { return partial[0] }
    if partial.count > 1 {
        log("'\(query)' matches several apps: " + partial.map { $0.bundleIdentifier }.joined(separator: ", "))
    }
    return nil
}

// ---- call detection (Core Audio process objects) -----------------------------

func audioProperty<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector, _ value: inout T) -> Bool {
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<T>.size)
    return withUnsafeMutablePointer(to: &value) {
        AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, $0) == noErr
    }
}

/// (input, output): whether any process of `bundle` (or a `bundle.*` helper)
/// has a mic / speaker stream running right now.
func callState(_ bundle: String) -> (input: Bool, output: Bool) {
    let system = AudioObjectID(kAudioObjectSystemObject)
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyProcessObjectList,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size) == noErr, size > 0 else { return (false, false) }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids) == noErr else { return (false, false) }
    var input = false, output = false
    for id in ids {
        var cf: Unmanaged<CFString>?
        guard audioProperty(id, kAudioProcessPropertyBundleID, &cf), let b = cf?.takeRetainedValue() as String?,
              b == bundle || b.hasPrefix(bundle + ".") else { continue }
        var on: UInt32 = 0
        if audioProperty(id, kAudioProcessPropertyIsRunningInput, &on), on != 0 { input = true }
        on = 0
        if audioProperty(id, kAudioProcessPropertyIsRunningOutput, &on), on != 0 { output = true }
    }
    return (input, output)
}

/// One output leg: converts to mono 16 kHz Float32 and writes at the sample
/// offset its timestamp implies, zero-filling any gap.
final class Leg {
    let name: String
    let file: AVAudioFile
    let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false)!
    var converter: AVAudioConverter?
    var written: Int64 = 0
    var peak: Float = 0
    var buffers = 0
    /// Timestamp position minus written position at the last buffer, before
    /// correction. Stays near zero when the leg tracks the shared clock.
    var skew: Int64 = 0
    var filled: Int64 = 0
    var dropped: Int64 = 0

    init(name: String, url: URL) throws {
        self.name = name
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
        ]
        file = try AVAudioFile(forWriting: url, settings: settings, commonFormat: .pcmFormatFloat32, interleaved: false)
    }

    func convert(_ input: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        if input.format == target { return input }
        if converter == nil || converter!.inputFormat != input.format {
            converter = AVAudioConverter(from: input.format, to: target)
        }
        guard let conv = converter else { return nil }
        let cap = AVAudioFrameCount(Double(input.frameLength) * sampleRate / input.format.sampleRate) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: cap) else { return nil }
        var fed = false
        var err: NSError?
        conv.convert(to: out, error: &err) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return input
        }
        if let err { log("\(name): convert failed: \(err.localizedDescription)"); return nil }
        return out
    }

    func writeZeros(_ n: Int64) {
        var left = n
        while left > 0 {
            let chunk = AVAudioFrameCount(min(left, 16_000))
            guard let z = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: chunk) else { return }
            z.frameLength = chunk  // AVAudioPCMBuffer memory starts zeroed
            try? file.write(from: z)
            written += Int64(chunk)
            left -= Int64(chunk)
        }
    }

    /// `offset`: where this buffer belongs on the shared timeline, in samples.
    func append(_ input: AVAudioPCMBuffer, at offset: Int64) {
        guard var buf = convert(input), buf.frameLength > 0 else { return }
        // Correct only past 20 ms so timestamp jitter never splices a
        // continuous leg. Behind its timestamps (the app went quiet and SCK
        // stopped sending): fill the gap with silence. Ahead of them (a device
        // clock running fast, delivering more samples than time passed): drop
        // the overlap, or the legs drift apart over a long meeting.
        let tolerance = Int64(sampleRate / 50)
        skew = offset - written
        if skew > tolerance {
            writeZeros(skew)
            filled += skew
        } else if -skew > tolerance {
            let drop = Int(min(-skew, Int64(buf.frameLength)))
            dropped += Int64(drop)
            guard drop < Int(buf.frameLength), let rest = tail(of: buf, from: drop) else { return }
            buf = rest
        }
        if let ch = buf.floatChannelData?[0] {
            for i in 0..<Int(buf.frameLength) { peak = max(peak, abs(ch[i])) }
        }
        do { try file.write(from: buf) } catch { log("\(name): write failed: \(error.localizedDescription)") }
        written += Int64(buf.frameLength)
        buffers += 1
    }

    func tail(of buf: AVAudioPCMBuffer, from start: Int) -> AVAudioPCMBuffer? {
        let n = Int(buf.frameLength) - start
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: AVAudioFrameCount(n)),
              let src = buf.floatChannelData?[0], let dst = out.floatChannelData?[0] else { return nil }
        dst.update(from: src + start, count: n)
        out.frameLength = AVAudioFrameCount(n)
        return out
    }

    /// e.g. "skew +3ms, filled 1.2s, dropped 0ms" for the progress line.
    func driftSummary() -> String {
        String(format: "skew %+.0fms, filled %.1fs, dropped %.0fms", Double(skew) / sampleRate * 1000,
               Double(filled) / sampleRate, Double(dropped) / sampleRate * 1000)
    }

    func takePeakDB() -> String {
        defer { peak = 0 }
        return peak > 0 ? String(format: "%.0f dB", 20 * log10(peak)) : "silent"
    }
}

func pcmBuffer(_ sb: CMSampleBuffer) -> AVAudioPCMBuffer? {
    guard CMSampleBufferIsValid(sb), CMSampleBufferDataIsReady(sb),
          let fd = CMSampleBufferGetFormatDescription(sb) else { return nil }
    let fmt = AVAudioFormat(cmAudioFormatDescription: fd)
    let n = AVAudioFrameCount(CMSampleBufferGetNumSamples(sb))
    guard n > 0, let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: n) else { return nil }
    buf.frameLength = n
    let st = CMSampleBufferCopyPCMDataIntoAudioBufferList(sb, at: 0, frameCount: Int32(n), into: buf.mutableAudioBufferList)
    return st == noErr ? buf : nil
}

// ---- meeting mute (Accessibility read of the Teams mic button, #224) ----------
//
// While Teams shows you muted, the mic leg is written as zeros (never dropped),
// so a muted aside does not end up in the left channel and the two legs stay on
// one timeline. Read-only: the helper only copies AX attributes (AXDescription,
// AXTitle, AXHelp) of the Teams window; it never presses, focuses or sets
// anything. Every doubt records the mic: no Accessibility grant, no button, an
// unrecognised or conflicting label, or a reading older than muteMaxAge.
//
// The label wording comes from the Teams client (and the browser gate in
// lib/record_browser.py): "Mute (⌘+Shift+M)" while live, "Unmute (⌘+Shift+M)"
// while muted. UNVERIFIED on the native client: the probe that would confirm
// which AX attribute carries it ran without an Accessibility grant (#224).
// `record-app --mute-state --app com.microsoft.teams2` prints what it sees.

enum MuteState: String { case muted, unmuted, unknown }

/// The mute state one control's label implies, or nil when it is not the
/// meeting's own mic button. Only "Mute"/"Unmute" alone, followed by a
/// shortcut hint "(...)" or by "mic"/"microphone" count: "Mute all",
/// "Unmute Jane Doe" (roster entries), "Mute notifications" are not it.
func muteLabelState(_ raw: String) -> MuteState? {
    let s = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    for (word, state) in [("unmute", MuteState.muted), ("mute", MuteState.unmuted)] {
        guard s.hasPrefix(word) else { continue }
        let rest = s.dropFirst(word.count).trimmingCharacters(in: .whitespaces)
        if rest.isEmpty || rest.hasPrefix("(") || rest == "mic" || rest == "microphone"
            || rest.hasPrefix("mic (") || rest.hasPrefix("microphone (") {
            return state
        }
        return nil
    }
    return nil
}

/// One state from every mic-button label found: unknown when there is none
/// or when they disagree (a stale second window must not silence the mic).
func combineMuteLabels(_ labels: [String]) -> MuteState {
    let states = Set(labels.compactMap(muteLabelState))
    return states.count == 1 ? states.first! : .unknown
}

/// A reading older than this counts as unknown, so a hung AX call or a
/// stopped poller can never leave the mic stuck silent.
let muteMaxAge = 1.0

/// Whether this mic buffer is written as silence: only a fresh "muted"
/// reading, and never with --mic-always.
func shouldSilenceMic(state: MuteState, readAt: Date?, now: Date, micAlways: Bool) -> Bool {
    guard !micAlways, state == .muted, let at = readAt else { return false }
    let age = now.timeIntervalSince(at)
    return age >= 0 && age <= muteMaxAge
}

/// Zero every sample in place; frameLength (and so the buffer's place on the
/// timeline) is unchanged. Works for any PCM layout.
func zeroSamples(_ buf: AVAudioPCMBuffer) {
    for b in UnsafeMutableAudioBufferListPointer(buf.mutableAudioBufferList) {
        if let p = b.mData { memset(p, 0, Int(b.mDataByteSize)) }
    }
}

/// The gate at the .microphone branch: silences `buf` when it should.
/// Returns whether it did.
@discardableResult
func gateMic(_ buf: AVAudioPCMBuffer, state: MuteState, readAt: Date?, now: Date, micAlways: Bool) -> Bool {
    guard shouldSilenceMic(state: state, readAt: readAt, now: now, micAlways: micAlways) else { return false }
    zeroSamples(buf)
    return true
}

/// The log line for a mute change, in the browser recorder's format
/// (lib/record_browser.py mute_event_line).
func muteEventLine(_ state: MuteState, at: Date, start: Date?) -> String {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSS"
    let off = start.map { String(format: " (+%.1fs)", max(0, at.timeIntervalSince($0))) } ?? ""
    let text: String
    switch state {
    case .muted: text = "muted: left channel silenced"
    case .unmuted: text = "unmuted: recording the mic"
    case .unknown: text = "mute state unknown (no mic button): recording the mic"
    }
    return "mic \(text) at \(f.string(from: at))\(off)"
}

let accessibilityHint = "System Settings > Privacy & Security > Accessibility: turn on your terminal app "
    + "(whosaid record --app) or \"Whosaid Call Recorder\" (autorecord; + to add ~/Applications/Whosaid Call Recorder.app)"

func axString(_ e: AXUIElement, _ name: String) -> String? {
    var v: CFTypeRef?
    guard AXUIElementCopyAttributeValue(e, name as CFString, &v) == .success, let v else { return nil }
    return v as? String
}

/// Every mic-button label in the app's windows (read-only walk, bounded).
/// The element that carried one is returned too, so the next poll can re-read
/// just that element instead of walking the tree again.
func findMuteLabels(pid: pid_t, maxNodes: Int = 8000) -> (labels: [String], elements: [AXUIElement]) {
    let appEl = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(appEl, 0.2)  // our client-side wait, not a change to Teams
    var wins: CFTypeRef?
    guard AXUIElementCopyAttributeValue(appEl, kAXWindowsAttribute as CFString, &wins) == .success,
          let windows = wins as? [AXUIElement] else { return ([], []) }
    var labels: [String] = [], elements: [AXUIElement] = []
    var stack = windows, seen = 0
    while let e = stack.popLast(), seen < maxNodes {
        seen += 1
        if let l = elementMuteLabel(e) {
            labels.append(l)
            elements.append(e)
            continue
        }
        var kids: CFTypeRef?
        if AXUIElementCopyAttributeValue(e, kAXChildrenAttribute as CFString, &kids) == .success,
           let arr = kids as? [AXUIElement] {
            stack.append(contentsOf: arr)
        }
    }
    return (labels, elements)
}

/// The first of AXDescription / AXTitle / AXHelp that reads as a mic button.
func elementMuteLabel(_ e: AXUIElement) -> String? {
    for a in [kAXDescriptionAttribute, kAXTitleAttribute, kAXHelpAttribute] {
        if let s = axString(e, a), muteLabelState(s) != nil { return s }
    }
    return nil
}

/// Polls the Teams mic button every 250 ms on its own queue. The audio queue
/// only reads the cached (state, readAt) under a lock, never AX.
// ---- Teams local API (ws://127.0.0.1:8124), opt-in (#224 option 2) ----------
//
// Teams for desktop has a "Third-party app API" (Settings > Privacy > Manage
// API > Enable API, https://support.microsoft.com/office/aabca9f2-47bb-407f-9f9b-81a104a883d6)
// that pushes meeting state over a local WebSocket. Opt-in: it is only used
// when a token file exists (`whosaid teams pair-api` writes it). While the
// socket is open and Teams has sent a state, that state wins over the AX read.
// The helper never sends a frame: no commands, so it can never change the call.
//
// UNVERIFIED (no Microsoft protocol doc; taken from the open-source client
// github.com/svrooij/teams-monitor and not tested against a live Teams):
// the port 8124, the query parameters (token, protocol-version=2.0.0,
// manufacturer, device, app, app-version), the message shapes
// {"meetingUpdate":{"meetingState":{"isMuted":..,"isInMeeting":..}}} and
// {"tokenRefresh":"<new token>"}, and whether Teams pushes state to a client
// that never sent a command (that client says pairing completes on the first
// command, which this helper never sends). Microsoft retired a "legacy,
// non-published" meeting-control integration on 2026-06-30 (MC1266901); it
// is not confirmed whether that is this API.

func teamsAPITokenURL() -> URL {
    let env = ProcessInfo.processInfo.environment
    if let f = env["WHOSAID_TEAMS_API_TOKEN_FILE"], !f.isEmpty { return URL(fileURLWithPath: f) }
    let base = env["XDG_CONFIG_HOME"].flatMap { $0.isEmpty ? nil : $0 } ?? (NSHomeDirectory() + "/.config")
    return URL(fileURLWithPath: base).appendingPathComponent("whosaid/teams-api-token")
}

func readTeamsAPIToken() -> String? {
    guard let s = try? String(contentsOf: teamsAPITokenURL(), encoding: .utf8) else { return nil }
    let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
    return t.isEmpty ? nil : t
}

/// Atomic, owner-only (0600): the token lets a local process control Teams.
func writeTeamsAPIToken(_ token: String) {
    let url = teamsAPITokenURL()
    try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    let tmp = url.appendingPathExtension("tmp")
    FileManager.default.createFile(atPath: tmp.path, contents: Data((token + "\n").utf8),
                                   attributes: [.posixPermissions: 0o600])
    // rename(2), not replaceItemAt: that keeps the old file's (wider) mode.
    if rename(tmp.path, url.path) != 0 { try? FileManager.default.removeItem(at: tmp) }
    chmod(url.path, 0o600)
}

func teamsAPIURL(token: String) -> URL {
    let port = Int(ProcessInfo.processInfo.environment["WHOSAID_TEAMS_API_PORT"] ?? "") ?? 8124
    var c = URLComponents()
    c.scheme = "ws"
    c.host = "127.0.0.1"
    c.port = port
    c.path = "/"
    c.queryItems = [URLQueryItem(name: "token", value: token), URLQueryItem(name: "protocol-version", value: "2.0.0"),
                    URLQueryItem(name: "manufacturer", value: "whosaid"), URLQueryItem(name: "device", value: "whosaid"),
                    URLQueryItem(name: "app", value: "whosaid"), URLQueryItem(name: "app-version", value: "1.0")]
    return c.url!
}

/// One Teams API message: the mute state it implies (nil = says nothing about
/// mute; unknown = not in a meeting) and a refreshed token, if any.
func parseTeamsAPIMessage(_ text: String) -> (state: MuteState?, token: String?) {
    guard let d = text.data(using: .utf8), let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any]
    else { return (nil, nil) }
    let token = (obj["tokenRefresh"] as? String).flatMap { $0.isEmpty ? nil : $0 }
    var state: MuteState?
    if let mu = obj["meetingUpdate"] as? [String: Any], let ms = mu["meetingState"] as? [String: Any] {
        if let inMeeting = ms["isInMeeting"] as? Bool, !inMeeting {
            state = .unknown
        } else if let m = ms["isMuted"] as? NSNumber, CFGetTypeID(m) == CFBooleanGetTypeID() {
            state = m.boolValue ? .muted : .unmuted
        }
    }
    return (state, token)
}

/// Which reading the gate uses. A live Teams API state (pushed on change, so
/// fresh while the socket is open) wins, with two fail-safe exceptions: API
/// "unknown" (not in a meeting) falls through to AX, and API "muted" never
/// overrides a fresh AX "unmuted" (an unverified source must not silence
/// speech the mic button says is live).
func effectiveMute(api: MuteState?, ax: MuteState, axAt: Date?, now: Date) -> (MuteState, Date?) {
    switch api {
    case .unmuted?: return (.unmuted, now)
    case .muted?:
        if ax == .unmuted, let at = axAt, now.timeIntervalSince(at) <= muteMaxAge { return (.unmuted, at) }
        return (.muted, now)
    default: return (ax, axAt)
    }
}

/// True when the API says muted but a fresh AX read says unmuted (logged once).
func muteSourcesDisagree(api: MuteState?, ax: MuteState, axAt: Date?, now: Date) -> Bool {
    guard api == .muted, ax == .unmuted, let at = axAt else { return false }
    return now.timeIntervalSince(at) <= muteMaxAge
}

/// Listens on the Teams local API and keeps the last pushed mute state.
/// Never sends. Reconnects every 5 s while the token file exists.
final class TeamsAPIWatcher: NSObject, URLSessionWebSocketDelegate {
    private let lock = NSLock()
    private var state: MuteState?
    private var connected = false
    private var task: URLSessionWebSocketTask?
    private var session: URLSession!
    private var stopped = false
    /// Called (off the audio queue) on connect/close and every message.
    var onEvent: ((String) -> Void)?

    override init() {
        super.init()
        session = URLSession(configuration: .ephemeral, delegate: self, delegateQueue: nil)
    }

    /// The pushed state while connected, nil otherwise.
    func current() -> MuteState? {
        lock.lock(); defer { lock.unlock() }
        return connected ? state : nil
    }

    func start() { connect() }

    func stop() {
        lock.lock(); stopped = true; lock.unlock()
        task?.cancel(with: .goingAway, reason: nil)
    }

    private func connect() {
        lock.lock(); let done = stopped; lock.unlock()
        guard !done, let token = readTeamsAPIToken() else { return }
        let t = session.webSocketTask(with: teamsAPIURL(token: token))
        task = t
        t.resume()
        receive(t)
    }

    private func receive(_ t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let msg):
                var text: String?
                switch msg {
                case .string(let s): text = s
                case .data(let d): text = String(data: d, encoding: .utf8)
                @unknown default: text = nil
                }
                if let text { self.handle(text) }
                self.receive(t)
            case .failure:
                self.disconnected(t)
            }
        }
    }

    private func handle(_ text: String) {
        let (st, token) = parseTeamsAPIMessage(text)
        if let token {
            writeTeamsAPIToken(token)
            onEvent?("token refreshed")
        }
        if let st {
            lock.lock(); state = st; lock.unlock()
            onEvent?("state=\(st.rawValue)")
        }
    }

    private func disconnected(_ t: URLSessionWebSocketTask) {
        lock.lock()
        let was = connected && task === t
        if task === t { connected = false; state = nil }
        let done = stopped
        lock.unlock()
        if was { onEvent?("closed") }
        guard !done, task === t else { return }
        DispatchQueue.global().asyncAfter(deadline: .now() + 5) { [weak self] in self?.connect() }
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocol: String?) {
        lock.lock(); if task === webSocketTask { connected = true }; lock.unlock()
        onEvent?("connected")
    }
}

final class MuteWatcher {
    let bundle: String
    /// Read the AX tree (needs the Accessibility grant).
    let useAX: Bool
    /// Teams local API, when a token is stored; its pushed state wins.
    let api: TeamsAPIWatcher?
    let lock = NSLock()
    private var state = MuteState.unknown
    private var readAt: Date?
    private var logged: MuteState?
    private var warnedDisagree = false
    private var cached: [AXUIElement] = []
    private var lastWalk = Date.distantPast
    private let queue = DispatchQueue(label: "whosaid.record-app.mute", qos: .utility)
    private var timer: DispatchSourceTimer?
    var start: Date?

    init(bundle: String, useAX: Bool = true, api: TeamsAPIWatcher? = nil) {
        self.bundle = bundle
        self.useAX = useAX
        self.api = api
    }

    /// What the gate uses. Cheap: two locked reads, no AX, no I/O.
    func snapshot() -> (MuteState, Date?) {
        let apiState = api?.current()
        lock.lock(); defer { lock.unlock() }
        return effectiveMute(api: apiState, ax: state, axAt: readAt, now: Date())
    }

    func begin(start: Date) {
        self.start = start
        if let api {
            api.onEvent = { ev in
                if ev == "connected" || ev == "closed" { log("mute: Teams local API \(ev)") }
            }
            api.start()
        }
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now(), repeating: .milliseconds(250), leeway: .milliseconds(25))
        t.setEventHandler { [weak self] in self?.poll() }
        t.resume()
        timer = t
    }

    func poll() {
        let now = Date()
        let ax = useAX ? read(now: now) : .unknown
        lock.lock()
        state = ax
        readAt = now
        lock.unlock()
        let st = snapshot().0
        if !warnedDisagree, muteSourcesDisagree(api: api?.current(), ax: ax, axAt: now, now: now) {
            warnedDisagree = true
            log("mute: the Teams local API says muted but the mic button says unmuted; recording the mic while they disagree")
        }
        if st != logged {
            logged = st
            log(muteEventLine(st, at: now, start: start))
        }
    }

    private func read(now: Date) -> MuteState {
        // Cheap path: re-read the element(s) that carried the label last time.
        if !cached.isEmpty {
            let labels = cached.compactMap(elementMuteLabel)
            if labels.count == cached.count { return combineMuteLabels(labels) }
            // The button was replaced (a mute edge can do that): walk now,
            // not up to 2 s later; only empty walks are throttled.
            cached = []
            lastWalk = .distantPast
        }
        // Full walk at most every 2 s: a call window with no mic button
        // (lobby, call over) must not cost a tree walk every 250 ms.
        guard now.timeIntervalSince(lastWalk) >= 2 else { return .unknown }
        lastWalk = now
        guard let app = NSWorkspace.shared.runningApplications.first(where: { $0.bundleIdentifier == bundle })
        else { return .unknown }
        let found = findMuteLabels(pid: app.processIdentifier)
        cached = found.elements
        return combineMuteLabels(found.labels)
    }
}

/// Which bundles have a mic button the watcher knows how to read.
func readsMute(_ bundle: String) -> Bool { bundle == "com.microsoft.teams2" }

/// Asks macOS to show the Accessibility prompt at most once per responsible
/// app (the terminal, or Whosaid Call Recorder under autorecord): a marker in
/// ~/.cache/whosaid keyed on __CFBundleIdentifier, which `open` and terminals
/// set and children inherit. WHOSAID_AX_PROMPT=0 turns it off.
func promptAccessibilityOnce() {
    let env = ProcessInfo.processInfo.environment
    if env["WHOSAID_AX_PROMPT"] == "0" { return }
    let who = (env["__CFBundleIdentifier"] ?? "unknown").replacingOccurrences(of: "/", with: "_")
    let dir = URL(fileURLWithPath: env["XDG_CACHE_HOME"] ?? (NSHomeDirectory() + "/.cache")).appendingPathComponent("whosaid")
    let marker = dir.appendingPathComponent("ax-prompted-\(who)")
    if FileManager.default.fileExists(atPath: marker.path) { return }
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    FileManager.default.createFile(atPath: marker.path, contents: Data())
    let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    _ = AXIsProcessTrustedWithOptions(opts)
}

/// `--selftest-mute`: the label matcher, the gate decision and the zero-fill
/// through a real Leg. No AX, no capture, no prompt. Exit 0 = all pass.
func runMuteSelftest() -> Int32 {
    var failed = 0, passed = 0
    func check(_ ok: Bool, _ what: String) {
        if ok { passed += 1 } else { failed += 1; print("FAIL: \(what)") }
    }
    let labels: [(String, MuteState?)] = [
        ("Mute (⌘+Shift+M)", .unmuted), ("Unmute (⌘+Shift+M)", .muted), ("Unmute", .muted), ("Mute", .unmuted),
        ("  unmute  ", .muted), ("Mute mic", .unmuted), ("Unmute microphone (Ctrl+Shift+M)", .muted),
        ("Mute all", nil), ("Unmute Jane Doe", nil), ("Mute notifications", nil), ("Muted", nil),
        ("Stummschalten (⌘+Umschalt+M)", nil), ("Désactiver le micro", nil), ("", nil), ("Camera", nil),
    ]
    for (l, want) in labels { check(muteLabelState(l) == want, "label \(l.debugDescription) -> \(String(describing: want))") }
    check(combineMuteLabels(["Unmute (⌘+Shift+M)"]) == .muted, "one Unmute label = muted")
    check(combineMuteLabels(["Mute (⌘+Shift+M)", "Unmute Jane Doe"]) == .unmuted, "roster entries are ignored")
    check(combineMuteLabels([]) == .unknown, "no button = unknown")
    check(combineMuteLabels(["Stummschalten"]) == .unknown, "localized label = unknown")
    check(combineMuteLabels(["Mute", "Unmute"]) == .unknown, "conflicting buttons = unknown")
    let now = Date()
    check(shouldSilenceMic(state: .muted, readAt: now, now: now, micAlways: false), "fresh muted silences")
    check(!shouldSilenceMic(state: .muted, readAt: now, now: now, micAlways: true), "--mic-always never silences")
    check(!shouldSilenceMic(state: .unknown, readAt: now, now: now, micAlways: false), "unknown records")
    check(!shouldSilenceMic(state: .unmuted, readAt: now, now: now, micAlways: false), "unmuted records")
    check(!shouldSilenceMic(state: .muted, readAt: nil, now: now, micAlways: false), "never read records")
    check(!shouldSilenceMic(state: .muted, readAt: now.addingTimeInterval(-1.5), now: now, micAlways: false),
          "stale muted (1.5 s) records")
    check(muteEventLine(.muted, at: now, start: now.addingTimeInterval(-12.34)).hasPrefix("mic muted: left channel silenced at ")
          && muteEventLine(.muted, at: now, start: now.addingTimeInterval(-12.34)).hasSuffix(" (+12.3s)"), "muted log line format")
    check(muteEventLine(.unmuted, at: now, start: nil).hasPrefix("mic unmuted: recording the mic at "), "unmuted log line")
    check(muteEventLine(.unknown, at: now, start: nil).contains("mute state unknown"), "unknown log line")

    // Teams local API messages and precedence (option 2).
    check(parseTeamsAPIMessage(#"{"meetingUpdate":{"meetingState":{"isMuted":true,"isInMeeting":true}}}"#).state == .muted, "api isMuted true = muted")
    check(parseTeamsAPIMessage(#"{"meetingUpdate":{"meetingState":{"isMuted":false}}}"#).state == .unmuted, "api isMuted false = unmuted")
    check(parseTeamsAPIMessage(#"{"meetingUpdate":{"meetingState":{"isMuted":true,"isInMeeting":false}}}"#).state == .unknown, "api not in a meeting = unknown")
    check(parseTeamsAPIMessage(#"{"meetingUpdate":{"meetingState":{"isMuted":1}}}"#).state == nil, "api non-bool isMuted is ignored")
    check(parseTeamsAPIMessage(#"{"tokenRefresh":"abc"}"#).token == "abc", "api tokenRefresh")
    check(parseTeamsAPIMessage("not json").state == nil && parseTeamsAPIMessage("[]").token == nil, "api junk is ignored")
    check(effectiveMute(api: .unmuted, ax: .muted, axAt: now, now: now).0 == .unmuted, "api state wins over AX")
    check(effectiveMute(api: nil, ax: .muted, axAt: now, now: now).0 == .muted, "no api state: AX")
    check(effectiveMute(api: .muted, ax: .unmuted, axAt: now, now: now).0 == .unmuted, "api muted vs fresh AX unmuted: records")
    check(muteSourcesDisagree(api: .muted, ax: .unmuted, axAt: now, now: now), "api muted vs fresh AX unmuted is a disagreement")
    check(effectiveMute(api: .muted, ax: .unmuted, axAt: now.addingTimeInterval(-5), now: now).0 == .muted, "api muted vs stale AX unmuted: muted")
    check(effectiveMute(api: .unknown, ax: .muted, axAt: now, now: now).0 == .muted, "api unknown falls through to AX muted")
    check(effectiveMute(api: .unknown, ax: .unmuted, axAt: now, now: now).0 == .unmuted, "api unknown falls through to AX unmuted")
    let stale = effectiveMute(api: nil, ax: .muted, axAt: now.addingTimeInterval(-5), now: now)
    check(!shouldSilenceMic(state: stale.0, readAt: stale.1, now: now, micAlways: false), "no api, stale AX: records")
    let pushed = effectiveMute(api: .muted, ax: .unknown, axAt: nil, now: now)
    check(shouldSilenceMic(state: pushed.0, readAt: pushed.1, now: now, micAlways: false), "api muted silences without AX")

    // Gate through a real Leg: 0.1 s tone, 0.1 s while muted, 0.1 s tone, at
    // consecutive offsets. The muted block must be zeros of the same length at
    // the same place; with --mic-always it must stay the tone.
    for micAlways in [false, true] {
        let url = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("whosaid-mute-selftest-\(getpid())-\(micAlways).wav")
        defer { try? FileManager.default.removeItem(at: url) }
        guard let leg = try? Leg(name: "mic", url: url) else { check(false, "open test leg"); continue }
        let n = 1600
        func tone() -> AVAudioPCMBuffer {
            let b = AVAudioPCMBuffer(pcmFormat: leg.target, frameCapacity: AVAudioFrameCount(n))!
            b.frameLength = AVAudioFrameCount(n)
            for i in 0..<n { b.floatChannelData![0][i] = 0.5 * Float(sin(Double(i) * 0.3)) + 0.25 }
            return b
        }
        let states: [MuteState] = [.unmuted, .muted, .unknown]
        var silenced: [Bool] = []
        for (k, st) in states.enumerated() {
            let b = tone()
            silenced.append(gateMic(b, state: st, readAt: now, now: now, micAlways: micAlways))
            check(Int(b.frameLength) == n, "gate keeps frameLength (mic-always \(micAlways), \(st))")
            leg.append(b, at: Int64(k * n))
        }
        check(silenced == [false, !micAlways, false], "gate decisions \(silenced) (mic-always \(micAlways))")
        check(leg.written == Int64(3 * n) && leg.filled == 0 && leg.dropped == 0,
              "frame count preserved: written \(leg.written), filled \(leg.filled), dropped \(leg.dropped)")
        leg.file.close()
        guard let rf = try? AVAudioFile(forReading: url),
              let rb = AVAudioPCMBuffer(pcmFormat: rf.processingFormat, frameCapacity: AVAudioFrameCount(rf.length)),
              (try? rf.read(into: rb)) != nil, let ch = rb.floatChannelData?[0] else {
            check(false, "read back test leg"); continue
        }
        check(Int(rb.frameLength) == 3 * n, "file holds \(rb.frameLength) frames, want \(3 * n)")
        let block = { (k: Int) -> Float in (k * n..<(k + 1) * n).map { abs(ch[$0]) }.max() ?? 0 }
        check(block(0) > 0.1 && block(2) > 0.1, "unmuted and unknown blocks keep the tone")
        if micAlways {
            check(block(1) > 0.1, "--mic-always keeps the muted block")
        } else {
            check(block(1) == 0, "muted block is all zeros (peak \(block(1))) at offset \(n)")
        }
    }
    // The capture path itself: Recorder.deliver gates the mic leg only,
    // counts the silenced frames, and finish's marker reports them.
    do {
        let dir = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("whosaid-mute-rec-\(getpid())")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        if let appLeg = try? Leg(name: "app", url: dir.appendingPathComponent("app.wav")),
           let micLeg = try? Leg(name: "mic", url: dir.appendingPathComponent("mic.wav")) {
            let rec = Recorder(app: appLeg, mic: micLeg)
            rec.outDir = dir
            let n = 1600
            func tone() -> AVAudioPCMBuffer {
                let b = AVAudioPCMBuffer(pcmFormat: micLeg.target, frameCapacity: AVAudioFrameCount(n))!
                b.frameLength = AVAudioFrameCount(n)
                for i in 0..<n { b.floatChannelData![0][i] = 0.5 }
                return b
            }
            var muted = MuteState.muted
            rec.queue.sync { rec.muteSource = { (muted, Date()) } }
            let mb = tone(), ab = tone()
            rec.queue.sync {
                rec.deliver(mb, to: micLeg, at: 0)
                rec.deliver(ab, to: appLeg, at: 0)
            }
            check(mb.floatChannelData![0][n / 2] == 0, "deliver zeroes the mic buffer while muted")
            check(ab.floatChannelData![0][n / 2] == 0.5, "deliver leaves the app buffer alone")
            muted = .unmuted
            let mb2 = tone()
            rec.queue.sync { rec.deliver(mb2, to: micLeg, at: Int64(n)) }
            check(mb2.floatChannelData![0][n / 2] == 0.5, "deliver keeps the mic once unmuted")
            check(rec.micSilenced == Int64(n), "silenced frames counted: \(rec.micSilenced)")
            // A device-rate (48 kHz) buffer counts as its 16 kHz length.
            muted = .muted
            let hi = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 1, interleaved: false)!
            let hb = AVAudioPCMBuffer(pcmFormat: hi, frameCapacity: 4800)!
            hb.frameLength = 4800
            for i in 0..<4800 { hb.floatChannelData![0][i] = 0.5 }
            rec.queue.sync { rec.deliver(hb, to: micLeg, at: Int64(2 * n)) }
            check(rec.micSilenced == Int64(2 * n), "48 kHz buffer counted in 16 kHz frames: \(rec.micSilenced)")
            check(rec.micSilenced <= micLeg.written, "silenced \(rec.micSilenced) <= written \(micLeg.written)")
            muted = .unmuted
            rec.queue.sync { rec.writeMutedMarker() }
            let marker = (try? String(contentsOf: dir.appendingPathComponent("mic.muted"), encoding: .utf8)) ?? ""
            check(marker == "silenced_frames=\(rec.micSilenced)\ntotal_frames=\(micLeg.written)\n" && marker.hasPrefix("silenced_frames=\(2 * n)\n"),
                  "mic.muted marker \(marker.debugDescription)")
            appLeg.file.close(); micLeg.file.close()
        } else {
            check(false, "open recorder test legs")
        }
    }
    print("mute selftest: \(passed) passed, \(failed) failed")
    return failed == 0 ? 0 : 1
}

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let app: Leg
    let mic: Leg?
    /// Reads Teams' mic button; nil with --mic-always, --no-mic, another app,
    /// or no Accessibility grant (the mic is then always recorded).
    var muteWatcher: MuteWatcher? {
        didSet { muteSource = muteWatcher.map { w in { w.snapshot() } } }
    }
    /// What `deliver` asks for the mute state; set through muteWatcher, or
    /// directly by the selftest. Touch only on `queue`.
    var muteSource: (() -> (MuteState, Date?))?
    var micAlways = false
    /// Mic frames written as zeros because the meeting was muted.
    var micSilenced: Int64 = 0
    /// Where finish() leaves `mic.muted` (silenced and total mic frames) so
    /// the wrapper can tell a muted call from an unauthorized mic.
    var outDir: URL?
    /// All sample callbacks land here, serially, so buffers stay in capture
    /// order (a concurrent queue reorders ~10 ms chunks under load).
    let queue = DispatchQueue(label: "whosaid.record-app.samples", qos: .userInitiated)
    var t0: Double?
    var stream: SCStream?
    var finished = false
    var target: SCRunningApplication?
    /// Bounded restarts after macOS kills the stream mid-capture (display
    /// change, sleep): a meeting would otherwise lose the rest of its other
    /// side. The legs stay open, and because buffers are placed by timestamp
    /// the gap fills itself with silence once the new stream delivers.
    var restartAttempts = 0
    var restarts = 0
    let maxRestartAttempts = 5

    init(app: Leg, mic: Leg?) {
        self.app = app
        self.mic = mic
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        let leg: Leg
        switch type {
        case .audio: leg = app
        case .microphone:
            guard let mic else { return }
            leg = mic
        default: return  // the placeholder video frames
        }
        guard !finished, let buf = pcmBuffer(sb) else { return }
        let pts = CMSampleBufferGetPresentationTimeStamp(sb).seconds
        if t0 == nil { t0 = pts }
        let offset = Int64(((pts - t0!) * sampleRate).rounded())
        deliver(buf, to: leg, at: max(0, offset))
    }

    func writeMutedMarker() {
        guard let mic, micSilenced > 0, let dir = outDir else { return }
        try? "silenced_frames=\(micSilenced)\ntotal_frames=\(mic.written)\n"
            .write(to: dir.appendingPathComponent("mic.muted"), atomically: true, encoding: .utf8)
    }

    /// Gate (mic only), then append. Runs on `queue`.
    func deliver(_ buf: AVAudioPCMBuffer, to leg: Leg, at offset: Int64) {
        if leg === mic, let src = muteSource {
            // Zeros, not a drop: the buffer keeps its frames and its offset.
            // "now" is taken after the read: an API state is stamped at read
            // time, and an earlier "now" would make it look future-dated.
            let (st, at) = src()
            if gateMic(buf, state: st, readAt: at, now: Date(), micAlways: micAlways) {
                // Count in the leg's 16 kHz frames: SCK delivers the mic at
                // the device rate (often 48 kHz), and Leg converts it.
                micSilenced += Int64((Double(buf.frameLength) / buf.format.sampleRate * sampleRate).rounded())
            }
        }
        leg.append(buf, at: offset)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("capture stopped by macOS: \(error.localizedDescription)")
        // A revoked grant fails the same way every time: close the files (the
        // audio so far is valid and the caller keeps it) instead of retrying.
        if isPermissionError(error) {
            permissionHelp()
            finish(code: 5)
            return
        }
        guard stream === self.stream else { return }  // a replaced stream's late callback
        Task { await self.restart() }
    }

    func restart() async {
        while true {
            if queue.sync(execute: { finished }) { return }
            guard restartAttempts < maxRestartAttempts else {
                log("giving up after \(maxRestartAttempts) restart attempts; keeping the audio recorded so far.")
                finish(code: 3)
                return
            }
            restartAttempts += 1
            let attempt = restartAttempts
            try? await Task.sleep(nanoseconds: UInt64(0.5 * Double(attempt) * 1_000_000_000))
            if queue.sync(execute: { finished }) { return }
            do {
                let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
                guard let id = target?.bundleIdentifier,
                      let app = content.applications.first(where: { $0.bundleIdentifier == id }),
                      let display = content.displays.first else {
                    throw NSError(domain: "record-app", code: 1,
                                  userInfo: [NSLocalizedDescriptionKey: "the app or a display is not available"])
                }
                try await start(target: app, display: display)
                restarts += 1
                log("capture restarted (attempt \(attempt)); the gap is filled with silence.")
                return
            } catch {
                if isPermissionError(error) {
                    permissionHelp()
                    finish(code: 5)
                    return
                }
                log("restart attempt \(attempt) failed: \(error.localizedDescription)")
            }
        }
    }

    /// Stop capture, pad both legs to the same length, close the files.
    /// Runs once; later calls (a second Ctrl-C, the --max-seconds check, a
    /// late stream callback) return without logging or exiting again.
    func finish(code: Int32) {
        let first = queue.sync { () -> Bool in
            if finished { return false }
            finished = true
            if let mic {
                let n = max(app.written, mic.written)
                app.writeZeros(n - app.written)
                mic.writeZeros(n - mic.written)
            }
            // close() writes the final WAV header; exit() would skip deinit.
            app.file.close()
            mic?.file.close()
            writeMutedMarker()
            return true
        }
        guard first else { return }
        let s = stream
        // stopCapture on a stream macOS already killed can wait forever; the
        // files are closed, so cap the wait instead of leaving an orphan, and
        // keep the exit code: the recording itself is intact.
        _ = watchdog(3, "stopping the capture", code: code)
        Task {
            try? await s?.stopCapture()
            let secs = Double(self.app.written) / sampleRate
            log(String(format: "stopped after %.1fs (app buffers: %d%@, restarts: %d)", secs, self.app.buffers,
                       self.mic.map { ", mic buffers: \($0.buffers)" } ?? "", self.restarts)
                + (self.micSilenced > 0 ? String(format: "; mic silenced while muted: %.1fs", Double(self.micSilenced) / sampleRate) : ""))
            terminate(code)
        }
    }

    func start(target: SCRunningApplication, display: SCDisplay) async throws {
        let filter = SCContentFilter(display: display, including: [target], exceptingWindows: [])
        let cfg = SCStreamConfiguration()
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        cfg.queueDepth = 5
        cfg.showsCursor = false
        cfg.capturesAudio = true
        cfg.sampleRate = Int(sampleRate)
        cfg.channelCount = 1
        cfg.excludesCurrentProcessAudio = true
        if mic != nil { cfg.captureMicrophone = true }
        let s = SCStream(filter: filter, configuration: cfg, delegate: self)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try s.addStreamOutput(self, type: .screen, sampleHandlerQueue: .global(qos: .utility))
        if mic != nil { try s.addStreamOutput(self, type: .microphone, sampleHandlerQueue: queue) }
        try await s.startCapture()
        stream = s
        self.target = target
    }
}

// ---- main ---------------------------------------------------------------------

var args = Array(CommandLine.arguments.dropFirst())
var list = false
var appQuery: String?
var outDir: String?
var withMic = true
var maxSeconds = 10_800.0
var callStateOnly = false
var waitForCall = false
var untilCallEnds = false
var idleSeconds = 30.0
var watchCall = false
var watchInterval = 1.0
var micAlways = false
var muteStateOnly = false
var selftestMute = false
var teamsAPIListen: Double?

func usage() -> Never {
    log("usage: record-app --list | --call-state --app <bundle-id|name> | --app <bundle-id|name> --out-dir DIR"
        + " [--no-mic] [--max-seconds N] [--wait-for-call] [--until-call-ends] [--idle-seconds N]"
        + " [--mic-always] | --watch-call --app <bundle-id|name> [--interval S]"
        + " | --mute-state --app <bundle-id> | --selftest-mute | --teams-api-listen S")
    terminate(2)
}

func nextValue() -> String {
    if args.isEmpty { usage() }
    return args.removeFirst()
}

while !args.isEmpty {
    let a = args.removeFirst()
    switch a {
    case "--list": list = true
    case "--app": appQuery = nextValue()
    case "--out-dir": outDir = nextValue()
    case "--no-mic": withMic = false
    case "--mic-always": micAlways = true
    case "--mute-state": muteStateOnly = true
    case "--selftest-mute": selftestMute = true
    case "--teams-api-listen":
        guard !args.isEmpty, let v = Double(args.removeFirst()), v > 0, v.isFinite else { usage() }
        teamsAPIListen = v
    case "--max-seconds":
        guard !args.isEmpty, let v = Double(args.removeFirst()), v > 0 else { usage() }
        maxSeconds = v
    case "--call-state": callStateOnly = true
    case "--watch-call": watchCall = true
    case "--interval":
        guard !args.isEmpty, let v = Double(args.removeFirst()), v > 0, v.isFinite else { usage() }
        watchInterval = v
    case "--wait-for-call": waitForCall = true
    case "--until-call-ends": untilCallEnds = true
    case "--idle-seconds":
        guard !args.isEmpty, let v = Double(args.removeFirst()), v > 0 else { usage() }
        idleSeconds = v
    default: usage()
    }
}

if selftestMute { terminate(runMuteSelftest()) }

// `--teams-api-listen S`: connect to the Teams local API with the stored
// token, print each event (connected, state=..., token refreshed, closed) for
// S seconds, then exit (0 if it connected, 4 if not). Never sends a frame.
if let secs = teamsAPIListen {
    guard readTeamsAPIToken() != nil else {
        log("no Teams API token at \(teamsAPITokenURL().path); run: whosaid teams pair-api")
        terminate(2)
    }
    let w = TeamsAPIWatcher()
    let seen = NSLock()
    var didConnect = false, gotState = false
    w.onEvent = { ev in
        seen.lock()
        if ev == "connected" { didConnect = true }
        if ev.hasPrefix("state=") { gotState = true }
        seen.unlock()
        print(ev); fflush(stdout)
    }
    w.start()
    // 0 = Teams pushed a mute state; 3 = the socket opened but no state came
    // (not in a meeting, or Teams wants pairing first); 4 = no connection.
    DispatchQueue.global().asyncAfter(deadline: .now() + secs) {
        seen.lock(); let c = didConnect, g = gotState; seen.unlock()
        terminate(g ? 0 : c ? 3 : 4)
    }
    dispatchMain()
}

// Read-only: prints whether this process may read AX, and the mic-button
// labels and state the gate would use for a running app. Never prompts.
if muteStateOnly {
    guard let q = appQuery else { usage() }
    let trusted = AXIsProcessTrusted()
    guard let a = NSWorkspace.shared.runningApplications.first(where: {
        $0.bundleIdentifier == q || ($0.localizedName ?? "").lowercased() == q.lowercased()
    }) else {
        print("trusted=\(trusted)\tapp=\(q)\trunning=false\tstate=unknown")
        terminate(0)
    }
    let found: (labels: [String], elements: [AXUIElement]) = trusted ? findMuteLabels(pid: a.processIdentifier) : ([], [])
    let state = trusted ? combineMuteLabels(found.labels) : .unknown
    print("trusted=\(trusted)\tapp=\(a.bundleIdentifier ?? q)\trunning=true\tstate=\(state.rawValue)\tlabels="
          + found.labels.map { $0.debugDescription }.joined(separator: ","))
    if !trusted { log("no Accessibility grant: " + accessibilityHint) }
    terminate(0)
}

// Ignore default SIGINT/SIGTERM handling; these dispatch sources take over.
// Before capture starts there is nothing to save, so a signal exits at once.
// During capture the first one finishes cleanly; a second one exits hard, so
// Ctrl-C twice (or timeout/pkill) always ends the process. A second signal
// within 1 s of the first is the same stop arriving twice (a parent that
// signals both the process group and the whosaid script, which forwards its
// SIGTERM here), so it is ignored rather than turned into a hard exit.
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
var signalSources: [DispatchSourceSignal] = []
var recorder: Recorder?
var signalledAt: Date?
for sig in [SIGINT, SIGTERM] {
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler {
        guard let rec = recorder else { terminate(watchCall ? 0 : 130) }
        if let first = signalledAt {
            if Date().timeIntervalSince(first) < 1 { return }
            terminate(130)
        }
        signalledAt = Date()
        rec.finish(code: 0)
    }
    src.resume()
    signalSources.append(src)
}

/// Whether any process of `bundle` (or a `bundle.*` helper) is running.
/// NSWorkspace, not Core Audio: Core Audio lists only processes that have
/// touched audio, so an idle app would look absent. An open stream also
/// proves the app is running.
func isRunning(_ bundle: String, input: Bool, output: Bool) -> Bool {
    if input || output { return true }
    return NSWorkspace.shared.runningApplications.contains {
        guard let b = $0.bundleIdentifier else { return false }
        return b == bundle || b.hasPrefix(bundle + ".")
    }
}

/// Streams one JSON line per mic/speaker change (plus the first state) until
/// a signal ends the process with exit 0. Never captures audio.
func watchCallState(_ bundle: String) async -> Never {
    let stamp = ISO8601DateFormatter()
    stamp.formatOptions = [.withInternetDateTime]
    var last: (Bool, Bool)?
    while true {
        let st = callState(bundle)
        if last == nil || last! != (st.input, st.output) {
            last = (st.input, st.output)
            let running = isRunning(bundle, input: st.input, output: st.output)
            let data = try? JSONSerialization.data(withJSONObject: bundle, options: [.fragmentsAllowed])
            let name = data.flatMap { String(data: $0, encoding: .utf8) } ?? "\"\""
            let line = "{\"app\":\(name),\"running\":\(running),\"mic\":\(st.input),"
                + "\"speaker\":\(st.output),\"at\":\"\(stamp.string(from: Date()))\"}\n"
            FileHandle.standardOutput.write(line.data(using: .utf8)!)
        }
        try? await Task.sleep(nanoseconds: UInt64(watchInterval * 1_000_000_000))
    }
}

Task {
    if watchCall {
        guard let query = appQuery, !list, !callStateOnly, outDir == nil else { usage() }
        // A bundle ID is watched directly: no ScreenCaptureKit, no Screen
        // Recording grant, and the app need not be running yet.
        if query.contains(".") { await watchCallState(query) }
    }
    let limit = Double(ProcessInfo.processInfo.environment["WHOSAID_RECORD_APP_STARTUP_TIMEOUT"] ?? "") ?? 20
    let startup = watchdog(limit, list ? "listing apps" : "starting the capture")
    let content = await shareableContent()
    let apps = uniqueApps(content)
    if list {
        for a in apps { print("\(a.bundleIdentifier)\t\(a.applicationName)") }
        terminate(0)
    }
    guard let query = appQuery, callStateOnly || outDir != nil else { usage() }
    guard var target = resolveApp(query, in: apps) else {
        log("no running app matches '\(query)'. Running apps: whosaid record --list-apps")
        terminate(2)
    }
    let bundle = target.bundleIdentifier
    if watchCall {
        startup.cancel()
        await watchCallState(bundle)
    }
    if callStateOnly {
        let st = callState(bundle)
        print("\(bundle)\tmic=\(st.input ? 1 : 0)\tspeaker=\(st.output ? 1 : 0)")
        terminate(0)
    }
    let dir = outDir!
    var displays = content.displays
    var startupAfterWait = startup
    if waitForCall {
        startup.cancel()
        log("waiting for \(target.applicationName) to open the mic (a call to start); Ctrl-C to give up.")
        while !callState(bundle).input {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
        }
        log("\(target.applicationName) opened the mic; starting the capture.")
        startupAfterWait = watchdog(limit, "starting the capture")
        // The app may have restarted (an update) during a long wait; a stale
        // SCRunningApplication would fail the capture, so look it up again (#122).
        var fresh: SCShareableContent? = nil
        for attempt in 1...5 {
            do {
                fresh = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
                break
            } catch {
                guard isPermissionError(error), attempt < 5 else {
                    if isPermissionError(error) { permissionExit() }
                    log("ScreenCaptureKit failed: \(error.localizedDescription)")
                    terminate(1)
                }
                let ns = error as NSError
                log("app list refused (\(ns.domain) \(ns.code): \(ns.localizedDescription)); retry \(attempt)/4 in 2s")
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
        if let again = fresh!.applications.first(where: { $0.bundleIdentifier == bundle }) {
            target = again
        }
        displays = fresh!.displays
    }
    guard let display = displays.first else {
        log("no display available to attach the capture stream to.")
        terminate(1)
    }
    let base = URL(fileURLWithPath: dir)
    let rec: Recorder
    do {
        rec = Recorder(app: try Leg(name: "app", url: base.appendingPathComponent("app.wav")),
                       mic: withMic ? try Leg(name: "mic", url: base.appendingPathComponent("mic.wav")) : nil)
    } catch {
        log("cannot open output in \(dir): \(error.localizedDescription)")
        terminate(1)
    }
    rec.outDir = base
    recorder = rec
    // Right after a call starts, macOS has refused the capture once with a
    // permission error while the grant was on (#129); a retry seconds later
    // worked. After a wait (the grant was fine at startup), retry a few times.
    var attempt = 1
    while true {
        do {
            try await rec.start(target: target, display: display)
            break
        } catch {
            if isPermissionError(error) && waitForCall && attempt < 5 {
                let ns = error as NSError
                log("capture refused (\(ns.domain) \(ns.code): \(ns.localizedDescription)); retry \(attempt)/4 in 2s")
                attempt += 1
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                if let fresh = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false),
                   let again = fresh.applications.first(where: { $0.bundleIdentifier == bundle }) {
                    target = again
                }
                continue
            }
            if isPermissionError(error) { permissionExit() }
            log("could not start capture: \(error.localizedDescription)")
            terminate(1)
        }
    }
    startupAfterWait.cancel()
    log("capturing \(target.applicationName) (\(target.bundleIdentifier))\(withMic ? " + mic" : "") — Ctrl-C to stop.")
    if withMic {
        rec.queue.sync { rec.micAlways = micAlways }
        if micAlways {
            log("mic always on (--mic-always): meeting mute is ignored")
        } else if !readsMute(target.bundleIdentifier) {
            log("mute: unknown (only the Teams mic button is read; \(target.applicationName)'s mute is not); recording the mic")
        } else {
            let ax = AXIsProcessTrusted()
            let api = readTeamsAPIToken() != nil ? TeamsAPIWatcher() : nil
            if !ax {
                promptAccessibilityOnce()
                log("mute: unknown (no Accessibility grant; \(accessibilityHint)); "
                    + (api != nil ? "the Teams local API is used when it connects, else the mic is recorded"
                                  : "recording the mic"))
            }
            if ax || api != nil {
                log("mic follows meeting mute: silenced while muted"
                    + (api != nil ? " (Teams local API" + (ax ? ", then Accessibility)" : ")") : " (Accessibility)"))
                let w = MuteWatcher(bundle: target.bundleIdentifier, useAX: ax, api: api)
                rec.queue.sync { rec.muteWatcher = w }
                w.begin(start: Date())
            }
        }
    }

    if untilCallEnds {
        log("auto-stops once \(target.applicationName) has not used the mic for \(Int(idleSeconds))s (the call ended).")
    }

    // Progress: one line every 10 s so a dead leg is visible while it runs.
    // --until-call-ends polls the call state every 2 s in between.
    let started = Date()
    var lastInCall = Date()
    var lastProgress = Date()
    while true {
        // Wake at --max-seconds too, not only on the tick.
        let left = maxSeconds - Date().timeIntervalSince(started)
        let tick = untilCallEnds ? 2.0 : 10.0
        try? await Task.sleep(nanoseconds: UInt64(max(0.05, min(tick, left)) * 1_000_000_000))
        let elapsed = Date().timeIntervalSince(started)
        if untilCallEnds {
            if callState(bundle).input {
                lastInCall = Date()
            } else if Date().timeIntervalSince(lastInCall) >= idleSeconds {
                log("\(target.applicationName) has not used the mic for \(Int(idleSeconds))s; the call ended, stopping.")
                rec.finish(code: 0)
                return
            }
        }
        if elapsed < maxSeconds && Date().timeIntervalSince(lastProgress) < 9.5 { continue }
        lastProgress = Date()
        var line = String(format: "%5.0fs  app peak %@ (%@)", elapsed, rec.queue.sync { rec.app.takePeakDB() },
                          rec.queue.sync { rec.app.driftSummary() })
        if let mic = rec.mic {
            line += "  mic peak " + rec.queue.sync { mic.takePeakDB() } + " (" + rec.queue.sync { mic.driftSummary() } + ")"
        }
        log(line)
        // Test hook: simulate macOS killing the stream, to exercise restart.
        if let kill = ProcessInfo.processInfo.environment["WHOSAID_RECORD_APP_TEST_KILL_AFTER"],
           let at = Double(kill), elapsed >= at, rec.restarts == 0, rec.restartAttempts == 0, let s = rec.stream {
            log("test hook: stopping the stream to simulate a mid-capture death")
            try? await s.stopCapture()
            rec.stream(s, didStopWithError: NSError(domain: "record-app.test", code: -1,
                                                    userInfo: [NSLocalizedDescriptionKey: "simulated stream death"]))
        }
        if elapsed >= maxSeconds {
            log("reached --max-seconds \(Int(maxSeconds)); stopping.")
            rec.finish(code: 0)
            return  // finish() exits the process; don't spin on 0.05 s ticks until it does
        }
    }
}

dispatchMain()
