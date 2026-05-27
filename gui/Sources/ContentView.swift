import SwiftUI

/// The active surface. No clever "headline" copy - just the section
/// title at small caps with the row count, and dense data below.
/// Track rows inline-expand on click; no separate inspector pane.
struct ContentView: View {
    @EnvironmentObject var state: AppState
    let section: SidebarSection

    var body: some View {
        VStack(spacing: 0) {
            header
            ScrollView {
                surface
                    .padding(.horizontal, 14)
                    .padding(.vertical, 12)
            }
            .background(Theme.bg)
        }
        .background(Theme.bg)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text(section.title.uppercased())
                    .font(Type.micro).tracking(1.6)
                    .foregroundColor(Theme.ink2)
                if let n = count {
                    Text("\(n)")
                        .font(Type.data)
                        .foregroundColor(Theme.ink3)
                }
                Spacer()
            }
            Text(section.helperText)
                .font(Type.body)
                .foregroundColor(Theme.ink3)
        }
        .padding(.horizontal, 18)
        .padding(.top, 14)
        .padding(.bottom, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var count: Int? {
        switch section {
        case .forgotten:      return state.forgotten.count
        case .recentUnplayed: return state.recentUnplayed.count
        case .neverPlayed:    return state.neverPlayed.count
        case .prep:           return state.prepIssues.count
        case .together:       return state.coAppearance.count
        case .deleted:        return state.deletedCandidates.count
        case .usb:            return state.usbDrives.count
        case .sessions:       return state.sessions.count
        case .search:
            let t = state.searchTerm.trimmingCharacters(in: .whitespaces)
            return t.isEmpty ? nil : state.searchResults.count
        case .distribution:   return nil
        }
    }

    @ViewBuilder
    private var surface: some View {
        switch section {
        case .forgotten:      TrackTable(tracks: state.forgotten, kind: .forgotten)
        case .recentUnplayed: TrackTable(tracks: state.recentUnplayed, kind: .recentUnplayed)
        case .neverPlayed:    TrackTable(tracks: state.neverPlayed, kind: .neverPlayed)
        case .prep:           TrackTable(tracks: state.prepIssues, kind: .prep)
        case .together:       PairTable()
        case .distribution:   DistributionPanel()
        case .usb:            UsbTable()
        case .search:         SearchPanel()
        case .sessions:       SessionsTable()
        case .deleted:        DeletedTable()
        }
    }
}

// ---------------------------------------------------------------------------
// Track table - rekordbox-inspired dense rows with column headers
// ---------------------------------------------------------------------------

enum TrackTableKind {
    case forgotten, recentUnplayed, neverPlayed, prep, search

    var trailingLabel: String {
        switch self {
        case .forgotten:      return "LAST"
        case .recentUnplayed: return "ADDED"
        case .neverPlayed:    return "ADDED"
        case .prep:           return "MISSING"
        case .search:         return "PLAYS"
        }
    }
}

struct TrackTable: View {
    @EnvironmentObject var state: AppState
    let tracks: [Track]
    let kind: TrackTableKind

    var body: some View {
        if tracks.isEmpty {
            EmptyState(message: emptyMessage)
        } else {
            VStack(spacing: 0) {
                columnHeaders
                ForEach(tracks) { TrackRow(track: $0, kind: kind) }
            }
        }
    }

