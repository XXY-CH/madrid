import SwiftUI
import AppKit
import Contacts

// madrid UI — menu bar app for managing the madrid iMessage bridge.
//
// Build: cd tools/madridui && swift build
// Run:   ./.build/debug/madridui
//
// Features:
//   • Menu bar icon with live status dot (green/red)
//   • Doctor panel: permissions, database, bridge, contacts
//   • Accounts list with connection status
//   • madrid process monitor (serve/watch/bridge)
//   • One-click actions: start serve, open permissions, install bridge

@main
struct MadridApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        Settings {
            EmptyView()
        }
    }
}

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var panel: NSPanel!
    var viewModel: MadridViewModel!

    func applicationDidFinishLaunching(_ notification: Notification) {
        viewModel = MadridViewModel()
        setupMenuBar()
        setupPanel()
    }

    func setupMenuBar() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)

        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "message.fill",
                                    accessibilityDescription: "madrid")
        }

        statusItem.menu = buildMenu()
    }

    func buildMenu() -> NSMenu {
        let menu = NSMenu()

        // Status line
        let statusItem = NSMenuItem(
            title: viewModel.statusLine,
            action: nil, keyEquivalent: "")
        statusItem.isEnabled = false
        menu.addItem(statusItem)
        menu.addItem(.separator())

        // Actions
        menu.addItem(NSMenuItem(title: "Show Panel",
                                 action: #selector(showPanel),
                                 keyEquivalent: "p"))
        menu.addItem(NSMenuItem(title: "Start madrid serve",
                                 action: #selector(startServe),
                                 keyEquivalent: "s"))
        menu.addItem(NSMenuItem(title: "Run Doctor",
                                 action: #selector(runDoctor),
                                 keyEquivalent: "d"))
        menu.addItem(.separator())

        // Quick status
        let dbItem = NSMenuItem(title: "  Database: \(viewModel.dbStatus)",
                                action: nil, keyEquivalent: "")
        dbItem.isEnabled = false
        menu.addItem(dbItem)

        let bridgeItem = NSMenuItem(title: "  Bridge: \(viewModel.bridgeStatus)",
                                    action: nil, keyEquivalent: "")
        bridgeItem.isEnabled = false
        menu.addItem(bridgeItem)

        let permsItem = NSMenuItem(title: "  Permissions: \(viewModel.permsSummary)",
                                   action: #selector(openPermissions),
                                   keyEquivalent: "")
        menu.addItem(permsItem)
        menu.addItem(.separator())

        menu.addItem(NSMenuItem(title: "Quit",
                                 action: #selector(NSApplication.terminate(_:)),
                                 keyEquivalent: "q"))

        return menu
    }

    func setupPanel() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 380, height: 520),
            styleMask: [.titled, .closable, .miniaturizable, .nonactivatingPanel],
            backing: .buffered, defer: false)
        panel.title = "madrid"
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.contentView = NSHostingView(rootView: MadridPanelView(viewModel: viewModel))
        panel.center()
    }

    @objc func showPanel() {
        viewModel.refresh()
        panel?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func startServe() {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        task.arguments = ["-a", "Terminal", "--args"]
        try? task.run()

        // Better: launch in background via shell
        let shell = Process()
        shell.executableURL = URL(fileURLWithPath: "/bin/zsh")
        shell.arguments = ["-c",
            "nohup python3 -m madrid serve --port 8787 --watch > /tmp/madrid-serve.log 2>&1 &"]
        try? shell.run()

        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
            self.viewModel.refresh()
            self.statusItem.menu = self.buildMenu()
        }
    }

    @objc func runDoctor() {
        viewModel.refresh()
        showPanel()
    }

    @objc func openPermissions() {
        let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!
        NSWorkspace.shared.open(url)
    }
}

// MARK: - View Model

class MadridViewModel: ObservableObject {
    @Published var dbOK = false
    @Published var dbMessageCount = 0
    @Published var bridgeResident = false
    @Published var csrDebug = false
    @Published var fdaGranted = false
    @Published var messagesAutomation = false
    @Published var systemEventsAutomation = false
    @Published var contactsAuth: String = "unknown"
    @Published var accounts: [(desc: String, status: String)] = []
    @Published var serveRunning = false
    @Published var lastRefresh = Date()

    var statusLine: String {
        if dbOK && serveRunning { return "● madrid — running" }
        if dbOK { return "● madrid — db OK, serve stopped" }
        return "● madrid — needs setup"
    }

