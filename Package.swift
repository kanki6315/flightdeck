// swift-tools-version: 6.0
import PackageDescription
let package = Package(name: "FlightDeck", platforms: [.macOS(.v14)], products: [.executable(name: "FlightDeck", targets: ["FlightDeck"])], targets: [.executableTarget(name: "FlightDeck", swiftSettings: [.swiftLanguageMode(.v5)])])
