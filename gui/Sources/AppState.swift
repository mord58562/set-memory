import SwiftUI
import Combine

/// Central observable state. Owns the StateDB connection, kicks off
/// background reloads, watches state.db for changes, and exposes a
/// search throttle. Views observe @Published collections and re-render
/// without knowing about SQL.
@MainActor
final class AppState: ObservableObject {
    @Published var stats = SetMemoryStats()
    @Published var forgotten: [Track] = []
    @Published var recentUnplayed: [Track] = []
    @Published var neverPlayed: [Track] = []
    @Published var prepIssues: [Track] = []
    @Published var coAppearance: [CoAppearancePair] = []
    @Published var deletedCandidates: [DeletedCandidate] = []
    @Published var usbDrives: [UsbDrive] = []
    @Published var sessions: [SessionRecord] = []
    @Published var distribution = Distribution()
    @Published var sessionsByMonth: [(String, Int)] = []
    @Published var searchTerm: String = ""
    @Published var searchResults: [Track] = []
    @Published var thresholds = Thresholds()
    @Published var selectedSection: SidebarSection = .forgotten
    @Published var selectedTrack: Track?
    @Published var selectedSession: SessionRecord?
    @Published var suggestions: [PlaylistSuggestion] = []
    @Published var creatingPlaylistID: String? = nil
    @Published var lastPlaylistResult: String? = nil
    @Published var showDismissedSuggestions: Bool = false
    @Published var mountedRekordboxUsbs: [String] = []
    /// nil = sync every mounted USB. Otherwise restrict to the named volume.
    @Published var syncVolumeFilter: String? = nil
    @Published var syncing: Bool = false
    @Published var lastError: String?
    @Published var lastReloadAt: Date = .distantPast

    private let stateDbPath: String
    private let configPath: String
    private let scriptPath: String
    private let pythonPath: String
    private var fileWatcher: DispatchSourceFileSystemObject?
    private var searchTask: Task<Void, Never>?
    private var searchSinkCancellable: AnyCancellable?

    init(
        stateDbPath: String = NSString(string: "~/Downloads/set-memory/state.db").expandingTildeInPath,
        configPath: String = NSString(string: "~/Downloads/set-memory/config.json").expandingTildeInPath,
        scriptPath: String = NSString(string: "~/Downloads/set-memory/set_memory.py").expandingTildeInPath,
        pythonPath: String = NSString(string: "~/miniconda3/bin/python").expandingTildeInPath
    ) {
        self.stateDbPath = stateDbPath
        self.configPath = configPath
        self.scriptPath = scriptPath
        self.pythonPath = pythonPath
        loadThresholdsFromDisk()
        reloadAll()
        startFileWatcher()
        detectMountedRekordbox()
        observeSearch()
        startVolumeMonitoring()
    }

    deinit {
        fileWatcher?.cancel()
        NSWorkspace.shared.notificationCenter.removeObserver(self)
    }

    // MARK: - Live volume mount/unmount monitoring