    private var columnHeaders: some View {
        HStack(spacing: 12) {
            colLabel("BPM").frame(width: 36, alignment: .trailing)
            colLabel("KEY").frame(width: 42, alignment: .leading)
            colLabel("TITLE  /  ARTIST").frame(maxWidth: .infinity, alignment: .leading)
            colLabel(kind.trailingLabel).frame(width: 140, alignment: .trailing)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
        .background(Theme.surface)
    }

    private func colLabel(_ s: String) -> some View {
        Text(s)
            .font(Type.micro).tracking(1.0)
            .foregroundColor(Theme.ink3)
    }

    private var emptyMessage: String {
        switch kind {
        case .forgotten:
            return "Nothing at ≥\(state.thresholds.forgottenMinAppearances) plays and >\(state.thresholds.forgottenDaysSinceLast)d cold. Loosen the threshold in Settings (⌘,)."
        case .recentUnplayed:
            return "Every track added in the last \(state.thresholds.recentlyAddedWindowDays) days has been played."
        case .neverPlayed:
            return "Either every library track has been played, or no CDJ-export USB has been synced yet."
        case .prep:
            return "Every library track has BPM, key, and at least one hot cue."
        case .search:
            return "Type at least 2 characters in the search field above."
        }
    }
}

/// Inline-expanding row. Click to expand details inline; click again to
/// collapse. No separate inspector pane.
struct TrackRow: View {
    @EnvironmentObject var state: AppState
    let track: Track
    let kind: TrackTableKind
    @State private var hover = false

    var body: some View {
        let expanded = state.selectedTrack?.id == track.id
        VStack(spacing: 0) {
            mainRow(expanded: expanded)
            if expanded { expansion }
            Divider().background(Theme.stroke).opacity(0.5)
        }
        .background(expanded ? Theme.selected : (hover ? Theme.hover : Color.clear))
        .onHover { hover = $0 }
        .contentShape(Rectangle())
        .onTapGesture {
            state.selectedTrack = expanded ? nil : track
        }
        .contextMenu { TrackContextMenu(track: track) }
    }

    private func mainRow(expanded: Bool) -> some View {
        HStack(spacing: 12) {
            BPMCell(bpm: track.bpm)
            CamelotChip(key: track.keyCamelot).frame(width: 42, alignment: .leading)
            VStack(alignment: .leading, spacing: 1) {
                Text(track.displayTitle)
                    .font(Type.bodyStrong)
                    .foregroundColor(Theme.ink)
                    .lineLimit(1)
                Text(track.displayArtist)
                    .font(Type.body)
                    .foregroundColor(Theme.ink2)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            trailing
                .frame(width: 140, alignment: .trailing)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
    }

    @ViewBuilder
    private var trailing: some View {
        switch kind {
        case .forgotten:
            HStack(spacing: 8) {
                Text("\(track.totalAppearances)×")
                    .font(Type.dataMid).foregroundColor(Theme.mono)
                Text(shortDate(track.lastSessionDate))
                    .font(Type.data).foregroundColor(Theme.ink3)
            }
        case .recentUnplayed, .neverPlayed:
            Text(shortDate(track.addedAt ?? track.dateCreated))
                .font(Type.data).foregroundColor(Theme.ink3)
        case .prep:
            HStack(spacing: 4) {
                if track.bpm == nil { PrepPill(label: "bpm") }
                if track.keyCamelot == nil { PrepPill(label: "key") }
                if (track.hotCueCount ?? -1) == 0 { PrepPill(label: "cues") }
                Text("\(track.totalAppearances)×")
                    .font(Type.data).foregroundColor(Theme.ink3)
                    .padding(.leading, 6)
            }
        case .search:
            HStack(spacing: 8) {
                Text("\(track.totalAppearances)×")
                    .font(Type.dataMid).foregroundColor(Theme.mono)
                if !track.inLibrary {
                    Circle().fill(Theme.coral).frame(width: 5, height: 5)
                        .help("Not in current library - possibly deleted")
                }
            }
        }
    }

    /// Inline expansion - shown when the row is selected. Holds detail
    /// fields and copy buttons. Replaces what used to be the right
    /// inspector pane.
    private var expansion: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 24) {
                detailColumn(items: [
                    ("PLAYS",        "\(track.totalAppearances)"),
                    ("LAST PLAYED",  track.lastSessionDate.map { String($0.prefix(16)) } ?? "never"),
                    ("ADDED",        track.addedAt.map { String($0.prefix(10)) } ?? "—"),
                ])
                detailColumn(items: [
                    ("FILE MTIME",   track.dateCreated.map { String($0.prefix(10)) } ?? "—"),
                    ("HOT CUES",     track.hotCueCount.map { "\($0)" } ?? "unknown"),
                    ("MEMORY CUES",  track.memoryCueCount.map { "\($0)" } ?? "unknown"),
                ])
                Spacer()
                copyActions
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(Theme.surface)
    }

    private func detailColumn(items: [(String, String)]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(items, id: \.0) { item in
                HStack(spacing: 10) {
                    Text(item.0).font(Type.micro).tracking(1.0)
                        .foregroundColor(Theme.ink3).frame(width: 100, alignment: .leading)
                    Text(item.1).font(Type.data).foregroundColor(Theme.mono)
                        .textSelection(.enabled)
                }
            }
        }
    }

