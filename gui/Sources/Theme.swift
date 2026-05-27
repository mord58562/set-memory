import SwiftUI

/// Restrained palette. Three layered neutrals (bg / surface / hover),
/// two text weights, three deliberate accents with split roles:
///   • cyan  = selected state, brand wordmark
///   • amber = primary action (sync), warnings, attention-needed
///   • coral = danger, deletion candidates
/// Nothing else uses colour. BPM is the only place gradients live
/// (the tempo ramp - meaningful gradient, not decorative).
enum Theme {
    // Backgrounds (three layers, intentional z-axis)
    static let bg          = Color(hex: 0x0B0D11)
    static let surface     = Color(hex: 0x13161C)
    static let hover       = Color(hex: 0x1B1F27)
    static let selected    = Color(hex: 0x202632)
    static let stroke      = Color(hex: 0x232832)
    static let strokeBright = Color(hex: 0x3A4150)

    // Text
    static let ink         = Color(hex: 0xEEF0F4)
    static let ink2        = Color(hex: 0x9CA3B0)   // metadata
    static let ink3        = Color(hex: 0x5B6373)   // dimmed
    static let mono        = Color(hex: 0xC0C6D0)   // numerical readouts

    // Accents - one job each, never doubled up
    static let cyan        = Color(hex: 0x4FB4C7)   // selection, brand, sparkline
    static let amber       = Color(hex: 0xD89B4D)   // action, warning, missing-prep
    static let coral       = Color(hex: 0xD96A6E)   // deletion, error

    // BPM tempo ramp (the one meaningful gradient in the app)
    static let tempoRamp: [(label: String, color: Color)] = [
        ("<100",    Color(hex: 0x6BBBCD)),
        ("100-119", Color(hex: 0x6BAFA0)),
        ("120-127", Color(hex: 0x7DB871)),
        ("128-134", Color(hex: 0xC3BC68)),
        ("135-144", Color(hex: 0xD8AB60)),
        ("145-159", Color(hex: 0xCC8458)),
        ("160+",    Color(hex: 0xC76164)),
    ]

    static func tempoColor(for bpm: Double?) -> Color {
        guard let bpm = bpm else { return ink3 }
        for (label, color) in tempoRamp {
            switch label {
            case "<100":    if bpm < 100   { return color }
            case "100-119": if bpm < 120   { return color }
            case "120-127": if bpm < 128   { return color }
            case "128-134": if bpm < 135   { return color }
            case "135-144": if bpm < 145   { return color }
            case "145-159": if bpm < 160   { return color }
            case "160+":    return color
            default: break
            }
        }
        return ink3
    }
}

extension Color {
    init(hex: UInt32, alpha: Double = 1.0) {
        self.init(.sRGB,
                  red:   Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >>  8) & 0xFF) / 255,
                  blue:  Double( hex        & 0xFF) / 255,
                  opacity: alpha)
    }
}

/// Type scale - 5 deliberate sizes / weights, no more. Display +
/// sectionTitle pair carry visual hierarchy; body is the workhorse;
/// micro is for tracked UPPER category labels; data is the only mono.
enum Type {
    static let display      = Font.system(size: 28, weight: .semibold, design: .default)
    static let sectionTitle = Font.system(size: 14, weight: .semibold, design: .default)
    static let body         = Font.system(size: 12, weight: .regular,  design: .default)
    static let bodyStrong   = Font.system(size: 12, weight: .medium,   design: .default)
    static let micro        = Font.system(size:  9, weight: .semibold, design: .default)
    static let data         = Font.system(size: 11, weight: .regular,  design: .monospaced)
    static let dataMid      = Font.system(size: 12, weight: .medium,   design: .monospaced)
    static let dataLarge    = Font.system(size: 13, weight: .medium,   design: .monospaced)
}

/// Single brand glyph - three vertical bars of varying height, used
/// only in the top-left wordmark. Suggests both waveform (audio) and
/// the dual-data shape (library + sessions). Never reused as an
/// accent or row icon.
struct BrandGlyph: View {
    var body: some View {
        GeometryReader { geo in
            let s = min(geo.size.width, geo.size.height)
            let bw = s * 0.16
            let gap = s * 0.08
            HStack(alignment: .center, spacing: gap) {
                Capsule().fill(Theme.cyan)
                    .frame(width: bw, height: s * 0.55)
                Capsule().fill(Theme.ink)
                    .frame(width: bw, height: s * 0.95)
                Capsule().fill(Theme.cyan)
                    .frame(width: bw, height: s * 0.40)
            }
            .frame(width: geo.size.width, height: geo.size.height)
        }
    }
}

/// Camelot key chip - tiny pill with subtle wheel-position colour.
struct CamelotChip: View {
    let key: String?
    var body: some View {
        let value = key ?? "—"
        let color = key.map(camelotColor(for:)) ?? Theme.ink3
        Text(value)
            .font(Type.data)
            .foregroundColor(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 1)
            .background(color.opacity(0.14))
            .cornerRadius(3)
    }
}

private func camelotColor(for key: String) -> Color {
    let digits = key.prefix(while: { $0.isNumber })
    guard let n = Int(digits), (1...12).contains(n) else { return Theme.ink2 }
    let hue = Double(n - 1) / 12.0
    let isMajor = key.hasSuffix("B")
    return Color(hue: hue,
                 saturation: isMajor ? 0.45 : 0.36,
                 brightness: isMajor ? 0.88 : 0.74)
}

/// Tempo-coloured BPM cell. Used in every row that shows a track.
struct BPMCell: View {
    let bpm: Double?
    var body: some View {
        let display = bpm.map { String(format: "%.0f", $0) } ?? "—"
        Text(display)
            .font(Type.dataMid)
            .foregroundColor(Theme.tempoColor(for: bpm))
            .frame(width: 36, alignment: .trailing)
    }
}

/// Compact pill for the prep audit (missing BPM / key / cues).
struct PrepPill: View {
    let label: String
    var body: some View {
        Text(label.uppercased())
            .font(Type.micro)
            .tracking(0.6)
            .foregroundColor(Theme.amber)
            .padding(.horizontal, 4)
            .padding(.vertical, 1)
            .background(Theme.amber.opacity(0.16))
            .cornerRadius(2)
    }
}
