import Foundation
import SQLite3

/// Read-only SQLite wrapper around ~/Downloads/set-memory/state.db.
///
/// state.db is plain SQLite (the SQLCipher boundary stays in Python).
/// Opens read-only via URI mode=ro, queries are direct C API.
final class StateDB {
    private var handle: OpaquePointer?
    private let path: String

    init(path: String) throws {
        self.path = path
        var h: OpaquePointer?
        let flags = SQLITE_OPEN_READONLY | SQLITE_OPEN_URI
        let uri = "file:\(path)?mode=ro"
        let rc = sqlite3_open_v2(uri, &h, flags, nil)
        guard rc == SQLITE_OK, let h else {
            let msg = String(cString: sqlite3_errmsg(h))
            sqlite3_close(h)
            throw NSError(domain: "StateDB", code: Int(rc),
                          userInfo: [NSLocalizedDescriptionKey: msg])
        }
        self.handle = h
    }

    deinit { sqlite3_close(handle) }

    // MARK: - High-level queries

    func stats() -> SetMemoryStats {
        var s = SetMemoryStats()
        s.totalSessions = scalarInt("SELECT COUNT(*) FROM sessions") ?? 0
        s.totalUniqueTracks = scalarInt("SELECT COUNT(DISTINCT content_id) FROM appearances") ?? 0
        s.librarySize = scalarInt("SELECT COUNT(*) FROM tracks WHERE in_library = 1") ?? 0
        s.stateTrackCount = scalarInt("SELECT COUNT(*) FROM tracks") ?? 0
        s.usbDrivesSeen = scalarInt("SELECT COUNT(*) FROM usb_drives") ?? 0
        s.lastSyncAt = scalarString("SELECT value FROM meta WHERE key='last_sync_at'") ?? "never"
        return s
    }

    func forgotten(minAppearances: Int, daysSinceLast: Int, limit: Int) -> [Track] {
        let cutoff = isoDateOffset(days: -daysSinceLast)
        let sql = """
            SELECT t.content_id, t.title, t.artist, t.bpm, t.key_camelot,
                   t.total_appearances, s.session_date, t.added_at, t.date_created,
                   t.hot_cue_count, t.memory_cue_count, t.in_library
            FROM tracks t
            JOIN sessions s ON s.session_id = t.last_seen_session
            WHERE t.total_appearances >= ?
              AND s.session_date < ?
            ORDER BY t.total_appearances DESC
            LIMIT ?
        """
        return queryTracks(sql, params: [.int(minAppearances), .text(cutoff), .int(limit)])
    }

    func neverPlayed(minDaysSinceAdd: Int, limit: Int) -> [Track] {
        let cutoff = isoDateOffset(days: -minDaysSinceAdd)
        let sql = """
            SELECT t.content_id, t.title, t.artist, t.bpm, t.key_camelot,
                   t.total_appearances, NULL AS last_session_date,
                   t.added_at, t.date_created, t.hot_cue_count, t.memory_cue_count, t.in_library
            FROM tracks t
            WHERE t.total_appearances = 0
              AND t.in_library = 1
              AND COALESCE(t.added_at, t.date_created) IS NOT NULL
              AND COALESCE(t.added_at, t.date_created) < ?
            ORDER BY COALESCE(t.added_at, t.date_created) ASC
            LIMIT ?
        """
        return queryTracks(sql, params: [.text(cutoff), .int(limit)])
    }

    func recentlyAddedUnplayed(windowDays: Int, limit: Int) -> [Track] {
        let cutoff = isoDateOffset(days: -windowDays)
        let sql = """
            SELECT t.content_id, t.title, t.artist, t.bpm, t.key_camelot,
                   t.total_appearances, NULL AS last_session_date,
                   t.added_at, t.date_created, t.hot_cue_count, t.memory_cue_count, t.in_library
            FROM tracks t
            WHERE t.total_appearances = 0
              AND t.in_library = 1
              AND COALESCE(t.added_at, t.date_created) IS NOT NULL
              AND COALESCE(t.added_at, t.date_created) >= ?
            ORDER BY COALESCE(t.added_at, t.date_created) DESC
            LIMIT ?
        """
        return queryTracks(sql, params: [.text(cutoff), .int(limit)])
    }

    func prepIssues(limit: Int) -> [Track] {
        let sql = """
            SELECT t.content_id, t.title, t.artist, t.bpm, t.key_camelot,
                   t.total_appearances, NULL AS last_session_date,
                   t.added_at, t.date_created, t.hot_cue_count, t.memory_cue_count, t.in_library
            FROM tracks t
            WHERE t.in_library = 1
              AND (t.bpm IS NULL OR t.key_camelot IS NULL OR t.hot_cue_count = 0)
            ORDER BY t.total_appearances DESC, t.title ASC
            LIMIT ?
        """
        return queryTracks(sql, params: [.int(limit)])
    }