    private var copyActions: some View {
        VStack(alignment: .trailing, spacing: 5) {
            copyBtn("Copy \"Title — Artist\"") {
                copy("\(track.displayTitle) - \(track.displayArtist)")
            }
            copyBtn("Copy title") { copy(track.displayTitle) }
            copyBtn("Copy artist") { copy(track.displayArtist) }
        }
    }

    private func copyBtn(_ label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(label)
                .font(Type.body)
                .foregroundColor(Theme.ink)
                .padding(.horizontal, 10).padding(.vertical, 4)
                .background(Theme.hover)
                .cornerRadius(3)
        }
        .buttonStyle(.plain)
    }

    private func copy(_ s: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
    }
}

struct TrackContextMenu: View {
    let track: Track
    var body: some View {
        Button("Copy title") { copy(track.displayTitle) }
        Button("Copy artist") { copy(track.displayArtist) }
        Button("Copy \"Title - Artist\"") {
            copy("\(track.displayTitle) - \(track.displayArtist)")
        }
    }
    private func copy(_ s: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
    }
}

struct EmptyState: View {
    let message: String
    var body: some View {
        Text(message)
            .font(Type.body)
            .foregroundColor(Theme.ink3)
            .multilineTextAlignment(.leading)
            .frame(maxWidth: 520, alignment: .leading)
            .padding(20)
    }
}

// ---------------------------------------------------------------------------
// Pairs (played-together)
// ---------------------------------------------------------------------------

struct PairTable: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        if state.coAppearance.isEmpty {
            EmptyState(message: "Two tracks need to share at least \(state.thresholds.coAppearanceMinSessions) sessions before they show up.")
        } else {
            VStack(spacing: 0) {
                HStack(spacing: 12) {
                    Text("TRACK A").font(Type.micro).tracking(1.0)
                        .foregroundColor(Theme.ink3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Text("TRACK B").font(Type.micro).tracking(1.0)
                        .foregroundColor(Theme.ink3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Text("SHARED").font(Type.micro).tracking(1.0)
                        .foregroundColor(Theme.ink3)
                        .frame(width: 70, alignment: .trailing)
                }
                .padding(.horizontal, 14).padding(.vertical, 6)
                .background(Theme.surface)
                ForEach(state.coAppearance) { PairRow(pair: $0) }
            }
        }
    }
}

struct PairRow: View {
    let pair: CoAppearancePair
    @State private var hover = false
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(pair.aTitle ?? "Unknown")
                        .font(Type.bodyStrong).foregroundColor(Theme.ink).lineLimit(1)
                    Text(pair.aArtist ?? "—")
                        .font(Type.body).foregroundColor(Theme.ink2).lineLimit(1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                VStack(alignment: .leading, spacing: 1) {
                    Text(pair.bTitle ?? "Unknown")
                        .font(Type.bodyStrong).foregroundColor(Theme.ink).lineLimit(1)
                    Text(pair.bArtist ?? "—")
                        .font(Type.body).foregroundColor(Theme.ink2).lineLimit(1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Text("\(pair.sharedSessions)×")
                    .font(Type.dataMid).foregroundColor(Theme.mono)
                    .frame(width: 70, alignment: .trailing)
            }
            .padding(.horizontal, 14).padding(.vertical, 8)
            Divider().background(Theme.stroke).opacity(0.5)
        }
        .background(hover ? Theme.hover : Color.clear)
        .onHover { hover = $0 }
    }
}

