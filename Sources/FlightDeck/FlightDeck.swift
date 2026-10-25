import SwiftUI
import AppKit

struct Project: Codable, Identifiable { let id, name, root, script, buildRoot: String; let families: [String] }
struct Job: Codable, Identifiable {
    let id, project, root, mode, status, stage, created, updated: String
    let commit, log, error: String
    var output: String?
    var monitorError: String?
    var active: Bool { ["queued", "running", "cancelling", "interrupted"].contains(status) }
}
struct Lease: Codable { let device, token, project, worktree, owner, created, heartbeat, profile: String }
struct Seed: Codable { let project, profile, revision, updated: String }
struct Device: Codable, Identifiable { let id, name, state, runtime, family: String; let lease: Lease?; let seed: Seed? }
struct StorageRow: Codable, Identifiable { let id, project, kind, path, reason: String; let bytes: Int64?; let eligible: Bool }
struct StorageScan: Codable { let id, note: String; let rows: [StorageRow] }
struct Check: Codable, Identifiable { var id: String { name }; let name, detail: String; let ok: Bool }
struct Preflight: Codable { let checks: [Check]; let ready: Bool; let note: String }
struct Failure: Codable { let error: String }

@MainActor final class Model: ObservableObject {
    @Published var projects: [Project] = []
    @Published var jobs: [Job] = []
    @Published var devices: [Device] = []
    @Published var scan: StorageScan?
    @Published var selectedProject = "patchbridge"
    @Published var section = "Releases"
    @Published var selectedJob: String?
    @Published var detail: Job?
    @Published var preflight: Preflight?
    @Published var worktree = ""
    @Published var skipTests = false
    @Published var build = ""
    @Published var busy = false
    @Published var error: String?
    @Published var notice: String?
    var currentProject: Project? { projects.first { $0.id == selectedProject } }
    var visibleJobs: [Job] { jobs.filter { $0.project == selectedProject } }
    var root: String { worktree.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? currentProject?.root ?? "" : worktree }
    var rootArgs: [String] { worktree.isEmpty ? [] : ["--worktree", worktree] }
    let engine: String
    let python: String

    init() {
        let resource = Bundle.main.resourceURL?.appendingPathComponent("engine/flightdeck.py").path
        engine = ProcessInfo.processInfo.environment["FLIGHTDECK_ENGINE"] ?? (resource.flatMap { FileManager.default.fileExists(atPath: $0) ? $0 : nil } ?? URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("engine/flightdeck.py").path)
        python = ProcessInfo.processInfo.environment["FLIGHTDECK_PYTHON"] ?? (["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"].first { FileManager.default.isExecutableFile(atPath: $0) } ?? "/usr/bin/python3")
    }

