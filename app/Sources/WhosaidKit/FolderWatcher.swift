import Foundation

/// Calls `onChange` (debounced) when entries are added, removed or renamed in a folder.
/// A DispatchSource on the folder's descriptor: no polling, no FSEvents permission.
public final class FolderWatcher: @unchecked Sendable {
  private let source: DispatchSourceFileSystemObject
  private let queue = DispatchQueue(label: "whosaid.folder-watcher")
  private var pending: DispatchWorkItem?

  public init?(path: String, debounce: TimeInterval = 1.0, onChange: @escaping @Sendable () -> Void) {
    let fd = open(path, O_EVTONLY)
    guard fd >= 0 else { return nil }
    source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write, .rename, .delete],
                                                       queue: queue)
    source.setEventHandler { [weak self] in
      guard let self else { return }
      self.pending?.cancel()
      let work = DispatchWorkItem(block: onChange)
      self.pending = work
      self.queue.asyncAfter(deadline: .now() + debounce, execute: work)
    }
    source.setCancelHandler { close(fd) }
    source.resume()
  }

  public func cancel() { source.cancel() }
  deinit { source.cancel() }
}
