#!/usr/bin/env python3
"""G4 for the macOS app: the app itself never touches the network.

No URLSession / NWConnection / CFNetwork / sockets anywhere in app/Sources; the
only WKWebView loads are loadFileURL of the bundled page (or a local error
string), every web view gets LocalOnlyNavigation, and the shipped wordgraph page
carries the CSP and no fetch / XHR / WebSocket / EventSource. A session-wide
"ask the engine" goes through the whosaid CLI (`live ask`), never Swift.
Static check over the tree; nothing runs."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "app" / "Sources"
PAGE = SRC / "WhosaidApp" / "Resources" / "wordgraph"
CHECKS = 0


def check(ok, msg):
    global CHECKS
    CHECKS += 1
    if not ok:
        print(f"FAIL: {msg}")
        sys.exit(1)


def where(f, text, m):
    return f"{f.relative_to(ROOT)}:{text[:m.start()].count(chr(10)) + 1}"


NETWORK = re.compile(r"\bURLSession\b|\bNWConnection\b|\bNWListener\b|\bNWPathMonitor\b|\bCFStream\w*|"
                     r"\bCFSocket\w*|\bsocket\(|\bURLRequest\(url:\s*URL\(string:\s*\"https?:|"
                     r"\bInputStream\(url:|\bData\(contentsOf:\s*URL\(string:")
files = sorted(SRC.rglob("*.swift"))
check(files, f"no Swift sources under {SRC}")
webviews = loads = 0
for f in files:
    text = f.read_text()
    for m in NETWORK.finditer(text):
        check(False, f"{where(f, text, m)}: network API in the app ({m.group(0)})")
    for m in re.finditer(r"\.load\(|\.loadRequest\(|\.loadData\(|\.loadSimulatedRequest\(", text):
        check(False, f"{where(f, text, m)}: a WKWebView load that is not loadFileURL")
    for m in re.finditer(r"\.loadHTMLString\([^\n]*baseURL:\s*([^)\n]+)\)", text):
        check(m.group(1).strip() == "nil", f"{where(f, text, m)}: loadHTMLString with a base URL")
    loads += len(re.findall(r"\.loadFileURL\(", text))
    for m in re.finditer(r"WKWebView\(frame:", text):
        webviews += 1
        # The view must get the local-only delegate right after it is made.
        after = text[m.start():m.start() + 400]
        check(".navigationDelegate = context.coordinator.navigation" in after or "LocalOnlyNavigation" in after,
              f"{where(f, text, m)}: WKWebView without LocalOnlyNavigation")
check(webviews >= 1 and loads >= 1, "expected the wordgraph web view and its loadFileURL")
nav = (SRC / "WhosaidKit" / "LocalWeb.swift").read_text()
check('scheme == "file" || scheme == "about"' in nav and "decisionHandler(.cancel)" in nav,
      "LocalOnlyNavigation allows file/about only and cancels the rest")

html = (PAGE / "index.html").read_text()
check("Content-Security-Policy" in html and "connect-src 'none'" in html and "default-src 'self'" in html,
      "the wordgraph page carries the D14 CSP")
check(not re.search(r"<script[^>]+src=[\"']https?:|<link[^>]+href=[\"']https?:", html), "the page loads nothing remote")
js = (PAGE / "index.js").read_text()
for pat in (r"\bfetch\(", r"XMLHttpRequest", r"\bWebSocket\b", r"\bEventSource\b", r"sendBeacon", r"importScripts"):
    check(re.search(pat, js) is None, f"the wordgraph bundle uses {pat}")
remote = [u for u in re.findall(r"https?://[^\"'\s)]+", js) if not u.startswith("http://www.w3.org/")]
check(not remote, f"the wordgraph bundle names remote URLs: {remote[:3]}")

# Bad case: the checker must catch an injected URLSession.
check(NETWORK.search('let s = URLSession.shared') is not None, "the pattern catches URLSession")
print(f"PASS: app_network_gate_test ({CHECKS} checks)")
