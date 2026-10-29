// record_app.swift: capture one app's audio (and optionally the mic) via ScreenCaptureKit.
//
// Built on first use by `whosaid record --app` (see ensure_record_app_helper in
// the whosaid script) and cached under ~/.cache/whosaid, keyed on this file's hash.
//
//   record-app --list
//   record-app --app <bundle-id|name> --out-dir DIR [--no-mic] [--max-seconds N]
//              [--wait-for-call] [--until-call-ends] [--idle-seconds N]
//   record-app --call-state --app <bundle-id|name>
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

final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let app: Leg
    let mic: Leg?
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
        leg.append(buf, at: max(0, offset))
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
                       self.mic.map { ", mic buffers: \($0.buffers)" } ?? "", self.restarts))
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

func usage() -> Never {
    log("usage: record-app --list | --call-state --app <bundle-id|name> | --app <bundle-id|name> --out-dir DIR"
        + " [--no-mic] [--max-seconds N] [--wait-for-call] [--until-call-ends] [--idle-seconds N]")
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
    case "--max-seconds":
        guard !args.isEmpty, let v = Double(args.removeFirst()), v > 0 else { usage() }
        maxSeconds = v
    case "--call-state": callStateOnly = true
    case "--wait-for-call": waitForCall = true
    case "--until-call-ends": untilCallEnds = true
    case "--idle-seconds":
        guard !args.isEmpty, let v = Double(args.removeFirst()), v > 0 else { usage() }
        idleSeconds = v
    default: usage()
    }
}

// Ignore default SIGINT/SIGTERM handling; these dispatch sources take over.
// Before capture starts there is nothing to save, so a signal exits at once.
// During capture the first one finishes cleanly; a second one exits hard, so
// Ctrl-C twice (or timeout/pkill) always ends the process.
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
var signalSources: [DispatchSourceSignal] = []
var recorder: Recorder?
var signalled = false
for sig in [SIGINT, SIGTERM] {
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler {
        guard let rec = recorder, !signalled else { terminate(130) }
        signalled = true
        rec.finish(code: 0)
    }
    src.resume()
    signalSources.append(src)
}

Task {
    let limit = Double(ProcessInfo.processInfo.environment["WHOSAID_RECORD_APP_STARTUP_TIMEOUT"] ?? "") ?? 20
    let startup = watchdog(limit, list ? "listing apps" : "starting the capture")
    let content = await shareableContent()
    let apps = uniqueApps(content)
    if list {
        for a in apps { print("\(a.bundleIdentifier)\t\(a.applicationName)") }
        terminate(0)
    }
    guard let query = appQuery, callStateOnly || outDir != nil else { usage() }
    guard let target = resolveApp(query, in: apps) else {
        log("no running app matches '\(query)'. Running apps: whosaid record --list-apps")
        terminate(2)
    }
    let bundle = target.bundleIdentifier
    if callStateOnly {
        let st = callState(bundle)
        print("\(bundle)\tmic=\(st.input ? 1 : 0)\tspeaker=\(st.output ? 1 : 0)")
        terminate(0)
    }
    let dir = outDir!
    if waitForCall {
        startup.cancel()
        log("waiting for \(target.applicationName) to open the mic (a call to start); Ctrl-C to give up.")
        while !callState(bundle).input {
            try? await Task.sleep(nanoseconds: 2_000_000_000)
        }
        log("\(target.applicationName) opened the mic; starting the capture.")
    }
    let startupAfterWait = waitForCall ? watchdog(limit, "starting the capture") : startup
    guard let display = content.displays.first else {
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
    recorder = rec
    do {
        try await rec.start(target: target, display: display)
    } catch {
        if isPermissionError(error) { permissionExit() }
        log("could not start capture: \(error.localizedDescription)")
        terminate(1)
    }
    startupAfterWait.cancel()
    log("capturing \(target.applicationName) (\(target.bundleIdentifier))\(withMic ? " + mic" : "") — Ctrl-C to stop.")

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