    nonisolated static func execute(python: String, engine: String, args: [String]) throws -> Data {
        let p = Process(); p.executableURL = URL(fileURLWithPath: python); p.arguments = ["-B", engine] + args
        let pipe = Pipe(); p.standardOutput = pipe
        let errors = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        FileManager.default.createFile(atPath: errors.path, contents: nil)
        let err = try FileHandle(forWritingTo: errors); p.standardError = err
        defer { try? err.close(); try? FileManager.default.removeItem(at: errors) }
        try p.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
        if p.terminationStatus != 0 {
            let message = (try? JSONDecoder().decode(Failure.self, from: data).error) ?? ((try? String(contentsOf: errors, encoding: .utf8)) ?? "Runner failed.")
            throw NSError(domain: "FlightDeck", code: Int(p.terminationStatus), userInfo: [NSLocalizedDescriptionKey: message])
        }
        return data
    }
    func call<T: Decodable>(_ args: [String], as type: T.Type) async throws -> T {
        let py = python, en = engine
        let data = try await Task.detached { try Self.execute(python: py, engine: en, args: args) }.value
        return try JSONDecoder().decode(type, from: data)
    }
    func command(_ args: [String]) async throws {
        let py = python, en = engine
        _ = try await Task.detached { try Self.execute(python: py, engine: en, args: args) }.value
    }
    func perform(_ action: @escaping () async throws -> Void) {
        guard !busy else { return }; busy = true; error = nil; notice = nil
        Task { do { try await action() } catch { self.error = error.localizedDescription }; busy = false }
    }
    func load() async {
        do { projects = try await call(["projects"], as: [Project].self); try await refresh() }
        catch { self.error = error.localizedDescription }
    }
    func refresh() async throws {
        jobs = try await call(["jobs"], as: [Job].self)
        if let key = selectedJob {
            let fetched = try await call(["detail", key], as: Job.self)
            if selectedJob == key && selectedProject == fetched.project { detail = fetched }
        }
    }
    func loadDevices() { perform { self.devices = try await self.call(["devices"], as: [Device].self) } }
    func checkReadiness() {
        let project = selectedProject, worktree = root
        perform {
            let result = try await self.call(["preflight", project] + self.rootArgs, as: Preflight.self)
            if self.selectedProject == project && self.root == worktree { self.preflight = result }
        }
    }
    func start(_ mode: String) {
        perform {
            var args = ["start", self.selectedProject, "--mode", mode] + self.rootArgs
            if self.skipTests { args.append("--skip-tests") }
            if !self.build.isEmpty { args += ["--build", self.build] }
            let job = try await self.call(args, as: Job.self)
            if self.selectedProject == job.project { self.selectedJob = job.id; self.detail = job }
            try await self.refresh()
        }
    }
    func select(_ id: String?) { selectedJob = id; detail = nil; Task { do { try await refresh() } catch { self.error = error.localizedDescription } } }
    func lease(_ d: Device) {
        perform {
            if let lease = d.lease { try await self.command(["lease", "release", "--device", d.id, "--token", lease.token]) }
            else { try await self.command(["lease", "acquire", "--device", d.id, "--project", self.selectedProject, "--owner", "manual"] + self.rootArgs) }
            self.devices = try await self.call(["devices"], as: [Device].self)
        }
    }
    func deviceAction(_ d: Device, _ action: String) {
        guard let lease = d.lease else { return }
        perform {
            try await self.command(["device-action", action, "--device", d.id, "--token", lease.token])
            self.devices = try await self.call(["devices"], as: [Device].self)
        }
    }
    func seed(_ d: Device) {
        guard let lease = d.lease else { return }
        perform {
            try await self.command(["seed", lease.project, "--worktree", lease.worktree, "--device", d.id, "--token", lease.token])
            self.notice = "Solina data is ready. Existing data was checked by the idempotent installer."
            self.devices = try await self.call(["devices"], as: [Device].self)
        }
    }
    func storage() { perform { self.scan = try await self.call(["storage"], as: StorageScan.self) } }
    func clean(_ row: StorageRow) {
        guard let scan else { return }
        perform {
            try await self.command(["cleanup", scan.id, row.id, "--confirm"])
            self.notice = "Removed release intermediates. Archives and dSYMs were retained."
            self.scan = nil
        }
    }
    func open(_ path: String) { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
    func openDeviceHub() {
        let options = ["/Applications/Xcode.app/Contents/Applications/DeviceHub.app", "/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app"]
        if let path = options.first(where: { FileManager.default.fileExists(atPath: $0) }) { open(path) }
        else { error = "Open Device Hub from Xcode → Open Developer Tool. FlightDeck could not find it in the default Xcode installation." }
    }
}

@main struct FlightDeckApp: App {
    @StateObject private var model = Model()
    var body: some Scene {
        WindowGroup {
            ContentView().environmentObject(model).frame(minWidth: 980, minHeight: 660)
                .task { await model.load() }
        }.defaultSize(width: 1180, height: 780)
        .commands {
            CommandGroup(after: .newItem) {
                Button("Refresh") { model.perform { try await model.refresh() } }.keyboardShortcut("r")
                Button("Open Device Hub") { model.openDeviceHub() }
            }
        }
    }
}

struct ContentView: View {
    @EnvironmentObject var m: Model
    var body: some View {
        NavigationSplitView {
            List {
                Section("Workspace") {
                    ForEach(["Releases", "Devices", "Storage"], id: \.self) { section in
                        Button { m.section = section; if section == "Devices" { m.loadDevices() } } label: {
                            Label(section, systemImage: section == "Releases" ? "paperplane" : section == "Devices" ? "iphone.and.ipad" : "internaldrive")
                                .foregroundStyle(m.section == section ? Color.accentColor : .primary)
                        }.buttonStyle(.plain).padding(.vertical, 5)
                    }
                }
                Section("Projects") {
                    ForEach(m.projects) { p in
                        Button { m.selectedProject = p.id; m.worktree = ""; m.preflight = nil; m.select(nil) } label: {
                            HStack { Text(p.name); Spacer(); if m.selectedProject == p.id { Image(systemName: "checkmark").foregroundStyle(Color.accentColor) } }
                        }.buttonStyle(.plain).padding(.vertical, 5).disabled(m.busy)
                    }
                }
            }.listStyle(.sidebar).navigationTitle("FlightDeck")
            VStack(alignment: .leading, spacing: 5) {
                Text("Local workspace").font(.caption.weight(.semibold))
                Text("Jobs continue when this window closes.").font(.caption).foregroundStyle(.secondary)
            }.padding()
        } detail: {
            VStack(spacing: 0) {
                if let error = m.error {
                    HStack(alignment: .top) {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                        Text(error).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                        Button("Dismiss") { m.error = nil }
                    }.padding().background(Color.orange.opacity(0.09))
                    Divider()
                }
                if let notice = m.notice { HStack { Text(notice); Spacer(); Button("Dismiss") { m.notice = nil } }.padding().background(Color.green.opacity(0.08)) }
                if m.section == "Releases" { ReleasesView() }
                else if m.section == "Devices" { DevicesView() }
                else { StorageView() }
            }
            .navigationTitle(m.section)
            .toolbar {
                if m.busy { ProgressView().controlSize(.small).help("Working…") }
                Button { m.openDeviceHub() } label: { Label("Device Hub", systemImage: "iphone.and.ipad") }
            }
        }
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3))
                if !m.busy { do { try await m.refresh() } catch { m.error = error.localizedDescription } }
            }
        }
    }
}

