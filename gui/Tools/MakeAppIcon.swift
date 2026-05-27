// MakeAppIcon.swift - generates Resources/AppIcon.icns at build time.
// Run via: swift Tools/MakeAppIcon.swift Resources/AppIcon.icns
//
// Design: cassette / dual-USB hybrid on a deep near-black tile with a
// teal accent disc. Two reels suggest the two streams of data Set Memory
// surfaces - your library, and your sessions. Hand-built, no SF Symbols.

import AppKit
import Foundation

let output = CommandLine.arguments.dropFirst().first ?? "AppIcon.icns"
let sizes = [16, 32, 64, 128, 256, 512, 1024]

let bg     = NSColor(srgbRed: 0x0E/255, green: 0x10/255, blue: 0x14/255, alpha: 1)
let ink    = NSColor(srgbRed: 0xEC/255, green: 0xEE/255, blue: 0xF1/255, alpha: 1)
let teal   = NSColor(srgbRed: 0x4E/255, green: 0xCD/255, blue: 0xC4/255, alpha: 1)
let amber  = NSColor(srgbRed: 0xE8/255, green: 0x98/255, blue: 0x49/255, alpha: 1)

func drawIcon(size: CGFloat) -> NSImage {
    let img = NSImage(size: NSSize(width: size, height: size))
    img.lockFocus()
    let ctx = NSGraphicsContext.current!
    ctx.cgContext.setShouldAntialias(true)
    ctx.cgContext.setAllowsAntialiasing(true)

    let inset: CGFloat = size * 0.10
    let radius: CGFloat = size * 0.22
    let rect = NSRect(x: inset, y: inset, width: size - inset * 2, height: size - inset * 2)

    // Tile background
    let bgPath = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
    bg.setFill()
    bgPath.fill()

    // Subtle inner stroke
    let strokeColor = NSColor(srgbRed: 0x3A/255, green: 0x42/255, blue: 0x50/255, alpha: 1)
    strokeColor.setStroke()
    bgPath.lineWidth = max(1, size * 0.006)
    bgPath.stroke()

    // Teal accent corner (top-right notch as a small triangle)
    let notchSize = size * 0.16
    let notchPath = NSBezierPath()
    notchPath.move(to: NSPoint(x: rect.maxX - notchSize, y: rect.maxY))
    notchPath.line(to: NSPoint(x: rect.maxX, y: rect.maxY))
    notchPath.line(to: NSPoint(x: rect.maxX, y: rect.maxY - notchSize))
    notchPath.close()
    teal.withAlphaComponent(0.9).setFill()
    notchPath.fill()

    // Two reels (the data-stream metaphor)
    let centerY = rect.midY - size * 0.02
    let reelSize = size * 0.30
    let reelGap = size * 0.08
    let leftCenter = NSPoint(x: rect.midX - reelSize/2 - reelGap/2, y: centerY)
    let rightCenter = NSPoint(x: rect.midX + reelSize/2 + reelGap/2, y: centerY)

    for (i, c) in [leftCenter, rightCenter].enumerated() {
        let outer = NSBezierPath(ovalIn: NSRect(x: c.x - reelSize/2,
                                                y: c.y - reelSize/2,
                                                width: reelSize, height: reelSize))
        ink.setFill()
        outer.fill()

        // Inner cutout (hub)
        let hub = NSBezierPath(ovalIn: NSRect(x: c.x - reelSize/6,
                                              y: c.y - reelSize/6,
                                              width: reelSize/3, height: reelSize/3))
        bg.setFill()
        hub.fill()

        // Spokes (subtle, suggesting motion)
        let spokeColor = (i == 0) ? teal : amber
        spokeColor.withAlphaComponent(0.85).setStroke()
        for angle in stride(from: 0.0, to: .pi * 2, by: .pi / 3) {
            let ca = CGFloat(Darwin.cos(angle))
            let sa = CGFloat(Darwin.sin(angle))
            let path = NSBezierPath()
            path.move(to: NSPoint(x: c.x + ca * reelSize * 0.18,
                                  y: c.y + sa * reelSize * 0.18))
            path.line(to: NSPoint(x: c.x + ca * reelSize * 0.42,
                                  y: c.y + sa * reelSize * 0.42))
            path.lineWidth = max(1, size * 0.012)
            path.stroke()
        }
    }

    // "SM" wordmark below the reels (only at larger sizes)
    if size >= 64 {
        let label = "S·M" as NSString
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size * 0.10, weight: .semibold),
            .foregroundColor: NSColor(srgbRed: 0x9B/255, green: 0xA2/255, blue: 0xAE/255, alpha: 1),
            .kern: size * 0.014,
        ]
        let labelSize = label.size(withAttributes: attrs)
        label.draw(at: NSPoint(x: rect.midX - labelSize.width/2,
                               y: rect.minY + size * 0.08),
                   withAttributes: attrs)
    }

    img.unlockFocus()
    return img
}

func saveIcns(_ pngsByPixelSize: [Int: Data], to icnsPath: String) throws {
    let tmpDir = NSTemporaryDirectory() + "iconset-\(UUID().uuidString).iconset/"
    try FileManager.default.createDirectory(atPath: tmpDir,
                                            withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(atPath: tmpDir) }
    let layout: [(file: String, px: Int)] = [
        ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
    ]
    for entry in layout {
        guard let png = pngsByPixelSize[entry.px] else { continue }
        try png.write(to: URL(fileURLWithPath: tmpDir + entry.file))
    }
    let task = Process()
    task.launchPath = "/usr/bin/iconutil"
    task.arguments = ["-c", "icns", "-o", icnsPath, tmpDir]
    try task.run()
    task.waitUntilExit()
}

var pngs: [Int: Data] = [:]
for size in sizes {
    let img = drawIcon(size: CGFloat(size))
    guard let tiff = img.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else { continue }
    pngs[size] = png
}

try saveIcns(pngs, to: output)
print("Wrote: \(output)")
