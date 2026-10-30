import Foundation

public enum WhosaidError: Error, Sendable, Equatable {
  case notRunning, timeout(String), rpc(String), badReply(String), exited(Int32)
}

/// JSON-RPC over stdio to `whosaid mcp` (D2). One long-lived child; calls are
/// matched by id; each call has a deadline; a dead child is restarted on the next call.
public actor MCPClient {
  let executable: URL
  let environment: [String: String]
  var process: Process?
  var stdin: FileHandle?
  var nextID = 1
  var pending: [Int: CheckedContinuation<JSONValue, Error>] = [:]
  var buffer = Data()

  public init(whosaid: URL, workspace: String?) {
    executable = whosaid
    var env = WhosaidPaths.childEnvironment()
    if let workspace { env["WHOSAID_WORKSPACE"] = workspace }
    environment = env
  }

  public func start() async throws {
    if let p = process, p.isRunning { return }
    let p = Process()
    p.executableURL = executable
    p.arguments = ["mcp"]
    p.environment = environment
    let inPipe = Pipe(), outPipe = Pipe()
    p.standardInput = inPipe
    p.standardOutput = outPipe
    p.standardError = FileHandle.nullDevice
    outPipe.fileHandleForReading.readabilityHandler = { [weak self] h in
      let chunk = h.availableData
      Task { await self?.receive(chunk) }
    }
    p.terminationHandler = { [weak self] proc in
      let pid = proc.processIdentifier, status = proc.terminationStatus
      Task { await self?.died(status, pid: pid) }
    }
    try p.run()
    process = p
    stdin = inPipe.fileHandleForWriting
    _ = try await request("initialize", params: .object([
      "protocolVersion": .string("2025-06-18"), "capabilities": .object([:]),
      "clientInfo": .object(["name": .string("whosaid-app"), "version": .string("0")])]), timeout: 60)
    try send(.object(["jsonrpc": .string("2.0"), "method": .string("notifications/initialized")]))
  }

  public func stop() {
    process?.terminate()
    process = nil
    stdin = nil
  }

  /// tools/call; returns the tool's JSON payload (the text content parsed as JSON).
  public func call(_ tool: String, _ args: [String: JSONValue] = [:], timeout: Double = 30) async throws -> JSONValue {
    try await start()
    let r = try await request("tools/call", params: .object(["name": .string(tool), "arguments": .object(args)]), timeout: timeout)
    guard let text = r["content"]?.array?.first?["text"]?.string else { throw WhosaidError.badReply(tool) }
    return (try? JSONValue.parse(Data(text.utf8))) ?? .string(text)
  }

  public func listTools() async throws -> [String] {
    try await start()
    let r = try await request("tools/list", params: .object([:]), timeout: 30)
    return (r["tools"]?.array ?? []).compactMap { $0["name"]?.string }
  }

  func request(_ method: String, params: JSONValue, timeout: Double) async throws -> JSONValue {
    let id = nextID
    nextID += 1
    try send(.object(["jsonrpc": .string("2.0"), "id": .number(Double(id)), "method": .string(method), "params": params]))
    let deadline = Task { [weak self] in
      try? await Task.sleep(for: .seconds(timeout))
      await self?.fail(id, WhosaidError.timeout(method))
    }
    defer { deadline.cancel() }
    return try await withCheckedThrowingContinuation { pending[id] = $0 }
  }

  func send(_ msg: JSONValue) throws {
    guard let stdin else { throw WhosaidError.notRunning }
    var data = try msg.data()
    data.append(0x0A)
    try stdin.write(contentsOf: data)
  }

  func receive(_ chunk: Data) {
    buffer.append(chunk)
    while let nl = buffer.firstIndex(of: 0x0A) {
      let line = buffer[buffer.startIndex..<nl]
      buffer = Data(buffer[buffer.index(after: nl)...])
      guard let msg = try? JSONValue.parse(Data(line)), let id = msg["id"]?.int, let k = pending.removeValue(forKey: id) else { continue }
      if let err = msg["error"] { k.resume(throwing: WhosaidError.rpc(err["message"]?.string ?? "error")) }
      else { k.resume(returning: msg["result"] ?? .null) }
    }
  }

  func fail(_ id: Int, _ error: Error) {
    pending.removeValue(forKey: id)?.resume(throwing: error)
  }

  func died(_ status: Int32, pid: Int32) {
    // a late handler from a child that stop() already let go of must not touch its successor
    if let p = process, p.processIdentifier != pid { return }
    process = nil
    stdin = nil
    for (_, k) in pending { k.resume(throwing: WhosaidError.exited(status)) }
    pending.removeAll()
  }
}