// ---------------------------------------------------------------------------
// USB drives
// ---------------------------------------------------------------------------

struct UsbTable: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        if state.usbDrives.isEmpty {
            EmptyState(message: "No CDJ-export USBs ingested yet. Plug one in and hit Sync.")
        } else {
            VStack(spacing: 0) {
                ForEach(state.usbDrives) { UsbRow(drive: $0) }
            }
        }
    }
}

struct UsbRow: View {
    @EnvironmentObject var state: AppState
    let drive: UsbDrive
    var body: some View {
        let mounted = state.mountedRekordboxUsbs.contains(drive.volumeLabel)
        VStack(spacing: 0) {
            HStack(spacing: 14) {
                Circle()
                    .fill(mounted ? Theme.cyan : Theme.ink3)
                    .frame(width: 6, height: 6)
                VStack(alignment: .leading, spacing: 1) {
                    Text(drive.volumeLabel)
                        .font(Type.bodyStrong).foregroundColor(Theme.ink)
                    Text(mounted ? "mounted now" : "last seen " + relative(drive.lastSeenAt))
                        .font(Type.body)
                        .foregroundColor(mounted ? Theme.cyan : Theme.ink2)
                }
                Spacer()
                Text("\(drive.librarySize) TRACKS")
                    .font(Type.data).tracking(0.8)
                    .foregroundColor(Theme.ink3)
            }
            .padding(.horizontal, 14).padding(.vertical, 12)
            Divider().background(Theme.stroke).opacity(0.5)
        }
    }
}

// ---------------------------------------------------------------------------
// Distribution
// ---------------------------------------------------------------------------

struct DistributionPanel: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        let totalPlays = state.distribution.bpmBuckets.reduce(0) { $0 + $1.count }
        let totalKey = state.distribution.topKeys.reduce(0) { $0 + $1.count }
        VStack(alignment: .leading, spacing: 22) {
            BpmRamp(buckets: state.distribution.bpmBuckets, total: totalPlays)
            KeyBars(keys: state.distribution.topKeys, total: totalKey)
            if !state.sessionsByMonth.isEmpty {
                MonthSpark(values: state.sessionsByMonth)
            }
        }
    }
}

struct BpmRamp: View {
    let buckets: [(label: String, count: Int)]
    let total: Int
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("TEMPO PROFILE").font(Type.micro).tracking(1.4).foregroundColor(Theme.ink2)
                Spacer()
                Text("\(total) PLAYS").font(Type.micro).tracking(1.0).foregroundColor(Theme.ink3)
            }
            VStack(spacing: 6) {
                ForEach(buckets, id: \.label) { b in
                    let color = (Theme.tempoRamp.first { $0.label == b.label }?.color) ?? Theme.ink3
                    HStack(spacing: 10) {
                        Text(b.label)
                            .font(Type.data)
                            .foregroundColor(color)
                            .frame(width: 60, alignment: .leading)
                        GeometryReader { geo in
                            let pct = total == 0 ? 0 : Double(b.count) / Double(total)
                            ZStack(alignment: .leading) {
                                Rectangle().fill(Theme.surface)
                                Rectangle().fill(color)
                                    .frame(width: max(2, geo.size.width * pct))
                            }
                        }
                        .frame(height: 8)
                        Text("\(b.count) · \(pctString(b.count, total))")
                            .font(Type.data).foregroundColor(Theme.mono)
                            .frame(width: 100, alignment: .trailing)
                    }
                }
            }
        }
    }
}

