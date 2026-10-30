import SwiftUI
import WebKit
import WhosaidKit

/// D4/D12: jibbr's balloon wordgraph, as a static page in the app bundle. Swift pushes the
/// insights with evaluateJavaScript; the page posts clicks, folds and fps back. D14: only
/// file: and about: navigations are allowed, and the page's CSP blocks every fetch.
struct WordgraphView: NSViewRepresentable {
  @Environment(AppModel.self) var model
  let insightsJSON: String?
  var motion = "lively"
  /// -1 shows every depth; the narrow lane passes 1 (topics and ideas).
  var maxDepth = -1

  static var page: URL? { LocalWeb.resourceFolder(bundleName: "Whosaid_WhosaidApp.bundle", folder: "wordgraph") }

  func makeCoordinator() -> Coordinator { Coordinator(live: model.live) }

  func makeNSView(context: Context) -> WKWebView {
    let config = WKWebViewConfiguration()
    config.userContentController.add(context.coordinator, name: "whosaid")
    config.websiteDataStore = .nonPersistent()
    let web = WKWebView(frame: .zero, configuration: config)
    web.navigationDelegate = context.coordinator.navigation
    web.setValue(false, forKey: "drawsBackground")
    context.coordinator.web = web
    context.coordinator.navigation.onBlock = { [weak live = model.live] _ in live?.blockedNavigations += 1 }
    if let dir = Self.page {
      web.loadFileURL(dir.appendingPathComponent("index.html"), allowingReadAccessTo: dir)
    } else {
      web.loadHTMLString("<body style='background:#101216;color:#969da9;font:13px -apple-system'>The wordgraph page is missing from the app bundle. Rebuild with <code>whosaid app build</code>.</body>", baseURL: nil)
    }
    return web
  }

  func updateNSView(_ web: WKWebView, context: Context) {
    context.coordinator.push(json: insightsJSON, motion: motion, maxDepth: maxDepth)
  }

  static func dismantleNSView(_ web: WKWebView, coordinator: Coordinator) {
    web.configuration.userContentController.removeScriptMessageHandler(forName: "whosaid")
  }

  @MainActor final class Coordinator: NSObject, WKScriptMessageHandler {
    let navigation = LocalOnlyNavigation()
    weak var web: WKWebView?
    weak var live: LiveModel?
    var ready = false
    var sentJSON: String?
    var sentMotion: String?
    var wantJSON: String?
    var wantMotion = "lively"
    var sentDepth: Int?
    var wantDepth = -1

    init(live: LiveModel) { self.live = live }

    func push(json: String?, motion: String, maxDepth: Int) {
      wantJSON = json
      wantMotion = motion
      wantDepth = maxDepth
      guard ready, let web else { return }
      if maxDepth != sentDepth {  // before the data, so the first draw is already filtered
        sentDepth = maxDepth
        web.evaluateJavaScript("window.whosaidWordgraph && window.whosaidWordgraph.setMaxDepth(\(maxDepth))")
      }
      if let json, json != sentJSON {
        let first = sentJSON == nil
        sentJSON = json
        web.evaluateJavaScript("window.whosaidWordgraph && window.whosaidWordgraph.setInsights(\(json))")
        if first {  // fit again once the balloons have spread out and the view has its final size
          DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak web] in
            web?.evaluateJavaScript("window.whosaidWordgraph && window.whosaidWordgraph.fit()")
          }
        }
      }
      if motion != sentMotion {
        sentMotion = motion
        web.evaluateJavaScript("window.whosaidWordgraph && window.whosaidWordgraph.setMotion('\(motion)')")
      }
    }

    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
      guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
      switch type {
      case "ready":
        ready = true
        live?.webReady = true
        push(json: wantJSON, motion: wantMotion, maxDepth: wantDepth)
      case "fps":
        live?.fps = (body["fps"] as? NSNumber)?.doubleValue
        live?.fpsNodes = (body["nodes"] as? NSNumber)?.intValue ?? 0
      case "click":
        live?.selectedNode = body["label"] as? String
      default: break
      }
    }
  }
}
