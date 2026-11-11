import Foundation
import Testing
@testable import WhosaidKit

@Test func draftRowFromListAndShow() throws {
  let v = try JSONValue.parse(Data(#"""
  {"id": "20260101-0900-quill-example-widget", "to": "Quill Example", "topic": "widget flags", "state": "verified",
   "pages": ["/tmp/widget-flags.html"], "mentions": ["Quill Example"], "updated_at": "2026-01-01T09:00:00Z",
   "owner_line": "worker-a", "rivals": 0, "text": "the widget build needs **two** flags", "gate": []}
  """#.utf8))
  let d = try #require(DraftRow(v))
  #expect(d.stageable && !d.staged && d.stateLabel == "ready to stage")
  #expect(d.pages == ["/tmp/widget-flags.html"] && d.mentions == ["Quill Example"] && d.text?.contains("two") == true)
  let rival = try #require(DraftRow(try JSONValue.parse(Data(#"{"id":"a","to":"B","state":"verified","rivals":1}"#.utf8))))
  #expect(!rival.stageable)
  let stale = try #require(DraftRow(try JSONValue.parse(Data(#"{"id":"a","to":"B","state":"stale"}"#.utf8))))
  #expect(!stale.stageable && stale.stateLabel == "edited after verify")
  let again = try #require(DraftRow(try JSONValue.parse(Data(#"{"id":"a","to":"B","state":"verified, changed since staged"}"#.utf8))))
  #expect(again.stageable)
  let fix = try #require(DraftRow(try JSONValue.parse(Data(#"{"id":"a","to":"B","state":"sent, edit verified"}"#.utf8))))
  #expect(fix.stageable && fix.isEdit && !fix.staged && fix.stateLabel == "correction ready to stage")
  let fixing = try #require(DraftRow(try JSONValue.parse(Data(#"{"id":"a","to":"B","state":"sent, edit drafting"}"#.utf8))))
  #expect(!fixing.stageable && fixing.isEdit)
  let saved = try #require(DraftRow(try JSONValue.parse(Data(#"{"id":"a","to":"B","state":"sent, edit staged"}"#.utf8))))
  #expect(saved.staged && !saved.stageable)
  #expect(DraftRow(try JSONValue.parse(Data(#"{"to":"B"}"#.utf8))) == nil)
}

@Test func stageOutcomeLines() throws {
  let ok = StageOutcome(try JSONValue.parse(Data(#"""
  {"ok": true, "staged": [{"id": "a", "to": "Quill Example", "stager": "{}"}], "skipped": [], "failed": []}
  """#.utf8)))
  #expect(ok.ok && ok.staged == ["a"] && ok.lines.first?.contains("press send") == true)
  let bad = StageOutcome(try JSONValue.parse(Data(#"""
  {"ok": true, "staged": [], "skipped": [{"id": "b", "why": "already staged"}], "failed": [{"id": "c", "why": "stale"}]}
  """#.utf8)))
  #expect(!bad.ok && bad.lines == ["Skipped: already staged", "Not staged: stale"])
  let err = StageOutcome(try JSONValue.parse(Data(#"{"ok": false, "error": "outbox locked"}"#.utf8)))
  #expect(!err.ok && err.lines == ["outbox locked"])
  #expect(StageOutcome(try JSONValue.parse(Data(#"{"ok": true, "staged": [], "skipped": [], "failed": []}"#.utf8))).lines == ["Nothing to stage."])
  let edit = StageOutcome(try JSONValue.parse(Data(#"""
  {"ok": true, "edit": true, "staged": [{"id": "a", "to": "Quill Example"}], "skipped": [], "failed": [],
   "warnings": ["you posted 1 message(s) after this one"]}
  """#.utf8)))
  #expect(edit.ok && edit.lines.first?.contains("save it") == true && edit.lines.last?.hasPrefix("Warning:") == true)
}

@Test func draftsFolderHonoursTheOverride() {
  #expect(DraftsFolder.url(["WHOSAID_DRAFTS_DIR": "/tmp/d"]).path == "/tmp/d")
  #expect(DraftsFolder.url([:]).path.hasSuffix("/.drafts"))
}
