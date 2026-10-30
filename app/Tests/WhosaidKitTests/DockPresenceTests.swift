import Testing
@testable import WhosaidKit

private func w(titled: Bool = true, panel: Bool = false, visible: Bool = true, mini: Bool = false) -> WindowFacts {
  WindowFacts(titled: titled, panel: panel, visible: visible, miniaturized: mini)
}

@Test func dockShowsWithAnOpenWindow() {
  #expect(DockPresence.shows([w()]))
  #expect(DockPresence.shows([w(visible: false, mini: true)]))
}

@Test func dockHidesWithOnlyTheMenuOrClosedWindows() {
  #expect(!DockPresence.shows([]))
  #expect(!DockPresence.shows([w(visible: false)]))
  #expect(!DockPresence.shows([w(panel: true)]))
  #expect(!DockPresence.shows([w(titled: false)]))
}
