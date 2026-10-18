import SQLite3
import Foundation
import AppKit
import Contacts

// madridctl — management tool for the madrid iMessage bridge.
//
// Commands:
//   madridctl doctor          Full environment check (permissions, database, accounts)
//   madridctl permissions     Check and guide macOS permission setup
//   madridctl accounts        List iMessage accounts and connection status
//   madridctl watch           Monitor madrid health (serve, watch, bridge)
//   madridctl setup           Interactive setup wizard
//   madridctl bridge          Bridge status and management

let args = CommandLine.arguments
let command = args.count > 1 ? args[1] : "doctor"

switch command {
case "doctor": doctor()
case "permissions": permissions()
case "accounts": accounts()
case "watch": watchHealth()
case "setup": setup()
case "bridge": bridgeStatus()
case "--help", "-h": printHelp()
default:
    print("Unknown command: \(command)")
    printHelp()
    exit(1)
}

// MARK: - Doctor

func doctor() {
    print("madrid doctor")
    print(String(repeating: "=", count: 50))
    print("")

    var allPass = true

    // 1. System
    print("System")
    let os = ProcessInfo.processInfo
    print("  macOS \(os.operatingSystemVersionString)")
    print("  arch: arm64")
    print("")

    // 2. Permissions
    print("Permissions")
    let perms = checkPermissions()
    for (name, granted) in perms {
        let icon = granted ? "✅" : "❌"
        print("  \(icon) \(name)")
        if !granted { allPass = false }
    }
    print("")

    // 3. Database
    print("Database")
    let dbPath = NSHomeDirectory() + "/Library/Messages/chat.db"
    let dbExists = FileManager.default.fileExists(atPath: dbPath)
    print("  \(dbExists ? "✅" : "❌") chat.db exists: \(dbPath)")
    if dbExists {
        let attrs = try? FileManager.default.attributesOfItem(atPath: dbPath)
        if let size = attrs?[.size] as? Int {
            print("     size: \(ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file))")
        }
        // Test read
        if canReadDB(atPath: dbPath) {
            let count = messageCount(atPath: dbPath)
            print("  ✅ readable, \(count) messages")
        } else {
            print("  ❌ not readable (needs Full Disk Access)")
            allPass = false
        }
    } else {
        allPass = false
    }
    print("")

    // 4. Madrid processes
    print("madrid processes")
    let procs = checkMadridProcesses()
    if procs.isEmpty {
        print("  ⚠️  no madrid processes running")
    } else {
        for (name, pid, port) in procs {
            print("  ✅ \(name) (pid \(pid)\(port != nil ? ", port \(port!)" : ""))")
        }
    }
    print("")

    // 5. Bridge
    print("Bridge")
    let bridgeOK = checkBridge()
    print("  \(bridgeOK ? "✅" : "—") resident: \(bridgeOK)")
    let csrOK = checkCSRUtil()
    print("  \(csrOK ? "✅" : "—") csrutil debug exemption: \(csrOK)")
    print("")

    // 6. Contacts
    print("Contacts")
    let contactsAuth = CNContactStore.authorizationStatus(for: .contacts)
    let contactsOK = contactsAuth == .authorized || contactsAuth == .notDetermined
    print("  \(contactsOK ? "✅" : "⚠️") access: \(authString(contactsAuth))")
    print("")

    // Summary
    print(String(repeating: "-", count: 50))
    if allPass {
        print("✅ All checks passed. madrid is ready.")
    } else {
        print("⚠️  Some checks failed. Run `madridctl permissions` for help.")
    }
}

// MARK: - Permissions

func permissions() {
    print("madrid permissions")
    print(String(repeating: "=", count: 50))
    print("")

    let perms = checkPermissions()
    var allOK = true

    for (name, granted) in perms {
        let icon = granted ? "✅" : "❌"
        print("\(icon) \(name)")
        if !granted {
            allOK = false
            switch name {
            case "Full Disk Access":
                print("   → System Settings → Privacy & Security → Full Disk Access")
                print("     Add your terminal app (Terminal, iTerm, etc.)")
            case "Automation → Messages":
                print("   → System Settings → Privacy & Security → Automation")
                print("     Allow your terminal to control Messages")
                print("     Or run any madrid command that sends; macOS will prompt")
            case "Automation → System Events":
                print("   → System Settings → Privacy & Security → Automation")
                print("     Allow your terminal to control System Events")
                print("     Needed for the pasteboard rich-text channel")
            default:
                break
            }
            print("")
        }
    }

    if allOK {
        print("✅ All permissions granted.")
    } else {
        print("\nAfter granting permissions, restart your terminal and run `madridctl doctor` again.")
    }
}

func checkPermissions() -> [(String, Bool)] {
    var results: [(String, Bool)] = []

    // Full Disk Access (test by reading chat.db)
    let dbPath = NSHomeDirectory() + "/Library/Messages/chat.db"
    let fda = FileManager.default.fileExists(atPath: dbPath) && canReadDB(atPath: dbPath)
    results.append(("Full Disk Access", fda))

    // Automation → Messages (test osascript)
    let msgOK = testAppleScript("tell application \"Messages\" to get version")
    results.append(("Automation → Messages", msgOK))

    // Automation → System Events (test)
    let seOK = testAppleScript("tell application \"System Events\" to get name of first process")
    results.append(("Automation → System Events", seOK))

    return results
}

