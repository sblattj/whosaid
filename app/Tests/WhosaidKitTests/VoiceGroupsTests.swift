import Foundation
import Testing
@testable import WhosaidKit

let voiceGroupsJSON = #"""
{"ok": true, "clusters": 4, "short_skipped": 1, "groups": [
  {"id": "vg-0123456789", "meetings": 3, "clusters": 3, "seconds": 360, "first": "2026-03-01", "last": "2026-03-10",
   "recent": true, "cohesion": 0.9, "hint": null,
   "members": [{"id": "2026-03-10-0900/transcript.diarization.json#SPEAKER_03", "folder": "2026-03-10-0900",
                "cluster": "SPEAKER_03", "seconds": 200, "date": "2026-03-10"},
               {"id": "2026-03-01-0900/transcript.diarization.json#SPEAKER_00", "folder": "2026-03-01-0900",
                "cluster": "SPEAKER_00", "seconds": 120, "date": "2026-03-01"}]},
  {"id": "vg-abcdefabcd", "meetings": 1, "clusters": 1, "seconds": 20, "first": null, "last": null, "recent": false,
   "hint": {"name": "Ada_Example", "similarity": 0.81, "role": "boss", "next": {"name": "Bob_Example", "similarity": 0.2}},
   "members": [{"id": "notes/x.diarization.json#SPEAKER_02", "folder": "notes", "cluster": "SPEAKER_02", "seconds": 20}]},
  {"meetings": 2}]}
"""#

@Test func voiceGroupsFromTheTool() throws {
  let gs = VoiceGroup.list(try JSONValue.parse(Data(voiceGroupsJSON.utf8)))
  #expect(gs.map(\.id) == ["vg-0123456789", "vg-abcdefabcd"])  // a row without an id is dropped
  let big = gs[0]
  #expect(big.meetings == 3 && big.recent && big.hint == nil && big.members.count == 2)
  #expect(big.loudest?.folder == "2026-03-10-0900" && big.loudest?.cluster == "SPEAKER_03")
  #expect(big.summary == "3 meetings · 6 min · last 2026-03-10")
  let one = gs[1]
  #expect(one.hint == .init(name: "Ada_Example", similarity: 0.81, role: "boss"))
  #expect(one.summary == "1 meeting · <1 min" && !one.recent)
}
