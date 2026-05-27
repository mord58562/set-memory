// MakeAppIcon.swift - generates Resources/AppIcon.icns
// Concept: a single off-white circle (record / jog wheel) on a deep
// near-black tile, with one cyan dot at 2 o'clock representing a
// saved memory cue. Three elements, readable at 16pt. No spokes,
// no wordmark, no notch - just the metaphor.

import AppKit
import Foundation

let output = CommandLine.arguments.dropFirst().first ?? "AppIcon.icns"
let sizes = [16, 32, 64, 128, 256, 512, 1024]

let bg   = NSColor(srgbRed: 0x0B/255, green: 0x0D/255, blue: 0x11/255, alpha: 1)
let ink  = NSColor(srgbRed: 0xEE/255, green: 0xF0/255, blue: 0xF4/255, alpha: 1)
let cyan = NSColor(srgbRed: 0x4F/255, green: 0xB4/255, blue: 0xC7/255, alpha: 1)
let strokeColor = NSColor(srgbRed: 0x23/255, green: 0x28/255, blue: 0x32/255, alpha: 1)

func drawIcon(size: CGFloat) -> NSImage {
    let img = NSImage(size: NSSize(width: size, height: size))
    img.lockFocus()
    NSGraphicsContext.current!.cgContext.setShouldAntialias(true)

    // Tile - rounded square
    let inset: CGFloat = size * 0.10
    let radius: CGFloat = size * 0.225
    let rect = NSRect(x: inset, y: inset, width: size - inset * 2, height: size - inset * 2)
    let tile = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)
    bg.setFill()
    tile.fill()
    strokeColor.setStroke()
    tile.lineWidth = max(0.5, size * 0.004)
    tile.stroke()

    // Record disc - thick ring (not solid; suggests vinyl)
    let centre = NSPoint(x: rect.midX, y: rect.midY)
    let discR  = rect.width * 0.36
    let hubR   = rect.width * 0.06
    let ring = NSBezierPath(ovalIn: NSRect(x: centre.x - discR, y: centre.y - discR,
                                           width: discR * 2, height: discR * 2))
    ink.setFill()
    ring.fill()
    // Centre hole
    let hub = NSBezierPath(ovalIn: NSRect(x: centre.x - hubR, y: centre.y - hubR,
                                          width: hubR * 2, height: hubR * 2))
    bg.setFill()
    hub.fill()
    // Single faint groove (echo of vinyl grooves) - 8% inset from outer edge
    if size >= 64 {
        let gr1R = discR * 0.78
        let groove = NSBezierPath(ovalIn: NSRect(x: centre.x - gr1R, y: centre.y - gr1R,
                                                 width: gr1R * 2, height: gr1R * 2))
        bg.withAlphaComponent(0.55).setStroke()
        groove.lineWidth = max(0.5, size * 0.008)
        groove.stroke()
    }

    // The memory cue - one cyan dot at 1 o'clock (NE), slightly inside
    // the disc rim. Single saturated mark; the whole concept of the app.
    let angle: CGFloat = .pi * 0.30          // ~54deg from x-axis = ~1 o'clock
    let cueR  = discR * 0.86                 // sit just inside the rim
    let dotR  = max(1.2, size * 0.045)
    let dotCentre = NSPoint(x: centre.x + cos(angle) * cueR,
                            y: centre.y + sin(angle) * cueR)
    let dot = NSBezierPath(ovalIn: NSRect(x: dotCentre.x - dotR,
                                          y: dotCentre.y - dotR,
                                          width: dotR * 2, height: dotR * 2))
    cyan.setFill()
    dot.fill()

    // At very large sizes only, a faint thin line from hub to the cue
    // dot - reading head pointing at the memory. Subtle.
    if size >= 256 {
        let line = NSBezierPath()
        line.move(to: NSPoint(x: centre.x + cos(angle) * (hubR + size * 0.005),
                              y: centre.y + sin(angle) * (hubR + size * 0.005)))
        line.line(to: NSPoint(x: centre.x + cos(angle) * (cueR - dotR - size * 0.005),
                              y: centre.y + sin(angle) * (cueR - dotR - size * 0.005)))
        cyan.withAlphaComponent(0.35).setStroke()
        line.lineWidth = max(0.5, size * 0.005)
        line.stroke()
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