func testAppleScript(_ source: String, timeout: TimeInterval = 5) -> Bool {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    proc.arguments = ["-e", source]
    proc.standardOutput = Pipe()
    proc.standardError = Pipe()
    do {
        try proc.run()
        // Timeout: kill after N seconds
        let queue = DispatchQueue.global()
        let timer = queue.asyncAfter(deadline: .now() + timeout) {
            if proc.isRunning { proc.terminate() }
        }
        proc.waitUntilExit()
        _ = timer  // keep reference
        return proc.terminationStatus == 0
    } catch { return false }
}

func canReadDB(atPath path: String) -> Bool {
    // Try opening SQLite read-only
    var db: OpaquePointer?
    let uri = "file:\(path)?mode=ro"
    guard sqlite3_open_v2(uri, &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
        sqlite3_close(db)
        return false
    }
    // Test a simple query
    var stmt: OpaquePointer?
    defer { sqlite3_finalize(stmt); sqlite3_close(db) }
    return sqlite3_prepare_v2(db, "SELECT count(*) FROM message", -1, &stmt, nil) == SQLITE_OK
}

func messageCount(atPath path: String) -> Int {
    var db: OpaquePointer?
    guard sqlite3_open_v2("file:\(path)?mode=ro", &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
        sqlite3_close(db)
        return 0
    }
    defer { sqlite3_close(db) }
    var stmt: OpaquePointer?
    guard sqlite3_prepare_v2(db, "SELECT count(*) FROM message", -1, &stmt, nil) == SQLITE_OK else {
        sqlite3_finalize(stmt)
        return 0
    }
    defer { sqlite3_finalize(stmt) }
    if sqlite3_step(stmt) == SQLITE_ROW {
        return Int(sqlite3_column_int64(stmt, 0))
    }
    return 0
}

// MARK: - Accounts

func accounts() {
    print("madrid accounts")
    print(String(repeating: "=", count: 50))
    print("")

    // Get accounts via AppleScript
    let script = """
    tell application "Messages"
        set out to ""
        repeat with a in accounts
            set out to out & (id of a) & "|" & (description of a) & "|" & (connection status of a as string) & linefeed
        end repeat
        return out
    end tell
    """

    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    proc.arguments = ["-e", script]
    let pipe = Pipe()
    proc.standardOutput = pipe
    proc.standardError = Pipe()

    do {
        try proc.run()
        proc.waitUntilExit()
        if proc.terminationStatus != 0 { return }

        if proc.terminationStatus == 0 {
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            let output = String(data: data, encoding: .utf8) ?? ""
            let lines = output.components(separatedBy: .newlines).filter { !$0.isEmpty }

            if lines.isEmpty {
                print("No accounts found (Messages may not be signed in)")
                return
            }

            print("Found \(lines.count) account(s):")
            print("")

            for (i, line) in lines.enumerated() {
                let parts = line.components(separatedBy: "|")
                let id = parts.count > 0 ? parts[0] : "?"
                let desc = parts.count > 1 ? parts[1] : "?"
                let status = parts.count > 2 ? parts[2] : "?"

                let statusIcon: String
                switch status.lowercased() {
                case "connected": statusIcon = "🟢"
                case "connecting": statusIcon = "🟡"
                case "disconnecting": statusIcon = "🟠"
                default: statusIcon = "🔴"
                }

                print("  \(i + 1). \(statusIcon) \(desc)")
                print("     id: \(id)")
                print("     status: \(status)")
                print("")
            }
        } else {
            print("❌ Cannot query accounts (needs Automation permission)")
            print("   Run `madridctl permissions` for help")
        }
    } catch {
        print("❌ Failed to run AppleScript: \(error)")
    }
}

// MARK: - Health Watch

func watchHealth() {
    print("madrid watch — monitoring (Ctrl+C to stop)")
    print(String(repeating: "=", count: 50))

    let timer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { _ in
        let now = DateFormatter()
        now.dateFormat = "HH:mm:ss"
        let ts = now.string(from: Date())

        let procs = checkMadridProcesses()
        let dbOK = canReadDB(atPath: NSHomeDirectory() + "/Library/Messages/chat.db")
        let bridgeOK = checkBridge()

        var status = "[\(ts)] "
        status += dbOK ? "db:✅ " : "db:❌ "
        status += bridgeOK ? "bridge:✅ " : "bridge:— "
        status += "procs: \(procs.count) "

        if procs.isEmpty {
            status += "(none running)"
        } else {
            status += "[" + procs.map { $0.name }.joined(separator: ",") + "]"
        }

        print(status)
    }

    RunLoop.main.run()
}

