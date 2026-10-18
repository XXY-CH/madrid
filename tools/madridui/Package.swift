// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "madridui",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "madridui",
            path: "Sources/madridui",
            linkerSettings: [
                .linkedLibrary("sqlite3"),
                .linkedFramework("AppKit"),
                .linkedFramework("SwiftUI"),
                .linkedFramework("Contacts")
            ]
        )
    ]
)
