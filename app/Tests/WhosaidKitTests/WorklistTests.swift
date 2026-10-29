import Foundation
import Testing
@testable import WhosaidKit

@Test func worklistRowKeepsTierWhyAndSource() throws {
  let row = try JSONValue.parse(Data(#"""
  {"id": "CM-002", "text": "I will draft the roadmap by Thursday", "status": "open", "owner": "Alice_Example",
   "tier": "P1", "why": ["due=by thursday", "strong cue"], "kind": "explicit_commitment", "first_seen": "2026-10-27-0900",
   "acceptance_evidence": [{"source": "commitments", "meeting": "2026-10-28-1400", "t_sec": null},
                           {"source": "transcript", "meeting": "2026-10-28-1400", "t_sec": 5}]}
  """#.utf8))
  let i = try #require(TrackedItem(row))
  #expect(i.tier == "P1" && i.why == ["due=by thursday", "strong cue"] && i.kindLabel == "explicit commitment")
  #expect(i.meeting == "2026-10-28-1400" && i.t == 5 && !i.isDone)  // the evidence with a time wins
}

@Test func itemsRowAndFilters() throws {
  let open = try #require(TrackedItem(try JSONValue.parse(Data(#"{"id":"AI-002","text":"Review","status":"open","tier":"","type":"Asks from the team","first_seen":"2026-10-27-0900"}"#.utf8))))
  let done = try #require(TrackedItem(try JSONValue.parse(Data(#"{"id":"AI-001","text":"Send","status":"delivered"}"#.utf8))))
  #expect(open.tier == nil && open.section == "Asks from the team" && open.meeting == "2026-10-27-0900" && open.t == nil)
  #expect(done.isDone && ItemFilter.done.keeps(done) && !ItemFilter.open.keeps(done) && ItemFilter.all.keeps(open))
  #expect(TrackedItem(try JSONValue.parse(Data(#"{"id":"AI-3"}"#.utf8))) == nil)
}

@Test func suggestionNoteCarriesTheQuote() throws {
  let s = try #require(CompletionSuggestion(try JSONValue.parse(Data(#"""
  {"item_id": "AI-001", "status": "delivered", "id": "EV-1", "reason": "shares subject words",
   "evidence": {"meeting": "2026-10-29-0800", "quote": "I sent the release notes.", "speaker": "Alice_Example", "t_sec": 130}}
  """#.utf8))))
  #expect(s.itemId == "AI-001" && s.t == 130 && s.meeting == "2026-10-29-0800")
  #expect(s.note == #"Marked done in the whosaid app after reviewing: "I sent the release notes." (Alice_Example, 2026-10-29-0800)"#)
}
