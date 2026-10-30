import Foundation
import WebKit

/// D14: the wordgraph page may only ever load its own files. Every navigation that is
/// not file: or about: is cancelled and counted, and the page's CSP blocks fetches.
public enum LocalWeb {
  public static func allows(_ url: URL?) -> Bool {
    guard let scheme = url?.scheme?.lowercased() else { return true }  // about:blank before the first load
    return scheme == "file" || scheme == "about"
  }

  /// Bundle lookup: the .app's Contents/Resources first (`whosaid app build` copies the
  /// SwiftPM bundle there), then a dev build's folder next to the binary.
  public static func resourceFolder(bundleName: String, folder: String) -> URL? {
    let fm = FileManager.default
    let candidates = [Bundle.main.resourceURL, Bundle.main.executableURL?.deletingLastPathComponent()]
      .compactMap { $0?.appendingPathComponent(bundleName) }
    for b in candidates {
      for sub in [folder, "Contents/Resources/\(folder)"] {
        let u = b.appendingPathComponent(sub)
        if fm.fileExists(atPath: u.appendingPathComponent("index.html").path) { return u }
      }
    }
    return nil
  }
}

@MainActor public final class LocalOnlyNavigation: NSObject, WKNavigationDelegate {
  public private(set) var blocked: [URL] = []
  public var onBlock: ((URL) -> Void)?

  public override init() {}

  public func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                      decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
    if LocalWeb.allows(action.request.url) { decisionHandler(.allow); return }
    if let u = action.request.url { blocked.append(u); onBlock?(u) }
    decisionHandler(.cancel)
  }
}
