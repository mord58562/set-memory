import SwiftUI

/// Two-pane layout with a top bar and a bottom status bar.
/// No always-on inspector - track details inline-expand on click.
/// No sidebar stats footer - all stats live in the bottom status bar.
struct MainView: View {
    @EnvironmentObject var state: AppState
    @State private var showingSettings = false

    var body: some View {
        VStack(spacing: 0) {
            TopBar(showingSettings: $showingSettings)
            Divider().background(Theme.stroke)
            HStack(spacing: 0) {
                Sidebar()
                    .frame(width: 218)
                    .background(Theme.surface)
                Divider().background(Theme.stroke)
                ContentView(section: state.selectedSection)
                    .frame(maxWidth: .infinity)
            }
            Divider().background(Theme.stroke)
            StatusBar()
        }
        .background(Theme.bg)
        .preferredColorScheme(.dark)
        .sheet(isPresented: $showingSettings) {
            SettingsView().environmentObject(state)
        }
        .onAppear { state.detectMountedRekordbox() }
    }
}

// ---------------------------------------------------------------------------
// Top bar - wordmark, search, action. No symmetric toolbar slots.
// ---------------------------------------------------------------------------

struct TopBar: View {
    @EnvironmentObject var state: AppState
    @Binding var showingSettings: Bool
    @FocusState private var searchFocus: Bool

    var body: some View {
        HStack(spacing: 18) {
            wordmark
            usbPicker
            Spacer()
            searchField
            Spacer()
            syncButton
            settingsButton
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 9)
        .background(Theme.bg)
        .background(
            Button("") { searchFocus = true }
                .keyboardShortcut("f", modifiers: [.command])
                .opacity(0)
        )
    }

    private var wordmark: some View {
        HStack(spacing: 9) {
            BrandGlyph().frame(width: 14, height: 18)
            Text("SET MEMORY")
                .font(.system(size: 11, weight: .semibold))
                .tracking(2.4)
                .foregroundColor(Theme.ink)
        }
        .padding(.trailing, 8)
    }