struct ReleasesView: View {
    @EnvironmentObject var m: Model
    @State private var confirmingUpload = false
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .firstTextBaseline) {
                    Text(m.currentProject?.name ?? "Select a project").font(.title2.weight(.semibold))
                    Spacer()
                    Button("Check Readiness") { m.checkReadiness() }.disabled(m.busy)
                    Button("Build Archive") { m.start("archive") }.disabled(m.busy || m.jobs.contains { $0.active })
                    Button("Release to TestFlight…") { confirmingUpload = true }.buttonStyle(.borderedProminent).disabled(m.busy || m.jobs.contains { $0.active })
                }
                LabeledContent("Worktree") { TextField(m.currentProject?.root ?? "Repository path", text: $m.worktree).textFieldStyle(.roundedBorder).onChange(of: m.worktree) { _, _ in m.preflight = nil } }
                HStack {
                    TextField("Build number (automatic)", text: $m.build).textFieldStyle(.roundedBorder).frame(width: 230)
                    Toggle("Skip tests", isOn: $m.skipTests).help("Use only when this exact commit has already passed the required tests.")
                    Spacer()
                    Text("Free simulators are leased automatically").font(.caption).foregroundStyle(.secondary)
                }
                if let p = m.preflight {
                    DisclosureGroup(p.ready ? "Ready for project release checks" : "Readiness checks need attention") {
                        VStack(alignment: .leading, spacing: 7) {
                            ForEach(p.checks) { check in
                                HStack(alignment: .top) {
                                    Image(systemName: check.ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill").foregroundStyle(check.ok ? .green : .orange)
                                    Text(check.name).frame(width: 110, alignment: .leading)
                                    Text(check.detail).foregroundStyle(.secondary).textSelection(.enabled)
                                }.font(.caption)
                            }
                            Text(p.note).font(.caption).foregroundStyle(.secondary)
                        }.padding(.top, 8)
                    }
                }
            }.padding(22)
            Divider()
            HSplitView {
                VStack(alignment: .leading, spacing: 0) {
                    Text("Recent runs").font(.caption.weight(.semibold)).foregroundStyle(.secondary).padding()
                    if m.visibleJobs.isEmpty {
                        ContentUnavailableView("No releases yet", systemImage: "paperplane", description: Text("Check readiness, then build a local archive to verify the workflow."))
                    } else {
                        List(m.visibleJobs, selection: Binding(get: { m.selectedJob }, set: { m.select($0) })) { job in
                            VStack(alignment: .leading, spacing: 7) {
                                HStack { Image(systemName: job.active ? "clock" : job.status == "failed" ? "exclamationmark.circle" : "checkmark.circle"); Text(job.mode == "testflight" ? "TestFlight" : "Local archive").fontWeight(.medium) }
                                Text(job.stage).font(.caption).foregroundStyle(.secondary)
                                Text(String(job.commit.prefix(8)) + " · " + String(job.created.prefix(16)).replacingOccurrences(of: "T", with: " ")).font(.caption2).foregroundStyle(.secondary)
                            }.padding(.vertical, 5).tag(job.id)
                        }
                    }
                }.frame(minWidth: 230, idealWidth: 270, maxWidth: 330)
                if let job = m.detail { JobView(job: job) }
                else { ContentUnavailableView("Release details", systemImage: "list.bullet.rectangle", description: Text("Select a run to see its stage, logs and source commit.")).frame(maxWidth: .infinity, maxHeight: .infinity) }
            }
        }
        .confirmationDialog("Release \(m.currentProject?.name ?? "app") to TestFlight?", isPresented: $confirmingUpload, titleVisibility: .visible) {
            Button(m.skipTests ? "Build and Upload" : "Run Tests, Build and Upload") { m.start("testflight") }
            Button("Cancel", role: .cancel) { }
        } message: { Text("Source: \(m.root)\n\(m.skipTests ? "Tests will be skipped." : "Project tests and release gates will run.") The script may create a local Git tag. An upload already accepted by Apple cannot be cancelled.") }
    }
}