    func coAppearance(minSessions: Int, limit: Int) -> [CoAppearancePair] {
        let sql = """
            SELECT a.content_id, b.content_id,
                   ta.title, ta.artist, tb.title, tb.artist,
                   COUNT(DISTINCT a.session_id) AS shared
            FROM appearances a
            JOIN appearances b
              ON a.session_id = b.session_id AND a.content_id < b.content_id
            LEFT JOIN tracks ta ON ta.content_id = a.content_id
            LEFT JOIN tracks tb ON tb.content_id = b.content_id
            GROUP BY a.content_id, b.content_id
            HAVING shared >= ?
            ORDER BY shared DESC
            LIMIT ?
        """
        var result: [CoAppearancePair] = []
        prepare(sql, params: [.int(minSessions), .int(limit)]) { stmt in
            result.append(CoAppearancePair(
                aContentID: text(stmt, 0) ?? "",
                bContentID: text(stmt, 1) ?? "",
                aTitle: text(stmt, 2),
                aArtist: text(stmt, 3),
                bTitle: text(stmt, 4),
                bArtist: text(stmt, 5),
                sharedSessions: int(stmt, 6)
            ))
        }
        return result
    }

    func deletedCandidates(staleDays: Int, limit: Int) -> [DeletedCandidate] {
        let cutoff = isoDateOffset(days: -staleDays)
        let sql = """
            SELECT content_id, title, artist, last_in_library_at, total_appearances
            FROM tracks
            WHERE in_library = 0
               OR (last_in_library_at IS NOT NULL AND last_in_library_at < ?)
            ORDER BY total_appearances DESC, last_in_library_at ASC
            LIMIT ?
        """
        var result: [DeletedCandidate] = []
        prepare(sql, params: [.text(cutoff), .int(limit)]) { stmt in
            result.append(DeletedCandidate(
                contentID: text(stmt, 0) ?? "",
                title: text(stmt, 1),
                artist: text(stmt, 2),
                lastInLibraryAt: text(stmt, 3),
                totalAppearances: int(stmt, 4)
            ))
        }
        return result
    }

    func usbDrives() -> [UsbDrive] {
        let sql = """
            SELECT volume_label, master_db_path, first_seen_at, last_seen_at, library_size
            FROM usb_drives ORDER BY last_seen_at DESC
        """
        var result: [UsbDrive] = []
        prepare(sql) { stmt in
            result.append(UsbDrive(
                volumeLabel: text(stmt, 0) ?? "(unknown)",
                masterDbPath: text(stmt, 1),
                firstSeenAt: text(stmt, 2) ?? "",
                lastSeenAt: text(stmt, 3) ?? "",
                librarySize: int(stmt, 4)
            ))
        }
        return result
    }

    func search(term: String, limit: Int) -> [Track] {
        let like = "%\(term)%"
        let sql = """
            SELECT content_id, title, artist, bpm, key_camelot,
                   total_appearances, NULL AS last_session_date,
                   added_at, date_created, hot_cue_count, memory_cue_count, in_library
            FROM tracks
            WHERE title LIKE ? OR artist LIKE ?
            ORDER BY total_appearances DESC, title ASC
            LIMIT ?
        """
        return queryTracks(sql, params: [.text(like), .text(like), .int(limit)])
    }

    func sessions(limit: Int = 200) -> [SessionRecord] {
        let sql = """
            SELECT session_id, session_date, track_count, source_db_path
            FROM sessions ORDER BY session_date DESC LIMIT ?
        """
        var result: [SessionRecord] = []
        prepare(sql, params: [.int(limit)]) { stmt in
            let src = text(stmt, 3) ?? ""
            let label = (src as NSString).pathComponents
                .filter { $0 != "/" && $0 != "Volumes" && !$0.hasPrefix("PIONEER") && $0 != "Master" && !$0.hasPrefix("master.db") }
                .first ?? src
            result.append(SessionRecord(
                sessionID: int(stmt, 0),
                sessionDate: text(stmt, 1) ?? "",
                trackCount: int(stmt, 2),
                sourceLabel: label
            ))
        }
        return result
    }

    func tracksInSession(_ sessionID: Int) -> [Track] {
        let sql = """
            SELECT t.content_id, t.title, t.artist, t.bpm, t.key_camelot,
                   t.total_appearances, NULL AS last_session_date,
                   t.added_at, t.date_created, t.hot_cue_count, t.memory_cue_count, t.in_library
            FROM appearances a
            JOIN tracks t ON t.content_id = a.content_id
            WHERE a.session_id = ?
            ORDER BY a.track_no ASC
        """
        return queryTracks(sql, params: [.int(sessionID)])
    }

    func sessionsByMonth(months: Int) -> [(String, Int)] {
        let sql = """
            SELECT substr(session_date, 1, 7) AS ym, COUNT(*)
            FROM sessions GROUP BY ym
        """
        var raw: [String: Int] = [:]
        prepare(sql) { stmt in
            let ym = text(stmt, 0) ?? ""
            raw[ym] = int(stmt, 1)
        }
        var result: [(String, Int)] = []
        let cal = Calendar(identifier: .gregorian)
        var ref = Date()
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM"
        for _ in 0..<months {
            let ym = formatter.string(from: ref)
            result.append((ym, raw[ym] ?? 0))
            ref = cal.date(byAdding: .month, value: -1, to: ref) ?? ref
        }
        return Array(result.reversed())
    }