    private var usbPicker: some View {
        Menu {
            Button {
                state.syncVolumeFilter = nil
            } label: {
                HStack {
                    Image(systemName: state.syncVolumeFilter == nil ? "checkmark" : "")
                    Text("All mounted CDJ USBs")
                }
            }
            Divider()
            if state.mountedRekordboxUsbs.isEmpty {
                Text("No CDJ USB detected").foregroundColor(.secondary)
            } else {
                ForEach(state.mountedRekordboxUsbs, id: \.self) { label in
                    Button {
                        state.syncVolumeFilter = label
                    } label: {
                        HStack {
                            Image(systemName: state.syncVolumeFilter == label ? "checkmark" : "")
                            Text(label)
                        }
                    }
                }
            }
            Divider()
            Button("Rescan /Volumes") { state.detectMountedRekordbox() }
        } label: {
            HStack(spacing: 6) {
                Circle()
                    .fill(state.mountedRekordboxUsbs.isEmpty ? Theme.ink3 : Theme.cyan)
                    .frame(width: 5, height: 5)
                Text(pickerLabel)
                    .font(Type.body)
                    .foregroundColor(Theme.ink)
                Image(systemName: "chevron.down")
                    .font(.system(size: 8, weight: .semibold))
                    .foregroundColor(Theme.ink3)
            }
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .help("Which CDJ-export USB Sync should look at")
    }

    private var pickerLabel: String {
        if let v = state.syncVolumeFilter { return v }
        if state.mountedRekordboxUsbs.isEmpty { return "No USB" }
        if state.mountedRekordboxUsbs.count == 1 { return state.mountedRekordboxUsbs[0] }
        return "All (\(state.mountedRekordboxUsbs.count))"
    }

    private var searchField: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 10))
                .foregroundColor(Theme.ink3)
            TextField("Search every track ever ingested", text: $state.searchTerm)
                .textFieldStyle(.plain)
                .font(Type.body)
                .foregroundColor(Theme.ink)
                .focused($searchFocus)
                .onSubmit { state.selectedSection = .search }
                .onExitCommand { searchFocus = false }
            if state.searchTerm.isEmpty {
                Text(searchFocus ? "⎋" : "⌘F")
                    .font(Type.micro)
                    .tracking(0.6)
                    .foregroundColor(Theme.ink3)
            } else {
                Button {
                    state.searchTerm = ""
                    searchFocus = false
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundColor(Theme.ink3)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(Theme.surface)
        .cornerRadius(4)
        .frame(minWidth: 280, maxWidth: 440)
        .onChange(of: state.searchTerm) { newValue in
            if !newValue.isEmpty { state.selectedSection = .search }
        }
        // Hidden Escape catcher that fires even when the textfield isn't
        // the first responder - guarantees Escape always defocuses.
        .background(
            Button("") { searchFocus = false }
                .keyboardShortcut(.escape, modifiers: [])
                .opacity(0)
        )
    }

    private var syncButton: some View {
        Button {
            state.runSync()
        } label: {
            HStack(spacing: 5) {
                if state.syncing {
                    ProgressView().controlSize(.mini)
                } else {
                    Image(systemName: "arrow.triangle.2.circlepath")
                        .font(.system(size: 10, weight: .bold))
                }
                Text(state.syncing ? "SYNCING" : "SYNC")
                    .font(.system(size: 10, weight: .bold))
                    .tracking(1.2)
            }
            .foregroundColor(canSync ? .black : Theme.ink3)
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(canSync ? Theme.amber : Theme.surface)
            .cornerRadius(3)
        }
        .buttonStyle(.plain)
        .keyboardShortcut("r", modifiers: [.command])
        .disabled(!canSync)
        .help(canSync
              ? "Sync the selected USB · ⌘R"
              : "Plug in a CDJ-export USB to enable Sync")
    }

    private var canSync: Bool {
        !state.syncing && !state.mountedRekordboxUsbs.isEmpty
    }

    private var settingsButton: some View {
        Button { showingSettings = true } label: {
            Image(systemName: "slider.horizontal.3")
                .font(.system(size: 11))
                .foregroundColor(Theme.ink2)
                .frame(width: 22, height: 22)
        }
        .buttonStyle(.plain)
        .help("Tune thresholds · ⌘,")
        .keyboardShortcut(",", modifiers: [.command])
    }
}

// ---------------------------------------------------------------------------
// Bottom status bar - all stats live here, not in the sidebar.
// ---------------------------------------------------------------------------

struct StatusBar: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        HStack(spacing: 18) {
            stat("SESSIONS", "\(state.stats.totalSessions)")
            stat("LIBRARY", "\(state.stats.librarySize)")
            stat("UNIQUE PLAYED", "\(state.stats.totalUniqueTracks)")
            stat("DRIVES SEEN", "\(state.stats.usbDrivesSeen)")
            Spacer()
            if let err = state.lastError {
                Text(err)
                    .font(Type.body)
                    .foregroundColor(Theme.coral)
                    .lineLimit(1)
            }
            HStack(spacing: 6) {
                Circle()
                    .fill(state.mountedRekordboxUsbs.isEmpty ? Theme.ink3 : Theme.cyan)
                    .frame(width: 5, height: 5)
                Text(mountedStatus)
                    .font(Type.body)
                    .foregroundColor(Theme.ink2)
            }
            Text("·").foregroundColor(Theme.ink3)
            Text(syncedAgo)
                .font(Type.body)
                .foregroundColor(Theme.ink2)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 6)
        .background(Theme.bg)
    }

    private func stat(_ label: String, _ value: String) -> some View {
        HStack(spacing: 6) {
            Text(label)
                .font(Type.micro).tracking(0.8)
                .foregroundColor(Theme.ink3)
            Text(value)
                .font(Type.data)
                .foregroundColor(Theme.mono)
        }
    }

    private var mountedStatus: String {
        let m = state.mountedRekordboxUsbs
        if m.isEmpty { return "no USB mounted" }
        if m.count == 1 { return m[0] }
        return "\(m.count) USBs mounted"
    }

    private var syncedAgo: String {
        let last = state.stats.lastSyncAt
        if last == "never" { return "never synced" }
        return "synced " + relative(last)
    }
}