struct KeyBars: View {
    let keys: [(key: String, count: Int)]
    let total: Int
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("TOP CAMELOT KEYS").font(Type.micro).tracking(1.4).foregroundColor(Theme.ink2)
                Spacer()
                Text("\(total) PLAYS").font(Type.micro).tracking(1.0).foregroundColor(Theme.ink3)
            }
            VStack(spacing: 6) {
                ForEach(keys.prefix(10), id: \.key) { k in
                    HStack(spacing: 10) {
                        CamelotChip(key: k.key).frame(width: 50, alignment: .leading)
                        GeometryReader { geo in
                            let pct = total == 0 ? 0 : Double(k.count) / Double(total)
                            ZStack(alignment: .leading) {
                                Rectangle().fill(Theme.surface)
                                Rectangle().fill(Theme.cyan)
                                    .frame(width: max(2, geo.size.width * pct))
                            }
                        }
                        .frame(height: 8)
                        Text("\(k.count)")
                            .font(Type.data).foregroundColor(Theme.mono)
                            .frame(width: 50, alignment: .trailing)
                    }
                }
            }
        }
    }
}

struct MonthSpark: View {
    let values: [(String, Int)]
    var body: some View {
        let peak = max(1, values.map { $0.1 }.max() ?? 1)
        let total = values.reduce(0) { $0 + $1.1 }
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("SESSIONS PER MONTH").font(Type.micro).tracking(1.4).foregroundColor(Theme.ink2)
                Spacer()
                Text("\(total) TOTAL · PEAK \(peak)/MO").font(Type.micro).tracking(1.0).foregroundColor(Theme.ink3)
            }
            GeometryReader { geo in
                let barW = max(4, geo.size.width / CGFloat(max(values.count, 1)) - 3)
                HStack(alignment: .bottom, spacing: 3) {
                    ForEach(values, id: \.0) { ym, n in
                        VStack(spacing: 4) {
                            Spacer(minLength: 0)
                            Rectangle()
                                .fill(n == 0 ? Theme.surface : Theme.cyan)
                                .frame(width: barW,
                                       height: max(1, geo.size.height * CGFloat(n) / CGFloat(peak)))
                            Text(String(ym.suffix(2)))
                                .font(Type.micro).foregroundColor(Theme.ink3)
                        }
                        .help("\(ym): \(n) session(s)")
                    }
                }
            }
            .frame(height: 88)
        }
    }
}

private func pctString(_ count: Int, _ total: Int) -> String {
    guard total > 0 else { return "0%" }
    return "\(Int(round(Double(count) * 100 / Double(total))))%"
}

// ---------------------------------------------------------------------------
// Search / Sessions / Deleted
// ---------------------------------------------------------------------------

struct SearchPanel: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        if state.searchTerm.trimmingCharacters(in: .whitespaces).count < 2 {
            EmptyState(message: "Type at least 2 characters in the search field above.")
        } else if state.searchResults.isEmpty {
            EmptyState(message: "No matches for \"\(state.searchTerm)\".")
        } else {
            TrackTable(tracks: state.searchResults, kind: .search)
        }
    }
}

struct SessionsTable: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        if state.sessions.isEmpty {
            EmptyState(message: "No sessions in state.db yet.")
        } else {
            VStack(spacing: 0) {
                HStack(spacing: 12) {
                    Text("ID").font(Type.micro).tracking(1.0)
                        .foregroundColor(Theme.ink3).frame(width: 50, alignment: .trailing)
                    Text("DATE").font(Type.micro).tracking(1.0)
                        .foregroundColor(Theme.ink3).frame(width: 160, alignment: .leading)
                    Text("SOURCE").font(Type.micro).tracking(1.0)
                        .foregroundColor(Theme.ink3).frame(maxWidth: .infinity, alignment: .leading)
                    Text("TRACKS").font(Type.micro).tracking(1.0)
                        .foregroundColor(Theme.ink3).frame(width: 70, alignment: .trailing)
                }
                .padding(.horizontal, 14).padding(.vertical, 6)
                .background(Theme.surface)
                ForEach(state.sessions) { SessionRow(session: $0) }
            }
        }
    }
}