struct JobView: View {
    @EnvironmentObject var m: Model
    let job: Job
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(job.stage).font(.headline)
                Spacer()
                if job.active {
                    Button("Cancel") { m.perform { try await m.command(["cancel", job.id]); try await m.refresh() } }
                    Button("Recover…") { m.perform { try await m.command(["recover", job.id]); try await m.refresh() } }.help("Only releases resources when recorded worker and child processes have stopped.")
                }
            }
            Text(job.root).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            if !job.error.isEmpty { Text(job.error).foregroundStyle(.orange).textSelection(.enabled) }
            if job.status == "uploaded" || job.status == "available" {
                Text(job.status == "available" ? "App Store Connect reports this build is available to a tester group." : "Check Apple’s processing and beta distribution status using the project API key.").font(.callout)
                Button("Check TestFlight Status") { m.perform { try await m.command(["asc-status", job.id]); try await m.refresh() } }.disabled(m.busy)
                if let issue = job.monitorError, !issue.isEmpty { Text(issue).font(.caption).foregroundStyle(.secondary) }
                Link("Check App Store Connect", destination: URL(string: "https://appstoreconnect.apple.com/apps")!)
            }
            HStack {
                Text("Run log").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Spacer()
                Button("Reveal Run Files") { NSWorkspace.shared.selectFile(job.log, inFileViewerRootedAtPath: "") }
                Button("Open Project") { m.open(job.root) }
            }
            ScrollView([.vertical, .horizontal]) {
                Text(job.output ?? "Waiting for output…").font(.system(size: 11, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .topLeading).padding(12)
            }.background(Color(nsColor: .textBackgroundColor)).border(Color(nsColor: .separatorColor), width: 0.5)
        }.padding(20).frame(minWidth: 370, maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

struct DevicesView: View {
    @EnvironmentObject var m: Model
    @State private var release: Device?
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Simulator leases").font(.title2.weight(.semibold))
                    Text("Reserve for \(m.currentProject?.name ?? "a project"). App data stays on the device when a lease ends.").foregroundStyle(.secondary)
                }
                Spacer(); Button("Refresh Devices") { m.loadDevices() }.disabled(m.busy)
            }
            LabeledContent("Worktree") { TextField(m.currentProject?.root ?? "Repository path", text: $m.worktree).textFieldStyle(.roundedBorder) }
            if m.devices.isEmpty {
                ContentUnavailableView("No simulator inventory loaded", systemImage: "iphone.and.ipad", description: Text("Refresh devices to discover the simulators available through Xcode.")).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(m.devices) { d in
                    HStack(alignment: .top, spacing: 16) {
                        Image(systemName: d.family == "iPad" ? "ipad" : "iphone").font(.title2).frame(width: 28).padding(.top, 3)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(d.name).font(.headline)
                            Text("\(d.runtime) · \(d.state)").font(.caption).foregroundStyle(.secondary)
                            if let l = d.lease {
                                Text("Leased · \(l.project) · \(l.owner == "manual" ? "manual" : String(l.owner.prefix(8)))").font(.caption).foregroundStyle(.orange)
                                Text(l.worktree).font(.caption2).foregroundStyle(.secondary).textSelection(.enabled)
                            } else { Text("Available").font(.caption).foregroundStyle(.green) }
                            if let s = d.seed { Text("\(s.profile) · revision \(s.revision)").font(.caption).foregroundStyle(.secondary) }
                            Text(d.id).font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary).textSelection(.enabled)
                        }
                        Spacer()
                        if d.lease != nil {
                            Button(d.state == "Booted" ? "Shut Down" : "Boot") { m.deviceAction(d, d.state == "Booted" ? "shutdown" : "boot") }.disabled(m.busy)
                        }
                        if d.lease?.project == "telemetee" {
                            Button("Ensure Solina Data") { m.seed(d) }.disabled(m.busy).help("Requires Telemetee to be installed. Runs its existing idempotent course installer.")
                        }
                        Button(d.lease == nil ? "Lease" : "Release…") { if d.lease == nil { m.lease(d) } else { release = d } }.disabled(m.busy)
                    }.padding(.vertical, 10)
                }.listStyle(.inset)
            }
            Text("Leases coordinate FlightDeck clients. Direct Xcode or simctl commands can bypass them. A stale lease is retained until its owner is resolved.").font(.caption).foregroundStyle(.secondary)
        }.padding(22)
        .confirmationDialog("Release this device?", isPresented: Binding(get: { release != nil }, set: { if !$0 { release = nil } }), titleVisibility: .visible) {
            if let d = release { Button("Release \(d.name)") { m.lease(d); release = nil } }
            Button("Cancel", role: .cancel) { release = nil }
        } message: { Text("Finish any agent work using this device first. Its app data will be retained. Active release jobs cannot have their leases removed.") }
    }
}

