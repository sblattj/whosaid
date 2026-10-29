import Foundation
import Testing
@testable import WhosaidKit

@Test func parsesSpeakerLines() {
  let t = Transcript.parse("""
    [00:00:01] Alice_Example: Morning: all good.
    not a turn
    [00:01:05.5] SPEAKER_02: Is the demo on Friday?
    [1:02:03] Bob_Example: Late one.
    """)
  #expect(t.count == 3)
  #expect(t[0].speaker == "Alice_Example" && t[0].text == "Morning: all good." && t[0].t == 1)
  #expect(t[1].t == 65.5 && t[1].unnamed && !t[0].unnamed)
  #expect(t[2].t == 3723 && t[2].clock == "1:02:03" && t[0].clock == "00:01")
}

@Test func talkTimeRunsToTheNextTurn() {
  let t = Transcript.parse("[00:00:00] A: x\n[00:00:10] B: y\n[00:00:12] A: z\n")
  let tt = Transcript.talkTime(t, end: 20)
  #expect(tt.map(\.speaker) == ["A", "B"])
  #expect(tt[0].seconds == 15 && tt[1].seconds == 2)
  #expect(Transcript.talkTime(t, end: 14)[0].seconds == 12)
  #expect(Transcript.speakers(t) == ["A", "B"])
}

@Test func meetingSummaryFromRow() throws {
  let row = try JSONValue.parse(Data(#"{"folder":"2026-10-28-1400-sprint-planning","created":"2026-10-28T21:00:00Z","duration_s":2710,"kind":"audio","segments":2}"#.utf8))
  let m = try #require(MeetingSummary(row))
  #expect(m.title == "sprint planning" && m.length == "45m" && !m.isChat && m.created != nil)
  let chat = try #require(MeetingSummary(try JSONValue.parse(Data(#"{"folder":"chat-x","kind":"teams-chat","duration_s":5400}"#.utf8))))
  #expect(chat.isChat && chat.title == "Teams chat" && chat.length == "1h 30m")
}

@Test func untitledFoldersAreNamedBySource() throws {
  func row(_ json: String) throws -> MeetingSummary { try #require(MeetingSummary(try JSONValue.parse(Data(json.utf8)))) }
  #expect(try row(#"{"folder":"2026-10-28-1400","source_name":"20261028T210000Z-teams.m4a"}"#).title == "Teams call")
  #expect(try row(#"{"folder":"2026-10-28-1400-2","source_name":"20261028 140000.m4a"}"#).title == "Recording")
  #expect(try row(#"{"folder":"2026-10-28-1400","kind":"outlook-mail"}"#).title == "Email")
  let local = try #require(try row(#"{"folder":"2026-10-28-1400"}"#).created)
  #expect(Calendar.current.dateComponents([.hour, .minute], from: local) == DateComponents(hour: 14, minute: 0))
}

@Test func readsAMeetingFolder() throws {
  let dir = FileManager.default.temporaryDirectory.appendingPathComponent("whosaid-mf-\(UUID().uuidString)")
  let f = dir.appendingPathComponent("2026-10-27-0900-standup")
  try FileManager.default.createDirectory(at: f, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: dir) }
  try "[00:00:01] Alice_Example: hi\n".write(to: f.appendingPathComponent("standup.speakers.txt"), atomically: true, encoding: .utf8)
  try "ignored".write(to: f.appendingPathComponent(".hidden.speakers.txt"), atomically: true, encoding: .utf8)
  try #"{"items":[{"line":1,"owner":"Alice_Example","text":"Send notes.","section":"Own"}]}"#
    .write(to: f.appendingPathComponent("action-items.json"), atomically: true, encoding: .utf8)
  let mf = MeetingFolder(workspace: dir.path, folder: "2026-10-27-0900-standup")
  #expect(mf.turns().count == 1)
  #expect(mf.items() == [FolderItem(line: 1, owner: "Alice_Example", text: "Send notes.", section: "Own")])
  #expect(mf.speakerCards() == nil)
}
