import Foundation
import Testing
@testable import WhosaidKit

let sidecarJSON = #"""
{"names": {"SPEAKER_00": "Alice_Example", "SPEAKER_01": "SPEAKER_01"},
 "roles": {"Alice_Example": "self"},
 "segments": [{"start": 0, "end": 4, "speaker": "SPEAKER_00"}, {"start": 4, "end": 14, "speaker": "SPEAKER_01"},
              {"start": 14, "end": 16, "speaker": "SPEAKER_00"}],
 "registry_matches": [
   {"cluster": "SPEAKER_00", "name": "Alice_Example", "matched": true, "similarity": 0.9},
   {"cluster": "SPEAKER_01", "name": "Bob_Example", "matched": false, "similarity": 0.55, "threshold": 0.7},
   {"cluster": "SPEAKER_01", "name": "Carol_Example", "matched": false, "similarity": 0.61, "threshold": 0.7}]}
"""#

@Test func clustersFromASidecar() throws {
  let cs = Speakers.clusters(sidecar: try JSONValue.parse(Data(sidecarJSON.utf8)))
  #expect(cs.map(\.cluster) == ["SPEAKER_01", "SPEAKER_00"])  // most talk first
  let unnamed = try #require(cs.first)
  #expect(unnamed.unnamed && unnamed.turns == 1 && unnamed.seconds == 10)
  #expect(unnamed.hint == .init(name: "Carol_Example", similarity: 0.61, threshold: 0.7))  // the best near miss
  let alice = cs[1]
  #expect(alice.name == "Alice_Example" && alice.role == "self" && alice.turns == 2 && alice.seconds == 6 && alice.hint == nil)
}

@Test func relabelNames() {
  #expect(Speakers.validName("Carol_Example") && Speakers.validName("ab-1"))
  #expect(!Speakers.validName("") && !Speakers.validName("Carol Example") && !Speakers.validName("x;rm"))
}

@Test func blankWhosaidVarsAreDroppedForChildren() {
  let env = WhosaidPaths.childEnvironment(["WHOSAID_SPEAKER_DB": "", "WHOSAID_WORKSPACE": "/ws", "HOME": "/h", "OTHER": ""])
  #expect(env == ["WHOSAID_WORKSPACE": "/ws", "HOME": "/h", "OTHER": ""])
}

/// G3: reading a meeting the way the views do (turns, items, cards, voices) makes no audio player.
@Test @MainActor func browsingMakesNoPlayer() throws {
  let dir = FileManager.default.temporaryDirectory.appendingPathComponent("whosaid-g3-\(UUID().uuidString)")
  let f = dir.appendingPathComponent("2026-10-27-0900")
  try FileManager.default.createDirectory(at: f, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: dir) }
  try sidecarJSON.write(to: f.appendingPathComponent("transcript.diarization.json"), atomically: true, encoding: .utf8)
  try "[00:00:00] Alice_Example: hi\n".write(to: f.appendingPathComponent("transcript.speakers.txt"), atomically: true, encoding: .utf8)
  let before = ClipPlayer.created
  let mf = MeetingFolder(workspace: dir.path, folder: "2026-10-27-0900")
  for _ in 0..<3 { _ = (mf.turns(), mf.items(), mf.speakerCards(), mf.speakerClusters()) }
  #expect(mf.speakerClusters().count == 2)
  #expect(ClipPlayer.created == before)
}