struct StorageView: View {
    @EnvironmentObject var m: Model
    @State private var pending: StorageRow?
    func bytes(_ value: Int64?) -> String { value.map { ByteCountFormatter.string(fromByteCount: $0, countStyle: .file) } ?? "Unknown" }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Build storage").font(.title2.weight(.semibold))
                    Text("Inspect usage before removing rebuildable release intermediates.").foregroundStyle(.secondary)
                }
                Spacer(); Button("Scan Storage") { m.storage() }.disabled(m.busy)
            }
            if let scan = m.scan {
                Text(scan.note).font(.caption).foregroundStyle(.secondary)
                List(scan.rows) { row in
                    HStack(alignment: .top) {
                        VStack(alignment: .leading, spacing: 5) {
                            Text("\(row.project) · \(row.kind)").font(.headline)
                            Text(row.path).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                            Text(row.reason).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text(bytes(row.bytes)).monospacedDigit()
                        Button("Reveal") { NSWorkspace.shared.selectFile(row.path, inFileViewerRootedAtPath: "") }
                        if row.eligible { Button("Remove…", role: .destructive) { pending = row }.disabled(m.busy) }
                    }.padding(.vertical, 8)
                }
            } else {
                ContentUnavailableView("Know what is taking space", systemImage: "internaldrive", description: Text("Scan project builds, shared DerivedData and simulator devices. No files are removed during a scan.")).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }.padding(22)
        .confirmationDialog("Remove release intermediates?", isPresented: Binding(get: { pending != nil }, set: { if !$0 { pending = nil } }), titleVisibility: .visible) {
            if let row = pending { Button("Remove \(bytes(row.bytes))", role: .destructive) { m.clean(row); pending = nil } }
            Button("Cancel", role: .cancel) { pending = nil }
        } message: { Text("\(pending?.path ?? "")\nThis permanently removes only this DerivedData directory. The next build must regenerate it. Archives and dSYMs remain.") }
    }
}