    var dbStatus: String { dbOK ? "✅ readable (\(dbMessageCount) msgs)" : "❌ not accessible" }
    var bridgeStatus: String {
        if bridgeResident { return "✅ resident" }
        if csrDebug { return "⚡ csrutil ready, not installed" }
        return "— not enabled"
    }
    var permsSummary: String {
        var granted = 0
        var total = 3
        if fdaGranted { granted += 1 }
        if messagesAutomation { granted += 1 }
        if systemEventsAutomation { granted += 1 }
        return "\(granted)/\(total)"
    }

    init() {
        refresh()
    }

    func refresh() {
        DispatchQueue.global(qos: .userInitiated).async {
            let db = self.checkDB()
            let bridge = self.checkBridge()
            let csr = self.checkCSR()
            let perms = self.checkPermissions()
            let accts = self.checkAccounts()
            let serve = self.checkServe()

            DispatchQueue.main.async {
                self.dbOK = db.0
                self.dbMessageCount = db.1
                self.bridgeResident = bridge
                self.csrDebug = csr
                self.fdaGranted = perms.0
                self.messagesAutomation = perms.1
                self.systemEventsAutomation = perms.2
                self.accounts = accts
                self.serveRunning = serve
                self.lastRefresh = Date()
            }
        }
    }

