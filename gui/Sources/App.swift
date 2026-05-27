import SwiftUI
import AppKit

@main
struct SetMemoryApp: App {
    @StateObject private var state = AppState()

    init() {
        NSApplication.shared.setActivationPolicy(.regular)
    }

    var body: some Scene {
        WindowGroup("Set Memory") {
            MainView()
                .environmentObject(state)
                .frame(minWidth: 1100, minHeight: 640)
        }
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(replacing: .newItem) {} // No File > New
            CommandGroup(after: .toolbar) {
                Button("Sync now") { state.runSync() }
                    .keyboardShortcut("r", modifiers: [.command])
                    .disabled(state.syncing || state.mountedRekordboxUsbs.isEmpty)
                Button("Refresh from state.db") { state.reloadAll() }
                    .keyboardShortcut("r", modifiers: [.command, .shift])
            }
        }
    }
}
