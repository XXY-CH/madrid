// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "madridctl",
    targets: [
        .executableTarget(
            name: "madridctl",
            path: "Sources/madridctl",
            linkerSettings: [
                .linkedLibrary("sqlite3"),
                .linkedFramework("AppKit"),
                .linkedFramework("Contacts")
            ]
        )
    ]
)
