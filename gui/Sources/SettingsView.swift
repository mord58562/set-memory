import SwiftUI

/// Modal settings sheet. Tunes the analysis thresholds, writes them to
/// config.json (preserving keys the GUI doesn't know about), and
/// re-runs the in-process analysis after save. The launchd agent picks
/// up the same config.json on the next mount, so behaviour stays
/// consistent across GUI tweaks and on-mount runs.
struct SettingsView: View {
    @EnvironmentObject var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var draft = Thresholds()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Settings")
                    .font(.system(size: 16, weight: .semibold))
                Spacer()
                Button("Done") { commit() }
                    .keyboardShortcut(.defaultAction)
            }
            .padding(.horizontal, 20)
            .padding(.top, 18)
            .padding(.bottom, 12)
            Divider()

            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    group(
                        title: "Forgotten favourites",
                        helper: "Tracks you've played a lot, but not recently. Lower the play threshold if the list is empty."
                    ) {
                        stepperRow("Minimum plays", value: $draft.forgottenMinAppearances, range: 1...100, suffix: "×")
                        stepperRow("Not seen in (days)", value: $draft.forgottenDaysSinceLast, range: 7...3650)
                        stepperRow("Limit", value: $draft.forgottenLimit, range: 1...500)
                    }

                    group(
                        title: "Recently added, unplayed",
                        helper: "Window for the buy-regret signal. Days since added."
                    ) {
                        stepperRow("Window (days)", value: $draft.recentlyAddedWindowDays, range: 1...365)
                        stepperRow("Limit", value: $draft.recentlyAddedLimit, range: 1...500)
                    }

                    group(
                        title: "Never played",
                        helper: "Library tracks with zero appearances. Min days since add filters out tracks added in the last week or so."
                    ) {
                        stepperRow("Min days since added", value: $draft.neverPlayedMinDaysSinceAdd, range: 0...3650)
                        stepperRow("Limit", value: $draft.neverPlayedLimit, range: 1...1000)
                    }

                    group(
                        title: "Prep audit",
                        helper: "Library tracks missing BPM, key, or hot cues."
                    ) {
                        stepperRow("Limit", value: $draft.prepLimit, range: 1...2000)
                    }

                    group(
                        title: "Played-together pairs",
                        helper: "Track pairs that appear together in many sessions."
                    ) {
                        stepperRow("Minimum shared sessions", value: $draft.coAppearanceMinSessions, range: 1...100)
                        stepperRow("Limit", value: $draft.coAppearanceLimit, range: 1...500)
                    }

                    group(
                        title: "Possibly deleted",
                        helper: "Tracks Set Memory has seen but haven't appeared in any synced library lately."
                    ) {
                        stepperRow("Days since last in any library", value: $draft.deletedStaleDays, range: 7...3650)
                        stepperRow("Limit", value: $draft.deletedLimit, range: 1...1000)
                    }

                    group(
                        title: "Activity sparkline",
                        helper: "How many months back to plot session activity."
                    ) {
                        stepperRow("Months", value: $draft.sparklineMonths, range: 3...60)
                    }
                }
                .padding(20)
            }

            Divider()

            HStack {
                Button("Restore defaults") {
                    draft = Thresholds()
                }
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Apply") { commit() }
                    .keyboardShortcut("s", modifiers: [.command])
                    .buttonStyle(.borderedProminent)
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 14)
        }
        .frame(width: 520, height: 640)
        .onAppear { draft = state.thresholds }
    }

    @ViewBuilder
    private func group<Content: View>(
        title: String,
        helper: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.system(size: 13, weight: .semibold))
            Text(helper)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            content()
        }
    }

    private func stepperRow(
        _ label: String,
        value: Binding<Int>,
        range: ClosedRange<Int>,
        suffix: String = ""
    ) -> some View {
        HStack {
            Text(label)
                .frame(width: 220, alignment: .leading)
            Spacer()
            Text("\(value.wrappedValue)\(suffix)")
                .font(.system(.body, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 60, alignment: .trailing)
            Stepper("", value: value, in: range)
                .labelsHidden()
        }
    }

    private func commit() {
        state.thresholds = draft
        state.saveThresholdsToDisk()
        dismiss()
    }
}