    private func checkDB() -> (Bool, Int) {
        let path = NSHomeDirectory() + "/Library/Messages/chat.db"
        guard FileManager.default.fileExists(atPath: path) else { return (false, 0) }
        var db: OpaquePointer?
        guard sqlite3_open_v2("file:\(path)?mode=ro", &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return (false, 0)
        }
        defer { sqlite3_close(db) }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, "SELECT count(*) FROM message", -1, &stmt, nil) == SQLITE_OK else {
            sqlite3_finalize(stmt)
            return (false, 0)
        }
        defer { sqlite3_finalize(stmt) }
        if sqlite3_step(stmt) == SQLITE_ROW {
            return (true, Int(sqlite3_column_int64(stmt, 0)))
        }
        return (false, 0)
    }

    private func checkBridge() -> Bool {
        FileManager.default.fileExists(atPath: "/tmp/madrid-bridge.sock")
    }

    private func checkCSR() -> Bool {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/csrutil")
        proc.arguments = ["status"]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = Pipe()
        try? proc.run()
        proc.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8)?.contains("Debugging Restrictions: disabled") ?? false
    }

    private func checkPermissions() -> (Bool, Bool, Bool) {
        let dbPath = NSHomeDirectory() + "/Library/Messages/chat.db"
        let fda = FileManager.default.fileExists(atPath: dbPath) && canReadSQLite(dbPath)

        let msgOK = testOSA("tell application \"Messages\" to get version")
        let seOK = testOSA("tell application \"System Events\" to get name of first process")

        return (fda, msgOK, seOK)
    }

    private func canReadSQLite(_ path: String) -> Bool {
        var db: OpaquePointer?
        guard sqlite3_open_v2("file:\(path)?mode=ro", &db, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            sqlite3_close(db)
            return false
        }
        var stmt: OpaquePointer?
        let ok = sqlite3_prepare_v2(db, "SELECT 1", -1, &stmt, nil) == SQLITE_OK
        sqlite3_finalize(stmt)
        sqlite3_close(db)
        return ok
    }

    private func testOSA(_ source: String) -> Bool {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        proc.arguments = ["-e", source]
        proc.standardOutput = Pipe()
        proc.standardError = Pipe()
        do {
            try proc.run()
            proc.waitUntilExit()
            return proc.terminationStatus == 0
        } catch { return false }
    }

    private func checkAccounts() -> [(String, String)] {
        let script = """
        tell application "Messages"
            set out to ""
            repeat with a in accounts
                set out to out & (description of a) & "|" & (connection status of a as string) & linefeed
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
            guard proc.terminationStatus == 0 else { return [] }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            let output = String(data: data, encoding: .utf8) ?? ""
            return output.components(separatedBy: .newlines)
                .filter { !$0.isEmpty }
                .map { line in
                    let parts = line.components(separatedBy: "|")
                    return (parts.first ?? "?", parts.count > 1 ? parts[1] : "?")
                }
        } catch { return [] }
    }

    private func checkServe() -> Bool {
        // Check if port 8787 is open
        let sock = socket(AF_INET, SOCK_STREAM, 0)
        guard sock >= 0 else { return false }
        defer { close(sock) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = UInt16(8787).bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let result = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(sock, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return result == 0
    }
}

// MARK: - Panel View

struct MadridPanelView: View {
    @ObservedObject var viewModel: MadridViewModel

    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Image(systemName: "message.fill")
                    .font(.system(size: 28))
                    .foregroundStyle(.blue)
                VStack(alignment: .leading, spacing: 2) {
                    Text("madrid")
                        .font(.system(size: 18, weight: .bold))
                    Text("iMessage bridge for agents")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Refresh") { viewModel.refresh() }
                    .buttonStyle(.borderless)
            }
            .padding()
            .background(Color(nsColor: .windowBackgroundColor))

            Divider()

            ScrollView {
                VStack(spacing: 16) {
                    // Permissions card
                    GroupBox("Permissions") {
                        VStack(alignment: .leading, spacing: 8) {
                            PermRow(icon: "internaldrive", name: "Full Disk Access",
                                    granted: viewModel.fdaGranted)
                            PermRow(icon: "applescript", name: "Messages Automation",
                                    granted: viewModel.messagesAutomation)
                            PermRow(icon: "keyboard", name: "System Events",
                                    granted: viewModel.systemEventsAutomation)
                            PermRow(icon: "person.crop.circle", name: "Contacts",
                                    granted: viewModel.contactsAuth == "authorized")
                        }
                        .padding(.vertical, 4)
                    }

                    // Database card
                    GroupBox("Database") {
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                Label(
                                    viewModel.dbOK ? "chat.db readable" : "chat.db NOT accessible",
                                    systemImage: viewModel.dbOK ? "checkmark.circle.fill" : "xmark.circle.fill")
                                    .foregroundStyle(viewModel.dbOK ? .green : .red)
                                if viewModel.dbOK {
                                    Text("\(viewModel.dbMessageCount) messages")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                        }
                        .padding(.vertical, 4)
                    }

                    // Bridge card
                    GroupBox("Bridge (optional)") {
                        VStack(alignment: .leading, spacing: 6) {
                            Label(
                                viewModel.bridgeResident ? "Resident and running" : "Not installed",
                                systemImage: viewModel.bridgeResident ? "bolt.fill" : "bolt.slash")
                                .foregroundStyle(viewModel.bridgeResident ? .yellow : .secondary)

                            if !viewModel.csrDebug {
                                Text("Enable: csrutil enable --without debug")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }

                            HStack {
                                Text("csrutil debug: ")
                                    .font(.caption)
                                Image(systemName: viewModel.csrDebug ? "checkmark.circle.fill" : "circle")
                                    .foregroundStyle(viewModel.csrDebug ? .green : .secondary)
                            }
                        }
                        .padding(.vertical, 4)
                    }

                    // Accounts card
                    GroupBox("Accounts") {
                        if viewModel.accounts.isEmpty {
                            Text("No accounts found")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        } else {
                            ForEach(viewModel.accounts.indices, id: \.self) { i in
                                let acct = viewModel.accounts[i]
                                HStack {
                                    Circle()
                                        .fill(colorForStatus(acct.status))
                                        .frame(width: 8, height: 8)
                                    Text(acct.desc)
                                        .font(.system(size: 12, design: .monospaced))
                                        .lineLimit(1)
                                    Spacer()
                                    Text(acct.status)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    }

                    // Processes card
                    GroupBox("Processes") {
                        HStack {
                            Label(
                                viewModel.serveRunning ? "serve running (port 8787)" : "serve not running",
                                systemImage: viewModel.serveRunning ? "play.circle.fill" : "stop.circle")
                                .foregroundStyle(viewModel.serveRunning ? .green : .secondary)
                            Spacer()
                        }
                        .padding(.vertical, 4)
                    }

                    // Actions
                    HStack(spacing: 12) {
                        Button("Start serve") {
                            startServe()
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(viewModel.serveRunning)

                        Button("Open Settings") {
                            NSWorkspace.shared.open(
                                URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!)
                        }
                        .buttonStyle(.bordered)

                        Spacer()

                        Text("Updated \(viewModel.lastRefresh.formatted(.relative(presentation: .named)))")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
                .padding()
            }
        }
        .frame(minWidth: 360, minHeight: 480)
    }

    private func colorForStatus(_ status: String) -> Color {
        switch status.lowercased() {
        case "connected": return .green
        case "connecting": return .yellow
        case "disconnecting": return .orange
        default: return .red
        }
    }

    private func startServe() {
        let shell = Process()
        shell.executableURL = URL(fileURLWithPath: "/bin/zsh")
        shell.arguments = ["-c",
            "nohup python3 -m madrid serve --port 8787 --watch > /tmp/madrid-serve.log 2>&1 &"]
        try? shell.run()
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
            viewModel.refresh()
        }
    }
}

struct PermRow: View {
    let icon: String
    let name: String
    let granted: Bool

    var body: some View {
        HStack {
            Image(systemName: icon)
                .frame(width: 20)
                .foregroundStyle(granted ? .green : .secondary)
            Text(name)
                .font(.system(size: 12))
            Spacer()
            Image(systemName: granted ? "checkmark.circle.fill" : "xmark.circle")
                .foregroundStyle(granted ? .green : .red)
        }
    }
}

import SQLite3
