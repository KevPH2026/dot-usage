// 把 SF Symbols 渲染成小 PNG，给 Canvas API 的 img 元素用。
//
// 为什么不用文字/emoji：墨水屏只有黑白灰，SF Symbol 是单色矢量，
// 渲染成 28×28 PNG 后缩到 14px 显示，边缘干净、单文件几百字节。
//
// 编译：
//   swiftc -O render_icons.swift -o render_icons
// 用法：
//   ./render_icons <输出目录>
// 映射在下面的 symbols 里；要换图标改这一处即可。

import AppKit
import Foundation

// 按各家的标志物选形，别用抽象几何充数：
//   智谱 GLM 是六边形 → hexagon（用**轮廓**版，实心在小尺寸下就是个黑块）
//   MiniMax            → bolt.fill（海兔太细，14px 下糊成一团）
//   DeepSeek logo 是鲸鱼 → fish
//   ChatGPT/OpenAI      → sparkles（四角星，小尺寸辨识度最高）
//   Grok              → flame.fill
// 这里只画 SF Symbol 类图标。
// 各家的**品牌 logo** 由 make_brand_icons.py 从本机 App bundle 里抠（更真）。
let symbols: [String: String] = [
    "grok": "bolt.horizontal.fill",  // Grok 的 logo 是渐变球，黑白化没意义，用闪电占位
    "summary": "chart.bar.fill",    // 构成明细
    "tibo": "megaphone.fill",       // Tibo 重置公告
]

let size = 36  // 像素（2x 渲染，页面上按 18px 显示）

let args = CommandLine.arguments
guard args.count > 1 else {
    FileHandle.standardError.write("用法: render_icons <输出目录>\n".data(using: .utf8)!)
    exit(1)
}
let outDir = URL(fileURLWithPath: args[1], isDirectory: true)
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)

for (key, symbolName) in symbols.sorted(by: { $0.key < $1.key }) {
    guard let base = NSImage(systemSymbolName: symbolName, accessibilityDescription: nil) else {
        FileHandle.standardError.write("跳过不存在的符号: \(symbolName)\n".data(using: .utf8)!)
        continue
    }
    let cfg = NSImage.SymbolConfiguration(pointSize: 19, weight: .bold)
    guard let icon = base.withSymbolConfiguration(cfg) else { continue }

    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    ) else { continue }

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    NSColor.black.set()
    let rect = NSRect(x: 0, y: 0, width: size, height: size)
    icon.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1.0)
    NSGraphicsContext.restoreGraphicsState()

    guard let png = rep.representation(using: .png, properties: [:]) else { continue }
    let path = outDir.appendingPathComponent("\(key).png")
    try? png.write(to: path)
    print("\(key).png  ← \(symbolName)  \(png.count) bytes")
}
