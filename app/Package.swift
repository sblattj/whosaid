// swift-tools-version:6.0
import PackageDescription
let package = Package(
  name: "Whosaid",
  platforms: [.macOS(.v15)],
  products: [.executable(name: "WhosaidApp", targets: ["WhosaidApp"])],
  targets: [
    .target(name: "WhosaidKit"),
    .executableTarget(name: "WhosaidApp", dependencies: ["WhosaidKit"]),
    .testTarget(name: "WhosaidKitTests", dependencies: ["WhosaidKit"]),
  ]
)
