import Foundation

/// The app is a menu bar agent (LSUIElement). It joins the Dock and ⌘Tab only
/// while one of its own windows is open, so a buried window can be found again.
public struct WindowFacts: Sendable, Equatable {
  public var titled: Bool
  public var panel: Bool
  public var visible: Bool
  public var miniaturized: Bool
  public init(titled: Bool, panel: Bool, visible: Bool, miniaturized: Bool) {
    self.titled = titled; self.panel = panel; self.visible = visible; self.miniaturized = miniaturized
  }
}

public enum DockPresence {
  /// True when any real window (titled, not the menu bar popover panel) is on
  /// screen or minimized to the Dock.
  public static func shows(_ windows: [WindowFacts]) -> Bool {
    windows.contains { $0.titled && !$0.panel && ($0.visible || $0.miniaturized) }
  }
}