    func distribution() -> Distribution {
        let buckets: [(String, Double, Double)] = [
            ("<100",    0,   100),
            ("100-119", 100, 120),
            ("120-127", 120, 128),
            ("128-134", 128, 135),
            ("135-144", 135, 145),
            ("145-159", 145, 160),
            ("160+",    160, 100_000),
        ]
        var bpmCounts: [String: Int] = Dictionary(uniqueKeysWithValues: buckets.map { ($0.0, 0) })
        bpmCounts["unknown"] = 0
        var keyCounts: [String: Int] = [:]
        let sql = """
            SELECT t.bpm, t.key_camelot, COUNT(*) AS n
            FROM appearances a
            JOIN tracks t ON t.content_id = a.content_id
            GROUP BY t.bpm, t.key_camelot
        """
        prepare(sql) { stmt in
            let bpm = realOrNil(stmt, 0)
            let key = text(stmt, 1)
            let n = int(stmt, 2)
            if let b = bpm, b > 0 {
                let bucket = buckets.first { b >= $0.1 && b < $0.2 }?.0 ?? "unknown"
                bpmCounts[bucket, default: 0] += n
            } else {
                bpmCounts["unknown", default: 0] += n
            }
            let kl = key ?? "unknown"
            keyCounts[kl, default: 0] += n
        }
        var dist = Distribution()
        dist.bpmBuckets = (buckets.map { $0.0 } + ["unknown"]).map { ($0, bpmCounts[$0] ?? 0) }
        dist.topKeys = keyCounts.sorted { $0.value > $1.value }.prefix(12).map { ($0.key, $0.value) }
        return dist
    }

    // MARK: - SQLite helpers

    private enum Bind {
        case int(Int)
        case text(String)
    }

    private func prepare(_ sql: String, params: [Bind] = [], handler: (OpaquePointer) -> Void) {
        guard let handle else { return }
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(handle, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else {
            NSLog("StateDB prepare failed: \(String(cString: sqlite3_errmsg(handle))) / \(sql)")
            return
        }
        defer { sqlite3_finalize(stmt) }
        for (i, p) in params.enumerated() {
            let idx = Int32(i + 1)
            switch p {
            case .int(let n):
                sqlite3_bind_int64(stmt, idx, Int64(n))
            case .text(let s):
                let SQLITE_TRANSIENT = unsafeBitCast(OpaquePointer(bitPattern: -1)!, to: sqlite3_destructor_type.self)
                sqlite3_bind_text(stmt, idx, s, -1, SQLITE_TRANSIENT)
            }
        }
        while sqlite3_step(stmt) == SQLITE_ROW {
            handler(stmt)
        }
    }

    private func queryTracks(_ sql: String, params: [Bind] = []) -> [Track] {
        var result: [Track] = []
        prepare(sql, params: params) { stmt in
            result.append(Track(
                contentID: text(stmt, 0) ?? "",
                title: text(stmt, 1),
                artist: text(stmt, 2),
                bpm: realOrNil(stmt, 3),
                keyCamelot: text(stmt, 4),
                totalAppearances: int(stmt, 5),
                lastSessionDate: text(stmt, 6),
                addedAt: text(stmt, 7),
                dateCreated: text(stmt, 8),
                hotCueCount: intOrNil(stmt, 9),
                memoryCueCount: intOrNil(stmt, 10),
                inLibrary: int(stmt, 11) != 0
            ))
        }
        return result
    }

    private func scalarInt(_ sql: String) -> Int? {
        var result: Int?
        prepare(sql) { stmt in result = self.int(stmt, 0) }
        return result
    }

    private func scalarString(_ sql: String) -> String? {
        var result: String?
        prepare(sql) { stmt in result = self.text(stmt, 0) }
        return result
    }

    private func text(_ stmt: OpaquePointer, _ col: Int32) -> String? {
        guard let cstr = sqlite3_column_text(stmt, col) else { return nil }
        return String(cString: cstr)
    }

    private func int(_ stmt: OpaquePointer, _ col: Int32) -> Int {
        Int(sqlite3_column_int64(stmt, col))
    }

    private func intOrNil(_ stmt: OpaquePointer, _ col: Int32) -> Int? {
        if sqlite3_column_type(stmt, col) == SQLITE_NULL { return nil }
        return Int(sqlite3_column_int64(stmt, col))
    }

    private func realOrNil(_ stmt: OpaquePointer, _ col: Int32) -> Double? {
        if sqlite3_column_type(stmt, col) == SQLITE_NULL { return nil }
        return sqlite3_column_double(stmt, col)
    }

    private func isoDateOffset(days: Int) -> String {
        let date = Date().addingTimeInterval(TimeInterval(days * 86400))
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withFullDate]
        return formatter.string(from: date)
    }
}
