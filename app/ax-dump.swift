// ax-dump: print a running app's accessibility tree (what VoiceOver sees), so a
// check can read the UI as text instead of squinting at a screenshot.
// Usage: ax-dump <pid> [--text] [--max-nodes N] [--max-depth N] [--timeout S]
//   default: JSON {role, subrole, title, value, desc, id, frame, children}
//   --text:  one indented line per element that has a role or some text
// Needs the Accessibility grant for the app this runs from (your terminal):
// System Settings > Privacy & Security > Accessibility. Exits 2 without it.
import ApplicationServices
import Foundation

var args = Array(CommandLine.arguments.dropFirst())
func opt(_ name: String, _ dflt: Double) -> Double {
  guard let i = args.firstIndex(of: name), i + 1 < args.count, let v = Double(args[i + 1]) else { return dflt }
  args.removeSubrange(i...i + 1)
  return v
}
let maxNodes = Int(opt("--max-nodes", 5000)), maxDepth = Int(opt("--max-depth", 60))
let deadline = Date().addingTimeInterval(opt("--timeout", 20))
let text = args.contains("--text")
args.removeAll { $0 == "--text" }
guard let pidArg = args.first, let pid = pid_t(pidArg) else {
  FileHandle.standardError.write("usage: ax-dump <pid> [--text] [--max-nodes N] [--max-depth N] [--timeout S]\n".data(using: .utf8)!)
  exit(64)
}
if !AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary) {
  FileHandle.standardError.write(("ax-dump: no Accessibility grant. Add the app you run this from (your terminal) in "
    + "System Settings > Privacy & Security > Accessibility, then run it again.\n").data(using: .utf8)!)
  exit(2)
}

func attr(_ e: AXUIElement, _ name: String) -> AnyObject? {
  var v: AnyObject?
  return AXUIElementCopyAttributeValue(e, name as CFString, &v) == .success ? v : nil
}
func str(_ e: AXUIElement, _ name: String) -> String? {
  switch attr(e, name) {
  case let s as String: return s.isEmpty ? nil : s
  case let n as NSNumber: return n.stringValue
  default: return nil
  }
}
func frame(_ e: AXUIElement) -> [Int]? {
  guard let p = attr(e, kAXPositionAttribute), let s = attr(e, kAXSizeAttribute),
        CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
  var pt = CGPoint.zero, sz = CGSize.zero
  AXValueGetValue(p as! AXValue, .cgPoint, &pt)
  AXValueGetValue(s as! AXValue, .cgSize, &sz)
  return [Int(pt.x), Int(pt.y), Int(sz.width), Int(sz.height)]
}

var seen = 0, truncated = false
func walk(_ e: AXUIElement, _ depth: Int) -> [String: Any] {
  seen += 1
  var n: [String: Any] = [:]
  for (k, a) in [("role", kAXRoleAttribute), ("subrole", kAXSubroleAttribute), ("title", kAXTitleAttribute),
                 ("value", kAXValueAttribute), ("desc", kAXDescriptionAttribute), ("id", kAXIdentifierAttribute)] {
    if let v = str(e, a) { n[k] = v }
  }
  if let f = frame(e) { n["frame"] = f }
  let kids = attr(e, kAXChildrenAttribute) as? [AXUIElement] ?? []
  var out: [[String: Any]] = []
  for c in kids {
    if depth >= maxDepth || seen >= maxNodes || Date() > deadline { truncated = true; break }
    out.append(walk(c, depth + 1))
  }
  if !out.isEmpty { n["children"] = out }
  return n
}

let app = AXUIElementCreateApplication(pid)
AXUIElementSetMessagingTimeout(app, 2)
let windows = attr(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
if windows.isEmpty {
  FileHandle.standardError.write("ax-dump: pid \(pid) has no windows (not running, or no window open yet)\n".data(using: .utf8)!)
  exit(1)
}
let tree: [String: Any] = ["pid": Int(pid), "windows": windows.map { walk($0, 0) }, "nodes": seen, "truncated": truncated]

if text {
  func lines(_ n: [String: Any], _ depth: Int) {
    let words = ["title", "value", "desc"].compactMap { n[$0] as? String }.map { $0.replacingOccurrences(of: "\n", with: " ") }
    let role = (n["role"] as? String ?? "?").replacingOccurrences(of: "AX", with: "")
    let id = (n["id"] as? String).map { " #\($0)" } ?? ""
    if !words.isEmpty || !id.isEmpty || n["children"] != nil {
      print(String(repeating: "  ", count: depth) + role + id + (words.isEmpty ? "" : ": " + words.joined(separator: " | ")))
    }
    for c in n["children"] as? [[String: Any]] ?? [] { lines(c, depth + 1) }
  }
  for w in tree["windows"] as! [[String: Any]] { lines(w, 0) }
  if truncated { print("(truncated at \(seen) nodes)") }
} else {
  let data = try! JSONSerialization.data(withJSONObject: tree, options: [.prettyPrinted, .sortedKeys])
  FileHandle.standardOutput.write(data)
  print()
}
