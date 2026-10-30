import Testing
import Foundation
@testable import WhosaidKit

@Test func jsonRoundTrip() throws {
  let v = try JSONValue.parse(Data(#"{"a":[1,true,null,"x"],"b":{"c":2.5}}"#.utf8))
  #expect(v["a"]?.array?.count == 4)
  #expect(v["b"]?["c"]?.double == 2.5)
  #expect(try JSONValue.parse(v.data()) == v)
}

@Test(.enabled(if: ProcessInfo.processInfo.environment["WHOSAID_TEST_BIN"] != nil))
func mcpRoundTrip() async throws {
  let env = ProcessInfo.processInfo.environment
  let ws = FileManager.default.temporaryDirectory.appendingPathComponent("whosaid-mcp-\(UUID().uuidString)")
  try FileManager.default.createDirectory(at: ws, withIntermediateDirectories: true)
  try "[summarizer]\nengine = \"none\"\n".write(to: ws.appendingPathComponent("whosaid.toml"), atomically: true, encoding: .utf8)
  defer { try? FileManager.default.removeItem(at: ws) }
  let c = MCPClient(whosaid: URL(fileURLWithPath: env["WHOSAID_TEST_BIN"]!), workspace: ws.path)
  let tools = try await c.listTools()
  #expect(tools.contains("whosaid_meetings"))
  let r = try await c.call("whosaid_meetings")
  #expect(r["ok"]?.bool == false)
  #expect(r["hint"]?.string?.contains("whosaid index") == true)
  await c.stop()
}

/// G8: an MCP server that dies at start is an error the app can show, not a hang.
@Test func mcpServerThatExitsThrows() async throws {
  let dir = FileManager.default.temporaryDirectory.appendingPathComponent("whosaid-mcpfail-\(UUID().uuidString)")
  try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: dir) }
  let bin = dir.appendingPathComponent("whosaid")
  try "#!/bin/sh\necho 'simulated MCP failure' >&2\nexit 1\n".write(to: bin, atomically: true, encoding: .utf8)
  try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: bin.path)
  let c = MCPClient(whosaid: bin, workspace: dir.path)
  let started = Date()
  await #expect(throws: WhosaidError.self) { try await c.call("whosaid_meetings", timeout: 10) }
  #expect(Date().timeIntervalSince(started) < 8)
  await c.stop()
}
