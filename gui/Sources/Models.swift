import Foundation

struct Track: Identifiable, Hashable {
    let contentID: String
    let title: String?
    let artist: String?
    let bpm: Double?
    let keyCamelot: String?
    let totalAppearances: Int
    let lastSessionDate: String?
    let addedAt: String?
    let dateCreated: String?
    let hotCueCount: Int?
    let memoryCueCount: Int?
    let inLibrary: Bool
    var id: String { contentID }

    var displayTitle: String { title ?? "Unknown" }
    var displayArtist: String { artist ?? "Unknown" }
}

struct UsbDrive: Identifiable, Hashable {
    let volumeLabel: String
    let masterDbPath: String?
    let firstSeenAt: String
    let lastSeenAt: String
    let librarySize: Int
    var id: String { volumeLabel }
}

struct CoAppearancePair: Identifiable, Hashable {
    let aContentID: String
    let bContentID: String
    let aTitle: String?
    let aArtist: String?
    let bTitle: String?
    let bArtist: String?
    let sharedSessions: Int
    var id: String { "\(aContentID)_\(bContentID)" }
}

struct DeletedCandidate: Identifiable, Hashable {
    let contentID: String
    let title: String?
    let artist: String?
    let lastInLibraryAt: String?
    let totalAppearances: Int
    var id: String { contentID }
}

struct SessionRecord: Identifiable, Hashable {
    let sessionID: Int
    let sessionDate: String
    let trackCount: Int
    let sourceLabel: String
    var id: Int { sessionID }
}

struct Distribution: Hashable {
    var bpmBuckets: [(label: String, count: Int)] = []
    var topKeys: [(key: String, count: Int)] = []

    static func == (lhs: Distribution, rhs: Distribution) -> Bool {
        lhs.bpmBuckets.elementsEqual(rhs.bpmBuckets, by: { $0 == $1 })
            && lhs.topKeys.elementsEqual(rhs.topKeys, by: { $0 == $1 })
    }
    func hash(into hasher: inout Hasher) {
        for b in bpmBuckets { hasher.combine(b.label); hasher.combine(b.count) }
        for k in topKeys    { hasher.combine(k.key);   hasher.combine(k.count) }
    }
}

struct SetMemoryStats {
    var totalSessions: Int = 0
    var totalUniqueTracks: Int = 0
    var librarySize: Int = 0
    var stateTrackCount: Int = 0
    var usbDrivesSeen: Int = 0
    var lastSyncAt: String = "never"
}

struct Thresholds: Equatable, Codable {
    var forgottenMinAppearances: Int = 5
    var forgottenDaysSinceLast: Int = 90
    var forgottenLimit: Int = 25
    var neverPlayedMinDaysSinceAdd: Int = 30
    var neverPlayedLimit: Int = 100
    var recentlyAddedWindowDays: Int = 30
    var recentlyAddedLimit: Int = 50
    var prepLimit: Int = 200
    var coAppearanceMinSessions: Int = 3
    var coAppearanceLimit: Int = 50
    var deletedStaleDays: Int = 60
    var deletedLimit: Int = 100
    var sparklineMonths: Int = 12
}

enum SidebarSection: String, CaseIterable, Identifiable {
    case forgotten, recentUnplayed, neverPlayed, prep, together
    case distribution, usb, search, sessions, deleted

    var id: String { rawValue }

    var title: String {
        switch self {
        case .forgotten:      return "Forgotten Favourites"
        case .recentUnplayed: return "Recently Added"
        case .neverPlayed:    return "Never Played"
        case .prep:           return "Prep Audit"
        case .together:       return "Played Together"
        case .distribution:   return "Distribution"
        case .usb:            return "USB Drives"
        case .search:         return "Search"
        case .sessions:       return "All Sessions"
        case .deleted:        return "Possibly Deleted"
        }
    }

    var sfSymbol: String {
        switch self {
        case .forgotten:      return "clock.arrow.circlepath"
        case .recentUnplayed: return "cart"
        case .neverPlayed:    return "moon.zzz"
        case .prep:           return "wrench.and.screwdriver"
        case .together:       return "link"
        case .distribution:   return "chart.bar"
        case .usb:            return "externaldrive"
        case .search:         return "magnifyingglass"
        case .sessions:       return "list.bullet.rectangle"
        case .deleted:        return "trash"
        }
    }

    var helperText: String {
        switch self {
        case .forgotten:      return "Tracks you played often, but haven't touched for a while. Loved + neglected."
        case .recentUnplayed: return "Tracks added recently that haven't made it into a set yet. Buy-regret check."
        case .neverPlayed:    return "Old library residents that have never appeared in any recorded session."
        case .prep:           return "Library tracks missing BPM, key, or hot cues. Sorted most-played first."
        case .together:       return "Track pairs that keep showing up in the same sessions. Set-planning fuel."
        case .distribution:   return "Histogram of plays across BPM buckets and Camelot keys. Set-diversity awareness."
        case .usb:            return "Every drive Set Memory has seen, with last-mounted timestamp and library size."
        case .search:         return "Type to search every track Set Memory has ever ingested - in library or not."
        case .sessions:       return "Every session in state.db, newest first. Click one to see its tracks."
        case .deleted:        return "Tracks recorded in state.db that no longer appear in any recently-synced library."
        }
    }
}
