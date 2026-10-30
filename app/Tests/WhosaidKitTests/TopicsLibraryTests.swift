import Foundation
import Testing
@testable import WhosaidKit

private let file = #"""
{"version": 1, "built": "2026-10-29T12:00:00Z", "meetings": 3, "skippedChats": 1,
 "topics": [
  {"id": "release-notes", "rootId": "release-notes", "label": "Release Notes", "rank": 2, "share": 0.3,
   "speakingMs": 125000, "visits": 1, "terms": ["version", "changelog"], "firstDate": "2026-09-01T09:00",
   "lastDate": "2026-09-01T09:00",
   "meetings": [{"folder": "2026-09-01-0900", "date": "2026-09-01T09:00", "label": "Release Notes", "speakingMs": 125000, "firstT": 310.5}]},
  {"id": "storage-bucket", "rootId": "storage-bucket", "label": "Storage Bucket", "rank": 1, "share": 0.7,
   "speakingMs": 3725000, "visits": 2, "terms": ["cost", "budget", "retention"], "firstDate": "2026-09-08T09:00",
   "lastDate": "2026-09-15T09:00",
   "meetings": [{"folder": "2026-09-15-0900", "date": "2026-09-15T09:00", "label": "Storage Bucket", "speakingMs": 200000, "firstT": 600},
                {"folder": "2026-09-08-0900", "date": "2026-09-08T09:00", "label": "Cost Alerts", "speakingMs": 90000, "firstT": 12}]},
  {"label": "no id, dropped"}],
 "nodes": [
  {"id": "storage-bucket", "topicId": "storage-bucket", "parentId": null, "depth": 0, "label": "Storage Bucket"},
  {"id": "storage-bucket/cost", "topicId": "storage-bucket", "parentId": "storage-bucket", "depth": 1, "label": "cost"},
  {"id": "release-notes", "topicId": "release-notes", "parentId": null, "depth": 0, "label": "Release Notes"}]}
"""#

@Test func topicsLibraryReadsRankedTopicsAndMeetings() throws {
  let lib = try #require(TopicsLibrary(data: Data(file.utf8)))
  #expect(lib.built == "2026-10-29T12:00:00Z" && lib.meetingCount == 3)
  #expect(lib.topics.map(\.id) == ["storage-bucket", "release-notes"])  // by rank, the id-less one dropped
  let t = lib.topics[0]
  #expect(t.meetingCount == 2 && t.terms == ["cost", "budget", "retention"] && t.lastDate == "2026-09-15T09:00")
  #expect(t.meetings.map(\.folder) == ["2026-09-15-0900", "2026-09-08-0900"] && t.meetings[1].firstT == 12)
  #expect(TopicsLibrary(data: Data(#"{"version": 2, "topics": []}"#.utf8)) == nil)
  #expect(TopicsLibrary(data: Data("not json".utf8)) == nil)
}

@Test func topicsLibraryGraphHoldsOnlyThatTopic() throws {
  let lib = try #require(TopicsLibrary(data: Data(file.utf8)))
  let json = try #require(lib.graphJSON(for: "storage-bucket"))
  let v = try JSONValue.parse(Data(json.utf8))
  #expect(v["topics"]?.array?.map { $0["id"]?.string } == ["storage-bucket"])
  #expect(v["nodes"]?.array?.compactMap { $0["topicId"]?.string } == ["storage-bucket", "storage-bucket"])
  #expect(lib.graphJSON(for: "nope") == nil)
}

@Test func topicsLibraryLabels() throws {
  #expect(TopicsLibrary.dateLabel("2026-09-15T09:00") == "Sep 15, 2026")
  #expect(TopicsLibrary.dateLabel(nil) == "" && TopicsLibrary.dateLabel("soon") == "")
  #expect(TopicsLibrary.duration(125_000) == "2:05" && TopicsLibrary.duration(3_725_000) == "1:02:05")
  let dir = FileManager.default.temporaryDirectory.appendingPathComponent("whosaid-topics-\(UUID().uuidString)")
  try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: dir) }
  #expect(TopicsLibrary.read(workspace: dir.path) == nil)
  try Data(file.utf8).write(to: TopicsLibrary.url(workspace: dir.path))
  #expect(TopicsLibrary.read(workspace: dir.path)?.topics.count == 2)
}