struct SessionRow: View {
    @EnvironmentObject var state: AppState
    let session: SessionRecord
    @State private var hover = false
    @State private var sessionTracks: [Track] = []
    var body: some View {
        let expanded = state.selectedSession?.id == session.id
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Text("#\(session.sessionID)")
                    .font(Type.data).foregroundColor(Theme.ink3)
                    .frame(width: 50, alignment: .trailing)
                Text(session.sessionDate.prefix(19).description)
                    .font(Type.data).foregroundColor(Theme.ink)
                    .frame(width: 160, alignment: .leading)
                Text(session.sourceLabel)
                    .font(Type.body).foregroundColor(Theme.ink2).lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text("\(session.trackCount)")
                    .font(Type.dataMid).foregroundColor(Theme.mono)
                    .frame(width: 70, alignment: .trailing)
            }
            .padding(.horizontal, 14).padding(.vertical, 7)
            if expanded {
                VStack(spacing: 0) {
                    ForEach(Array(sessionTracks.enumerated()), id: \.element.id) { idx, t in
                        HStack(spacing: 10) {
                            Text(String(format: "%02d", idx + 1))
                                .font(Type.data).foregroundColor(Theme.ink3)
                                .frame(width: 28, alignment: .trailing)
                            BPMCell(bpm: t.bpm)
                            CamelotChip(key: t.keyCamelot).frame(width: 42, alignment: .leading)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(t.displayTitle).font(Type.body).foregroundColor(Theme.ink).lineLimit(1)
                                Text(t.displayArtist).font(Type.body).foregroundColor(Theme.ink2).lineLimit(1)
                            }
                            Spacer()
                        }
                        .padding(.horizontal, 14).padding(.vertical, 4)
                    }
                }
                .padding(.vertical, 6)
                .background(Theme.surface)
            }
            Divider().background(Theme.stroke).opacity(0.5)
        }
        .background(expanded ? Theme.selected : (hover ? Theme.hover : Color.clear))
        .onHover { hover = $0 }
        .contentShape(Rectangle())
        .onTapGesture {
            if expanded {
                state.selectedSession = nil
            } else {
                state.selectedSession = session
                loadTracks()
            }
        }
    }

    private func loadTracks() {
        let id = session.id
        Task.detached {
            guard let db = try? StateDB(
                path: NSString(string: "~/Downloads/set-memory/state.db").expandingTildeInPath
            ) else { return }
            let rows = db.tracksInSession(id)
            await MainActor.run { self.sessionTracks = rows }
        }
    }
}

struct DeletedTable: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        if state.deletedCandidates.isEmpty {
            EmptyState(message: "Every track Set Memory has ever seen is still in at least one recently-synced library.")
        } else {
            VStack(spacing: 0) {
                ForEach(state.deletedCandidates) { d in
                    HStack(spacing: 12) {
                        Circle().fill(Theme.coral).frame(width: 5, height: 5)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(d.title ?? "Unknown")
                                .font(Type.bodyStrong).foregroundColor(Theme.ink).lineLimit(1)
                            Text(d.artist ?? "—")
                                .font(Type.body).foregroundColor(Theme.ink2).lineLimit(1)
                        }
                        Spacer()
                        Text("\(d.totalAppearances)× · last in lib \(shortDate(d.lastInLibraryAt))")
                            .font(Type.data).foregroundColor(Theme.ink3)
                    }
                    .padding(.horizontal, 14).padding(.vertical, 8)
                    Divider().background(Theme.stroke).opacity(0.5)
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func shortDate(_ iso: String?) -> String {
    guard let iso = iso else { return "—" }
    return String(iso.prefix(10))
}

func relative(_ iso: String) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let date = formatter.date(from: iso) ?? ISO8601DateFormatter().date(from: iso)
    guard let d = date else { return String(iso.prefix(16)) }
    let r = RelativeDateTimeFormatter()
    r.unitsStyle = .abbreviated
    return r.localizedString(for: d, relativeTo: Date())
}
