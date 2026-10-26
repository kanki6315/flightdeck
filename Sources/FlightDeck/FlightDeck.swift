import SwiftUI
import AppKit

struct Project: Codable, Identifiable { let id, name, root, script, buildRoot: String; let families: [String] }
struct Job: Codable, Identifiable {
    let id, project, root, mode, status, stage, created, updated: String
    let commit, log, error: String
    var dirty: Bool?
    var artifacts: String?
    var title: String { mode == "test" ? "Tests" : mode == "testflight" ? "TestFlight" : "Local archive" }
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
    var testConflict: Bool {
        let path = URL(fileURLWithPath: (root as NSString).expandingTildeInPath).resolvingSymlinksInPath().path
        return jobs.contains { $0.active && ($0.mode != "test" || $0.root == path) }
    }
    var rootArgs: [String] { worktree.isEmpty ? [] : ["--worktree", worktree] }
    private var refreshGeneration = 0
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
        refreshGeneration += 1
        let generation = refreshGeneration
        let key = selectedJob
        let project = selectedProject
        let fetchedJobs = try await call(["jobs"], as: [Job].self)
        guard generation == refreshGeneration else { return }
        jobs = fetchedJobs
        if let key {
            let fetched = try await call(["detail", key], as: Job.self)
            guard generation == refreshGeneration, selectedJob == key,
                  selectedProject == project, project == fetched.project else { return }
            detail = fetched
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
            if mode != "test" && self.skipTests { args.append("--skip-tests") }
            if mode != "test" && !self.build.isEmpty { args += ["--build", self.build] }
            let job = try await self.call(args, as: Job.self)
            if self.selectedProject == job.project { self.selectedJob = job.id; self.detail = job }
            try await self.refresh()
        }
    }
    func select(_ id: String?) { guard id != selectedJob else { return }; selectedJob = id; detail = nil; Task { do { try await refresh() } catch { self.error = error.localizedDescription } } }
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
                guard !Task.isCancelled else { break }
                do { try await m.refresh() } catch { m.error = error.localizedDescription }
            }
        }
    }
}

struct ReleasesView: View {
    @EnvironmentObject var m: Model
    @State private var confirmingUpload = false
    @State private var showingReadiness = false
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
                    Button("Run Tests") { m.start("test") }.disabled(m.busy || m.testConflict)
                        .help("Run the test suites without archiving or uploading. Uncommitted changes are allowed.")
                    TextField("Build number (automatic)", text: $m.build).textFieldStyle(.roundedBorder).frame(width: 230)
                    Toggle("Skip tests", isOn: $m.skipTests).help("Use only when this exact commit has already passed the required tests.")
                    Spacer()
                    Text("Free simulators are leased automatically").font(.caption).foregroundStyle(.secondary)
                }
                if let p = m.preflight {
                    DisclosureGroup(isExpanded: $showingReadiness) {
                        ScrollView(.vertical) {
                            VStack(alignment: .leading, spacing: 10) {
                                ForEach(p.checks) { check in
                                    HStack(alignment: .top, spacing: 8) {
                                        Image(systemName: check.ok ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                                            .foregroundStyle(check.ok ? .green : .orange)
                                            .accessibilityLabel(check.ok ? "Passed" : "Needs attention")
                                        VStack(alignment: .leading, spacing: 3) {
                                            Text(check.name).fontWeight(.medium)
                                            Text(check.detail)
                                                .foregroundStyle(.secondary)
                                                .textSelection(.enabled)
                                                .fixedSize(horizontal: false, vertical: true)
                                        }.frame(maxWidth: .infinity, alignment: .leading)
                                    }.font(.caption)
                                }
                                Text(p.note).font(.caption).foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }.frame(maxWidth: .infinity, alignment: .leading).padding(8)
                        }
                        .frame(height: 160)
                        .accessibilityLabel("Readiness check results")
                    } label: {
                        Text(p.ready ? "Ready for project release checks" : "Readiness checks need attention")
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
                                HStack { Image(systemName: job.active ? "clock" : job.status == "failed" ? "exclamationmark.circle" : "checkmark.circle"); Text(job.title).fontWeight(.medium) }
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
        .onChange(of: m.selectedProject) { _, _ in showingReadiness = false }
        .confirmationDialog("Release \(m.currentProject?.name ?? "app") to TestFlight?", isPresented: $confirmingUpload, titleVisibility: .visible) {
            Button(m.skipTests ? "Build and Upload" : "Run Tests, Build and Upload") { m.start("testflight") }
            Button("Cancel", role: .cancel) { }
        } message: { Text("Source: \(m.root)\n\(m.skipTests ? "Tests will be skipped." : "Project tests and release gates will run.") The script may create a local Git tag. An upload already accepted by Apple cannot be cancelled.") }
    }
}

struct JobView: View {
    @EnvironmentObject var m: Model
    let job: Job
    @State private var followOutput = true
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
            if job.mode == "test" { Text(job.dirty == true ? "Tests · includes uncommitted changes" : "Tests · no archive or upload").font(.caption).foregroundStyle(.secondary) }
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
                Toggle("Follow output", isOn: $followOutput).toggleStyle(.checkbox)
                Button("Reveal Run Files") { NSWorkspace.shared.selectFile(job.log, inFileViewerRootedAtPath: "") }
                if let artifacts = job.artifacts, job.mode == "test" { Button("Test Results") { m.open(artifacts) } }
                Button("Open Project") { m.open(job.root) }
            }
            RunLogView(jobID: job.id, output: job.output, active: job.active, followOutput: followOutput)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .border(Color(nsColor: .separatorColor), width: 0.5)
        }
        .onChange(of: job.id) { _, _ in followOutput = true }.padding(20).frame(minWidth: 370, maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

/// AppKit lays out large, selectable logs without SwiftUI Text's sizing ambiguity.
struct RunLogView: NSViewRepresentable {
    let jobID: String
    let output: String?
    let active: Bool
    let followOutput: Bool

    final class Coordinator {
        var jobID: String?
        var followOutput = false
    }
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = true
        scroll.autohidesScrollers = true
        scroll.borderType = .noBorder
        let text = NSTextView(frame: .zero)
        text.isEditable = false
        text.isSelectable = true
        text.isRichText = false
        text.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        text.textColor = .textColor
        text.backgroundColor = .textBackgroundColor
        text.textContainerInset = NSSize(width: 12, height: 12)
        text.minSize = .zero
        text.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        text.isVerticallyResizable = true
        text.isHorizontallyResizable = true
        text.textContainer?.containerSize = text.maxSize
        text.textContainer?.widthTracksTextView = false
        text.setAccessibilityLabel("Run log")
        scroll.documentView = text
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let text = scroll.documentView as? NSTextView else { return }
        let changedJob = context.coordinator.jobID != jobID
        let value = output.flatMap { $0.isEmpty ? nil : $0 }
            ?? (active ? "Waiting for output…" : "No output was recorded for this run.")
        let changedText = text.string != value
        let resumed = followOutput && !context.coordinator.followOutput
        let origin = scroll.contentView.bounds.origin
        if changedText || changedJob {
            text.string = value
            text.layoutManager?.ensureLayout(for: text.textContainer!)
            text.sizeToFit()
        }
        if followOutput && (changedText || changedJob || resumed) {
            text.scrollRangeToVisible(NSRange(location: text.string.utf16.count, length: 0))
        } else if changedText || changedJob {
            scroll.contentView.scroll(to: changedJob ? .zero : origin)
            scroll.reflectScrolledClipView(scroll.contentView)
        }
        context.coordinator.jobID = jobID
        context.coordinator.followOutput = followOutput
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
