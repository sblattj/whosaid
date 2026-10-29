import Foundation

/// A Sendable JSON tree for MCP and CLI payloads.
public enum JSONValue: Sendable, Equatable, Codable {
  case null, bool(Bool), number(Double), string(String), array([JSONValue]), object([String: JSONValue])

  public init(from decoder: Decoder) throws {
    let c = try decoder.singleValueContainer()
    if c.decodeNil() { self = .null }
    else if let b = try? c.decode(Bool.self) { self = .bool(b) }
    else if let n = try? c.decode(Double.self) { self = .number(n) }
    else if let s = try? c.decode(String.self) { self = .string(s) }
    else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
    else { self = .object(try c.decode([String: JSONValue].self)) }
  }

  public func encode(to encoder: Encoder) throws {
    var c = encoder.singleValueContainer()
    switch self {
    case .null: try c.encodeNil()
    case .bool(let b): try c.encode(b)
    case .number(let n): try c.encode(n)
    case .string(let s): try c.encode(s)
    case .array(let a): try c.encode(a)
    case .object(let o): try c.encode(o)
    }
  }

  public subscript(key: String) -> JSONValue? { if case .object(let o) = self { o[key] } else { nil } }
  public var string: String? { if case .string(let s) = self { s } else { nil } }
  public var double: Double? { if case .number(let n) = self { n } else { nil } }
  public var int: Int? { double.map { Int($0) } }
  public var bool: Bool? { if case .bool(let b) = self { b } else { nil } }
  public var array: [JSONValue]? { if case .array(let a) = self { a } else { nil } }
  public var isNull: Bool { self == .null }

  public static func parse(_ data: Data) throws -> JSONValue { try JSONDecoder().decode(JSONValue.self, from: data) }
  public func data() throws -> Data { try JSONEncoder().encode(self) }
}