    /// Subscribe to NSWorkspace mount/unmount notifications so the app
    /// reflects USB plug-in / pull-out without the user pressing Rescan.
    /// On a CDJ-export USB being mounted, also kick off an auto-sync.
    private func startVolumeMonitoring() {
        let nc = NSWorkspace.shared.notificationCenter
        nc.addObserver(forName: NSWorkspace.didMountNotification,
                       object: nil, queue: .main) { [weak self] note in
            guard let self else { return }
            self.detectMountedRekordbox()
            // If the newly-mounted volume is a CDJ-export USB and we're
            // not already syncing, fire ingest. Slight delay lets the
            // filesystem settle (rekordbox writes a couple of files
            // immediately after mount).
            let url = (note.userInfo?[NSWorkspace.volumeURLUserInfoKey] as? URL)
            let label = url?.lastPathComponent
            if let label, self.mountedRekordboxUsbs.contains(label), !self.syncing {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                    guard let self, !self.syncing else { return }
                    let saved = self.syncVolumeFilter
                    self.syncVolumeFilter = label
                    self.runSync()
                    self.syncVolumeFilter = saved
                }
            }
        }
        nc.addObserver(forName: NSWorkspace.didUnmountNotification,
                       object: nil, queue: .main) { [weak self] _ in
            self?.detectMountedRekordbox()
        }
    }

    // MARK: - Reload

    func reloadAll() {
        Task.detached(priority: .userInitiated) { [stateDbPath, thresholds] in
            guard FileManager.default.fileExists(atPath: stateDbPath),
                  let db = try? StateDB(path: stateDbPath) else {
                await MainActor.run { [weak self] in
                    self?.lastError = "No state.db at \(stateDbPath). Plug in a rekordbox USB to seed it."
                }
                return
            }
            let stats = db.stats()
            let forgotten = db.forgotten(
                minAppearances: thresholds.forgottenMinAppearances,
                daysSinceLast: thresholds.forgottenDaysSinceLast,
                limit: thresholds.forgottenLimit)
            let recent = db.recentlyAddedUnplayed(
                windowDays: thresholds.recentlyAddedWindowDays,
                limit: thresholds.recentlyAddedLimit)
            let never = db.neverPlayed(
                minDaysSinceAdd: thresholds.neverPlayedMinDaysSinceAdd,
                limit: thresholds.neverPlayedLimit)
            let prep = db.prepIssues(limit: thresholds.prepLimit)
            let co = db.coAppearance(
                minSessions: thresholds.coAppearanceMinSessions,
                limit: thresholds.coAppearanceLimit)
            let deleted = db.deletedCandidates(
                staleDays: thresholds.deletedStaleDays,
                limit: thresholds.deletedLimit)
            let usbs = db.usbDrives()
            let sessions = db.sessions(limit: 500)
            let byMonth = db.sessionsByMonth(months: thresholds.sparklineMonths)
            let dist = db.distribution()

            await MainActor.run { [weak self] in
                guard let self else { return }
                self.stats = stats
                self.forgotten = forgotten
                self.recentUnplayed = recent
                self.neverPlayed = never
                self.prepIssues = prep
                self.coAppearance = co
                self.deletedCandidates = deleted
                self.usbDrives = usbs
                self.sessions = sessions
                self.sessionsByMonth = byMonth
                self.distribution = dist
                self.lastReloadAt = Date()
                self.lastError = nil
            }
            // Suggestions are derived; refresh them whenever the base data
            // changes. Best-effort; the Python suggester runs in detached
            // task, doesn't block the UI.
            await MainActor.run { [weak self] in self?.reloadSuggestions() }
        }
    }

    // MARK: - Search (debounced)

    private func observeSearch() {
        searchSinkCancellable = $searchTerm
            .removeDuplicates()
            .debounce(for: .milliseconds(180), scheduler: RunLoop.main)
            .sink { [weak self] term in
                self?.runSearch(term: term)
            }
    }

    private func runSearch(term: String) {
        searchTask?.cancel()
        let trimmed = term.trimmingCharacters(in: .whitespaces)
        guard trimmed.count >= 2 else {
            searchResults = []
            return
        }
        searchTask = Task.detached(priority: .userInitiated) { [stateDbPath] in
            guard let db = try? StateDB(path: stateDbPath) else { return }
            let results = db.search(term: trimmed, limit: 200)
            await MainActor.run { [weak self] in
                self?.searchResults = results
            }
        }
    }

    // MARK: - Sync trigger

    func runSync() {
        guard !syncing else { return }
        syncing = true
        let py = pythonPath
        let script = scriptPath
        let filter = syncVolumeFilter
        Task.detached(priority: .userInitiated) { [weak self] in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: py)
            var args = [script, "--on-mount"]
            if let v = filter { args.append("--volume"); args.append(v) }
            proc.arguments = args
            let errPipe = Pipe()
            proc.standardError = errPipe
            do {
                try proc.run()
                proc.waitUntilExit()
                if proc.terminationStatus != 0 {
                    let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
                    let msg = String(data: errData, encoding: .utf8) ?? "exit \(proc.terminationStatus)"
                    let trimmed = msg.split(separator: "\n").suffix(3).joined(separator: " ")
                    await MainActor.run { [weak self] in
                        self?.lastError = "Sync exited \(proc.terminationStatus). \(trimmed)"
                    }
                }
            } catch {
                await MainActor.run { [weak self] in
                    self?.lastError = "Couldn't launch sync: \(error.localizedDescription)"
                }
            }
            await MainActor.run { [weak self] in
                self?.syncing = false
                self?.reloadAll()
                self?.detectMountedRekordbox()
            }
        }
    }

    // MARK: - File watcher (auto-refresh after launchd sync)

    private func startFileWatcher() {
        let fd = open(stateDbPath, O_EVTONLY)
        guard fd >= 0 else { return }
        let src = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .extend, .rename, .delete],
            queue: DispatchQueue.global(qos: .utility))
        src.setEventHandler { [weak self] in
            Task { @MainActor in
                self?.reloadAll()
            }
        }
        src.setCancelHandler { close(fd) }
        src.resume()
        fileWatcher = src
    }

    // MARK: - Suggestions + rekordbox playlist write

    /// Run the Python suggester. Results are cached in `suggestions`.
    /// Called automatically after every reloadAll(); also exposed as
    /// `Refresh suggestions` in the GUI.
    func reloadSuggestions() {
        let py = pythonPath
        let script = scriptPath
        let includeDismissed = showDismissedSuggestions
        Task.detached(priority: .utility) { [weak self] in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: py)
            var args = [script, "suggestions"]
            if includeDismissed { args.append("--include-dismissed") }
            proc.arguments = args
            let out = Pipe()
            proc.standardOutput = out
            proc.standardError = Pipe()  // discard
            do {
                try proc.run()
                proc.waitUntilExit()
                let data = out.fileHandleForReading.readDataToEndOfFile()
                guard let raw = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
                    return
                }
                let parsed: [PlaylistSuggestion] = raw.compactMap { dict in
                    guard let id = dict["id"] as? String,
                          let name = dict["name"] as? String,
                          let kind = dict["kind"] as? String,
                          let description = dict["description"] as? String,
                          let ids = dict["content_ids"] as? [String]
                    else { return nil }
                    return PlaylistSuggestion(
                        id: id, name: name, kind: kind, description: description,
                        contentIDs: ids,
                        rationale: (dict["rationale"] as? String) ?? "",
                        score: (dict["score"] as? Double) ?? 0,
                        dismissed: (dict["dismissed"] as? Bool) ?? false,
                    )
                }
                await MainActor.run { [weak self] in
                    self?.suggestions = parsed
                }
            } catch {
                // suggestions are best-effort; don't surface errors
            }
        }
    }

    func dismissSuggestion(_ suggestion: PlaylistSuggestion) {
        runSuggestionsCommand(["dismiss-suggestion", suggestion.id])
    }

    func undismissSuggestion(_ suggestion: PlaylistSuggestion) {
        runSuggestionsCommand(["undismiss-suggestion", suggestion.id])
    }

    func undismissAllSuggestions() {
        runSuggestionsCommand(["undismiss-suggestion", "--all"])
    }

    private func runSuggestionsCommand(_ extraArgs: [String]) {
        let py = pythonPath
        let script = scriptPath
        Task.detached(priority: .userInitiated) { [weak self] in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: py)
            proc.arguments = [script] + extraArgs
            proc.standardOutput = Pipe()
            proc.standardError = Pipe()
            try? proc.run()
            proc.waitUntilExit()
            await MainActor.run { [weak self] in self?.reloadSuggestions() }
        }
    }

    /// One-click: hand a suggestion's content_ids to Python's
    /// rekordbox_writer via the create-playlist CLI. Surfaces the JSON
    /// result back as `lastPlaylistResult` so the GUI can show a banner.
    func createPlaylist(from suggestion: PlaylistSuggestion) {
        guard creatingPlaylistID == nil else { return }
        creatingPlaylistID = suggestion.id
        let py = pythonPath
        let script = scriptPath
        let name = suggestion.name
        let sid = suggestion.id
        Task.detached(priority: .userInitiated) { [weak self] in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: py)
            proc.arguments = [script, "create-playlist", name, "--suggestion", sid]
            let out = Pipe()
            proc.standardOutput = out
            proc.standardError = Pipe()
            var message = "Created."
            do {
                try proc.run()
                proc.waitUntilExit()
                let data = out.fileHandleForReading.readDataToEndOfFile()
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    if let err = json["error"] as? String {
                        message = "rekordbox: \(err)"
                    } else if let added = json["tracks_added"] as? Int {
                        let unmatched = json["unmatched"] as? Int ?? 0
                        message = "\(name): added \(added) track(s)" +
                                  (unmatched > 0 ? "; \(unmatched) unmatched" : "")
                    }
                } else {
                    message = "rekordbox returned unexpected output"
                }
            } catch {
                message = "couldn't launch rekordbox writer: \(error.localizedDescription)"
            }
            await MainActor.run { [weak self] in
                self?.creatingPlaylistID = nil
                self?.lastPlaylistResult = message
            }
        }
    }

    // MARK: - Track file actions

    /// Resolve a track's stored file_path to an absolute reachable URL.
    /// - Pioneer-relative paths ("/Contents/..." from .pdb) are tried
    ///   against every currently-mounted rekordbox USB until one finds
    ///   the file on disk.
    /// - Absolute paths (master.db FolderPath+FileNameL) are used directly.
    /// Returns nil if the file isn't currently reachable.
    func resolveFile(for track: Track) -> URL? {
        guard let raw = track.filePath, !raw.isEmpty else { return nil }
        let fm = FileManager.default
        if raw.hasPrefix("/Volumes/") || raw.hasPrefix("/Users/") {
            let url = URL(fileURLWithPath: raw)
            return fm.fileExists(atPath: url.path) ? url : nil
        }
        // Pioneer-relative: walk mounted USBs.
        for label in mountedRekordboxUsbs {
            let candidate = URL(fileURLWithPath: "/Volumes/\(label)\(raw)")
            if fm.fileExists(atPath: candidate.path) { return candidate }
        }
        return nil
    }

    func playTrack(_ track: Track) {
        guard let url = resolveFile(for: track) else {
            lastError = "Track file not reachable. Plug the USB it lives on, or sync rekordbox to populate the absolute path."
            return
        }
        NSWorkspace.shared.open(url)
    }

    func revealTrackInFinder(_ track: Track) {
        guard let url = resolveFile(for: track) else {
            lastError = "Track file not reachable. Plug the USB it lives on, or sync rekordbox to populate the absolute path."
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    // MARK: - Mounted-USB detection

    func detectMountedRekordbox() {
        let fm = FileManager.default
        let volumes = (try? fm.contentsOfDirectory(atPath: "/Volumes")) ?? []
        var labels: [String] = []
        for v in volumes {
            // Two valid CDJ-export USB layouts:
            //   - desktop-rekordbox export: PIONEER/Master/master.db (SQLCipher)
            //   - CDJ export mode:          PIONEER/rekordbox/export.pdb (DeviceSQL)
            // Either qualifies the USB as ingestable.
            let master = "/Volumes/\(v)/PIONEER/Master/master.db"
            let pdb    = "/Volumes/\(v)/PIONEER/rekordbox/export.pdb"
            if fm.fileExists(atPath: master) || fm.fileExists(atPath: pdb) {
                labels.append(v)
            }
        }
        mountedRekordboxUsbs = labels
    }

    // MARK: - Config / thresholds

    private struct ConfigOnDisk: Codable {
        var forgotten_min_appearances: Int?
        var forgotten_days_since_last: Int?
        var forgotten_limit: Int?
        var never_played_min_days_since_add: Int?
        var never_played_limit: Int?
        var recently_added_window_days: Int?
        var recently_added_limit: Int?
        var prep_limit: Int?
        var co_appearance_min_sessions: Int?
        var co_appearance_limit: Int?
        var deleted_stale_days: Int?
        var deleted_limit: Int?
        var sparkline_months: Int?
    }

    private func loadThresholdsFromDisk() {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: configPath)),
              let parsed = try? JSONDecoder().decode(ConfigOnDisk.self, from: data) else {
            return
        }
        var t = Thresholds()
        if let v = parsed.forgotten_min_appearances { t.forgottenMinAppearances = v }
        if let v = parsed.forgotten_days_since_last { t.forgottenDaysSinceLast = v }
        if let v = parsed.forgotten_limit { t.forgottenLimit = v }
        if let v = parsed.never_played_min_days_since_add { t.neverPlayedMinDaysSinceAdd = v }
        if let v = parsed.never_played_limit { t.neverPlayedLimit = v }
        if let v = parsed.recently_added_window_days { t.recentlyAddedWindowDays = v }
        if let v = parsed.recently_added_limit { t.recentlyAddedLimit = v }
        if let v = parsed.prep_limit { t.prepLimit = v }
        if let v = parsed.co_appearance_min_sessions { t.coAppearanceMinSessions = v }
        if let v = parsed.co_appearance_limit { t.coAppearanceLimit = v }
        if let v = parsed.deleted_stale_days { t.deletedStaleDays = v }
        if let v = parsed.deleted_limit { t.deletedLimit = v }
        if let v = parsed.sparkline_months { t.sparklineMonths = v }
        thresholds = t
    }

    func saveThresholdsToDisk() {
        // Preserve any keys we don't know about by merging into existing JSON.
        var dict: [String: Any] = [:]
        if let data = try? Data(contentsOf: URL(fileURLWithPath: configPath)),
           let existing = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            dict = existing
        }
        dict["forgotten_min_appearances"] = thresholds.forgottenMinAppearances
        dict["forgotten_days_since_last"] = thresholds.forgottenDaysSinceLast
        dict["forgotten_limit"] = thresholds.forgottenLimit
        dict["never_played_min_days_since_add"] = thresholds.neverPlayedMinDaysSinceAdd
        dict["never_played_limit"] = thresholds.neverPlayedLimit
        dict["recently_added_window_days"] = thresholds.recentlyAddedWindowDays
        dict["recently_added_limit"] = thresholds.recentlyAddedLimit
        dict["prep_limit"] = thresholds.prepLimit
        dict["co_appearance_min_sessions"] = thresholds.coAppearanceMinSessions
        dict["co_appearance_limit"] = thresholds.coAppearanceLimit
        dict["deleted_stale_days"] = thresholds.deletedStaleDays
        dict["deleted_limit"] = thresholds.deletedLimit
        dict["sparkline_months"] = thresholds.sparklineMonths
        guard let out = try? JSONSerialization.data(withJSONObject: dict,
                                                    options: [.prettyPrinted, .sortedKeys]) else {
            lastError = "Could not encode config.json"
            return
        }
        do {
            try out.write(to: URL(fileURLWithPath: configPath), options: .atomic)
        } catch {
            lastError = "Could not write config.json: \(error.localizedDescription)"
            return
        }
        reloadAll()
    }
}
