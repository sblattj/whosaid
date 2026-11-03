import Testing
@testable import WhosaidKit

@Test func lanesHideAndShow() {
  var s = ""
  s = LiveLane.toggled(.topics, in: s)
  #expect(LiveLane.hidden(s) == [.topics])
  s = LiveLane.toggled(.nextSteps, in: s)
  #expect(s == "topics,nextSteps")
  s = LiveLane.toggled(.topics, in: s)
  #expect(LiveLane.hidden(s) == [.nextSteps])
}

@Test func theLastVisibleLaneStaysShown() {
  var s = ""
  for lane in LiveLane.allCases { s = LiveLane.toggled(lane, in: s) }
  #expect(LiveLane.hidden(s).count == LiveLane.allCases.count - 1)
  #expect(!LiveLane.hidden(s).contains(.nextSteps))
  #expect(LiveLane.hidden("transcript,topics,wordgraph,summaries,nextSteps").isEmpty)
  #expect(LiveLane.hidden("bogus,topics") == [.topics])
}
