import SwiftUI

/// Sidebar with three collapsible groups. No stats panel (those live
/// in the bottom StatusBar). Selected row gets a left-edge cyan strip
/// rather than a filled background - cleaner, more rekordbox-flavoured.
struct Sidebar: View {
    @EnvironmentObject var state: AppState
    @State private var expanded: Set<String> = ["library", "patterns", "maintenance"]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                group("library", title: "LIBRARY",
                      items: [.suggestions, .forgotten, .recentUnplayed, .neverPlayed, .prep])
                group("patterns", title: "PATTERNS",
                      items: [.together, .distribution])
                group("maintenance", title: "MAINTENANCE",
                      items: [.usb, .deleted, .sessions, .search])
            }
            .padding(.top, 12)
            .padding(.bottom, 16)
        }
    }

    @ViewBuilder
    private func group(_ id: String, title: String, items: [SidebarSection]) -> some View {
        let open = expanded.contains(id)
        VStack(alignment: .leading, spacing: 1) {
            Button {
                if open { expanded.remove(id) } else { expanded.insert(id) }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8, weight: .bold))
                        .foregroundColor(Theme.ink3)
                        .rotationEffect(.degrees(open ? 90 : 0))
                    Text(title)
                        .font(Type.micro).tracking(1.4)
                        .foregroundColor(Theme.ink2)
                    Spacer()
                }
                .padding(.horizontal, 14).padding(.vertical, 4)
            }
            .buttonStyle(.plain)
            if open {
                ForEach(items) { SidebarRow(section: $0) }
            }
        }
    }
}

struct SidebarRow: View {
    @EnvironmentObject var state: AppState
    let section: SidebarSection

    var body: some View {
        let selected = state.selectedSection == section
        Button {
            state.selectedSection = section
            state.selectedTrack = nil
            state.selectedSession = nil
        } label: {
            HStack(spacing: 0) {
                Rectangle()
                    .fill(selected ? Theme.cyan : Color.clear)
                    .frame(width: 2)
                HStack(spacing: 8) {
                    Text(section.title)
                        .font(selected ? Type.bodyStrong : Type.body)
                        .foregroundColor(selected ? Theme.ink : Theme.ink2)
                    Spacer()
                    if let n = badge {
                        Text("\(n)")
                            .font(Type.data)
                            .foregroundColor(selected ? Theme.ink : Theme.ink3)
                    }
                }
                .padding(.horizontal, 12).padding(.vertical, 4)
            }
            .background(selected ? Theme.selected : Color.clear)
        }
        .buttonStyle(.plain)
    }

    private var badge: Int? {
        switch section {
        case .suggestions:    return state.suggestions.count
        case .forgotten:      return state.forgotten.count
        case .recentUnplayed: return state.recentUnplayed.count
        case .neverPlayed:    return state.neverPlayed.count
        case .prep:           return state.prepIssues.count
        case .together:       return state.coAppearance.count
        case .deleted:        return state.deletedCandidates.count
        case .usb:            return state.usbDrives.count
        case .sessions:       return state.sessions.count
        case .distribution, .search: return nil
        }
    }
}