func checkMadridProcesses() -> [(name: String, pid: Int32, port: Int?)] {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/bin/ps")
    proc.arguments = ["aux"]
    let pipe = Pipe()
    proc.standardOutput = pipe
    proc.standardError = Pipe()

    // Use pgrep instead
    let pgrep = Process()
    pgrep.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
    pgrep.arguments = ["-fl", "madrid"]
    let ppipe = Pipe()
    pgrep.standardOutput = ppipe
    pgrep.standardError = Pipe()

    // Fallback: just check if serve port is open
    var results: [(String, Int32, Int?)] = []

    // Check if madrid serve is on default port
    if isPortOpen(8787) {
        results.append(("serve", 0, 8787))
    }
    if isPortOpen(8788) {
        results.append(("serve", 0, 8788))
    }

    // Check bridge socket
    if FileManager.default.fileExists(atPath: "/tmp/madrid-bridge.sock") {
        results.append(("bridge", 0, nil))
    }

    return results
}

func isPortOpen(_ port: Int) -> Bool {
    // Simple TCP connect check
    let sock = socket(AF_INET, SOCK_STREAM, 0)
    guard sock >= 0 else { return false }
    defer { close(sock) }

    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = UInt16(port).bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")

    let result = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(sock, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    return result == 0
}

func checkBridge() -> Bool {
    return FileManager.default.fileExists(atPath: "/tmp/madrid-bridge.sock")
}

func checkCSRUtil() -> Bool {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/csrutil")
    proc.arguments = ["status"]
    let pipe = Pipe()
    proc.standardOutput = pipe
    proc.standardError = Pipe()
    do {
        try proc.run()
        proc.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let output = String(data: data, encoding: .utf8) ?? ""
        return output.contains("Debugging Restrictions: disabled")
    } catch { return false }
}

// MARK: - Setup

func setup() {
    print("madrid setup wizard")
    print(String(repeating: "=", count: 50))
    print("")

    // Step 1: Permissions
    print("Step 1: Permissions")
    print("  Grant Full Disk Access to your terminal:")
    print("  → System Settings → Privacy & Security → Full Disk Access")
    print("  → Add Terminal (or iTerm, etc.)")
    print("")
    print("  Press Enter after granting... ", terminator: "")
    _ = readLine()
    print("")

    // Step 2: Test database access
    print("Step 2: Testing database access...")
    let dbOK = canReadDB(atPath: NSHomeDirectory() + "/Library/Messages/chat.db")
    if dbOK {
        let count = messageCount(atPath: NSHomeDirectory() + "/Library/Messages/chat.db")
        print("  ✅ chat.db readable (\(count) messages)")
    } else {
        print("  ❌ Cannot read chat.db. Grant Full Disk Access and retry.")
        return
    }
    print("")

    // Step 3: Messages automation
    print("Step 3: Messages automation")
    print("  madrid needs Automation permission to send messages.")
    print("  Testing (macOS will prompt if needed)...")
    let msgOK = testAppleScript("tell application \"Messages\" to get version")
    print("  \(msgOK ? "✅" : "❌") Messages automation: \(msgOK ? "granted" : "not granted")")
    print("")

    // Step 4: Start madrid
    print("Step 4: Start madrid")
    print("  Run this to start the HTTP API:")
    print("  $ python3 -m madrid serve --port 8787 --watch")
    print("")
    print("  For Hermes integration:")
    print("  $ export MADRID_URL=http://127.0.0.1:8787")
    print("  $ hermes gateway setup")
    print("")

    print("Setup complete! Run `madridctl doctor` to verify.")
}

// MARK: - Bridge

func bridgeStatus() {
    print("madrid bridge")
    print(String(repeating: "=", count: 50))
    print("")

    let resident = checkBridge()
    let csrOK = checkCSRUtil()

    print("  Resident: \(resident ? "✅ running" : "— not installed")")
    print("  csrutil debug exemption: \(csrOK ? "✅ enabled" : "— not enabled")")
    print("")

    if !csrOK {
        print("To enable the bridge:")
        print("  1. Reboot into recovery (hold power on Apple Silicon)")
        print("  2. Open Terminal from Utilities")
        print("  3. Run: csrutil enable --without debug")
        print("  4. Reboot")
        print("  5. Run: sudo DevToolsSecurity -enable")
        print("  6. Run: MADRID_ALLOW_REAL_SEND=1 python3 -m madrid bridge install")
        print("")
        print("Note: All other madrid features work WITHOUT the bridge.")
    } else if !resident {
        print("csrutil is enabled but bridge is not installed.")
        print("Run: MADRID_ALLOW_REAL_SEND=1 python3 -m madrid bridge install")
    } else {
        print("Bridge is running. Test with: python3 -m madrid bridge ping")
    }
}

// MARK: - Helpers

func authString(_ status: CNAuthorizationStatus) -> String {
    switch status {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "not determined"
    @unknown default: return "unknown"
    }
}

func printHelp() {
    print("""
    madridctl — management tool for the madrid iMessage bridge

    Usage: madridctl <command>

    Commands:
      doctor        Full environment check
      permissions   Check macOS permissions and guide setup
      accounts      List iMessage accounts and connection status
      watch         Monitor madrid health (Ctrl+C to stop)
      setup         Interactive setup wizard
      bridge        Bridge status and management

    Options:
      --help, -h    Show this help
    """)
}
