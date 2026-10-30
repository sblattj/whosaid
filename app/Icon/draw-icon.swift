// Draws the whosaid app icon (1024x1024 PNG). `app/Icon/make-icon.sh` turns it into AppIcon.icns.
// Two overlapping speech bubbles in the Live page colors: yellow = me, blue = them,
// a waveform in the front one. Run: swift app/Icon/draw-icon.swift <out.png>
import AppKit

let size = 1024.0
let out = CommandLine.arguments.dropFirst().first ?? "icon.png"
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size), bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                           bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
func rgb(_ h: UInt32, _ a: CGFloat = 1) -> NSColor {
  NSColor(srgbRed: CGFloat(h >> 16 & 255) / 255, green: CGFloat(h >> 8 & 255) / 255, blue: CGFloat(h & 255) / 255, alpha: a)
}

// macOS icon grid: an 824pt rounded body centred on the 1024 canvas, with a soft drop shadow.
let body = NSRect(x: 100, y: 100, width: 824, height: 824)
let squircle = NSBezierPath(roundedRect: body, xRadius: 185, yRadius: 185)
NSGraphicsContext.saveGraphicsState()
let shadow = NSShadow()
shadow.shadowColor = NSColor.black.withAlphaComponent(0.35)
shadow.shadowBlurRadius = 24
shadow.shadowOffset = NSSize(width: 0, height: -10)
shadow.set()
rgb(0x0B1020).setFill()
squircle.fill()
NSGraphicsContext.restoreGraphicsState()
NSGraphicsContext.saveGraphicsState()
squircle.addClip()
NSGradient(colors: [rgb(0x1B2440), rgb(0x0B1020)])!.draw(in: body, angle: -90)
NSGraphicsContext.restoreGraphicsState()

func bubble(_ r: NSRect, tailLeft: Bool) -> NSBezierPath {
  let p = NSBezierPath(roundedRect: r, xRadius: r.height * 0.42, yRadius: r.height * 0.42)
  let tail = NSBezierPath()
  let x = tailLeft ? r.minX + r.width * 0.2 : r.maxX - r.width * 0.2
  let dir: CGFloat = tailLeft ? -1 : 1
  tail.move(to: NSPoint(x: x - 46 * dir, y: r.minY + 30))
  tail.line(to: NSPoint(x: x + 70 * dir, y: r.minY - 78))
  tail.line(to: NSPoint(x: x + 40 * dir, y: r.minY + 30))
  tail.close()
  // mirrored tails wind the other way; keep the winding the same as the bubble or the overlap cancels out
  p.append(tailLeft ? tail.reversed : tail)
  return p
}

// back bubble: them (blue)
let back = NSRect(x: 330, y: 470, width: 470, height: 300)
NSGradient(colors: [rgb(0x8EC5FF), rgb(0x4F8DF5)])!.draw(in: bubble(back, tailLeft: false), angle: -90)

// front bubble: me (yellow), with a dark rim so it reads in front
let front = NSRect(x: 215, y: 280, width: 500, height: 320)
let fp = bubble(front, tailLeft: true)
rgb(0x0B1020).setStroke()
fp.lineWidth = 28
fp.stroke()
NSGradient(colors: [rgb(0xFFE27A), rgb(0xF5B83D)])!.draw(in: fp, angle: -90)

// waveform in the front bubble
let bars: [CGFloat] = [0.30, 0.62, 1.0, 0.72, 0.45, 0.85, 0.40]
let bw = 30.0, gap = 22.0
let total = Double(bars.count) * bw + Double(bars.count - 1) * gap
var x = front.midX - total / 2
rgb(0x1B2440).setFill()
for h in bars {
  let bh = 190 * h
  NSBezierPath(roundedRect: NSRect(x: x, y: front.midY - bh / 2, width: bw, height: bh), xRadius: bw / 2, yRadius: bw / 2).fill()
  x += bw + gap
}

NSGraphicsContext.current = nil
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print(out)
