// 菜单栏小工具：显示当前 AI 用量配额，支持手动刷新 + 自动刷新。
//
// 为什么不用 SwiftUI 的 MenuBarExtra：
//   本机 macOS（Darwin 27）上 MenuBarExtra 的状态项不渲染，最小复现也一样，
//   所以改用 AppKit 原生 NSStatusItem + NSPopover 承载 SwiftUI 面板。
//
// 编译（不需要 Xcode 工程）：
//   swiftc -O -parse-as-library -target arm64-apple-macos13.0 DotUsageBar.swift -o build/DotUsageBar
//
// ── 这个 App 只做一件事：读状态文件 ────────────────────────────────
// 数据来自 dot-usage 采集器写下的 state.json
// （~/.local/state/dot-usage/state.json）。
//
// **它跟推送链路完全无关**：不认识任何设备、不碰任何密钥、
// 不知道数据最后被送到哪儿去。采集器把状态写下来，这个 App 把状态显示出来，
// 中间隔着一个文件契约。加新的输出目标，这个 App 一行都不用改。
//
// 手动刷新 = 跑一次 `python3 -m dotusage.cli collect`（用已安装的 python 包）。
// 推送到各个输出目标是采集器自己的事，这里不碰。

import SwiftUI
import AppKit
import Combine

// MARK: - 数据模型（对应 state.json）

/// 一行用量
struct UsageItem: Codable, Identifiable {
    // 必须唯一：同一平台的不同窗口（5 小时 / 周）label 相同，
    // 只用 source+label 会让 SwiftUI 的 ForEach 把两行当成同一个。
    let uid: String
    var id: String { uid }
    let source: String
    let label: String
    let kind: String
    let used_percent: Double?
    let headline: String
    let detail: String
    let bar_width: Double
    let icon: String?
}

/// state.json 的契约（字段都是可选的，缺一个不该整个文件读不出来）
struct State: Codable {
    /// 契约版本
    let version: Int?
    /// 采集时间
    let generated_at: String?
    /// 图标目录的绝对路径（采集器给的）
    let icon_dir: String?
    /// 消耗数据的统计窗口标签（近24h / 近7天 / …）
    /// 采集器给，不同源窗口不一致时为空串
    let window: String?
    /// 原始明细
    let rows: [UsageItem]?
    /// 按平台合并后的一行一平台，优先显示
    let platforms: [UsageItem]?
    /// 按模型维度：窗口内（默认近 24h）+ 历史累计
    let models: [ModelUsage]?
    /// 近 30 天按天 × 模型的消耗与折算成本
    let daily: DailyTrend?
    /// 等效成本（按官方 API 单价折算）
    let cost: CostSummary?

    /// 显示用的窗口说法。拿不到就说「今天」——宁可保守也别写错。
    var windowLabel: String {
        let w = (window ?? "").trimmingCharacters(in: .whitespaces)
        return w.isEmpty ? "今天" : w
    }

    var displayRows: [UsageItem] {
        if let p = platforms, !p.isEmpty { return p }
        return rows ?? []
    }
}

/// 一个模型在一段时间里的 token 用量
struct ModelUsage: Codable, Identifiable {
    let platform: String?
    let model: String?
    let icon: String?
    let source: String?
    let today_tokens: Int?
    let today_calls: Int?
    let today_cost: Double?
    let total_tokens: Int?
    let total_calls: Int?
    let total_cost: Double?
    /// 这个成本是按官方 API 单价**折算**的，不是账单上的钱
    let estimated: Bool?

    var id: String { (platform ?? "?") + "|" + (model ?? "?") }
}

/// 每日趋势里的一条模型曲线
struct TrendSeries: Codable, Identifiable {
    let model: String?
    let platform: String?
    let icon: String?
    /// 每天的 token
    let tokens: [Int]?
    /// 每天的折算成本（元）
    let cost: [Double?]?
    let total: Int?
    let total_cost: Double?

    var id: String { model ?? "?" }
}

/// 近 N 天按天 × 模型的消耗（菜单栏「趋势」tab）
struct DailyTrend: Codable {
    let days: [String]?
    let series: [TrendSeries]?
    let by_day: [Int]?
    let by_day_cost: [Double]?
    let total_cost: Double?
    let currency: String?
}

/// 等效成本汇总
struct CostSummary: Codable {
    let currency: String?
    /// 当前窗口的折算成本
    let total: Double?
    /// 全历史累计折算成本
    let total_cumulative: Double?
    let estimated: Bool?
    let by_model: [CostItem]?
    let by_model_cumulative: [CostItem]?
}

struct CostItem: Codable, Identifiable {
    let model: String?
    let platform: String?
    let cost: Double?
    let tokens: Int?
    let estimated: Bool?

    var id: String { model ?? "?" }
}

/// 离屏渲染模式。
///
/// ImageRenderer 抓不到动画中间态（会截到一半），
/// 所以自查排版时要把动画关掉，渲染出**最终**画面。
enum RenderMode {
    nonisolated(unsafe) static var isOffscreen = false
}

/// 面板视图切换
enum PanelTab: String, CaseIterable, Identifiable {
    case quota = "配额"
    case model = "模型"
    case trend = "趋势"

    var id: String { rawValue }
}

/// token 数压成短字符串：12.3M / 2.31B / 845K
func compactTokens(_ n: Int?) -> String {
    guard let n else { return "—" }
    if n == 0 { return "—" }
    let v = Double(n)
    if v >= 1_000_000_000 { return String(format: "%.2fB", v / 1_000_000_000) }
    if v >= 1_000_000 { return String(format: "%.1fM", v / 1_000_000) }
    if v >= 1_000 { return String(format: "%.1fK", v / 1_000) }
    return "\(n)"
}

// MARK: - 图标

/// 平台图标（透明底黑形，模板图片跟着系统前景色走）
///
/// 目录按这个顺序找：
///   1. state.json 里的 icon_dir（采集器算好的绝对路径）
///   2. 环境变量 DOT_USAGE_ICON_DIR
///   3. 默认 ~/Tools/dot-usage/icons/png/
enum IconStore {
    static let fallbackDir = NSString(string: "~/Tools/dot-usage/icons/png").expandingTildeInPath

    /// 当前图标目录。采集器读到 state.json 后会把它改成实际路径。
    static var directory: String = fallbackDir

    static func resolve(stateIconDir: String?) -> String {
        if let d = stateIconDir, !d.isEmpty { return d }
        if let d = ProcessInfo.processInfo.environment["DOT_USAGE_ICON_DIR"], !d.isEmpty {
            return d
        }
        return fallbackDir
    }

    static func image(_ name: String?) -> NSImage? {
        guard let name, !name.isEmpty else { return nil }
        let path = (directory as NSString).appendingPathComponent("\(name).png")
        guard let img = NSImage(contentsOfFile: path) else { return nil }
        img.isTemplate = true
        return img
    }
}

// MARK: - 安装配置

/// install.sh 写的安装配置。
///
/// 为什么走文件不走环境变量：这个 App 是被 launchd 用 `open -g` 拉起来的，
/// LaunchServices 给的进程环境是登录会话的环境，**不继承 LaunchAgent 的 EnvironmentVariables**。
/// 想把「用哪个 python」「源码在哪」告诉它，只能落成一个文件。
///
/// 位置：~/Library/Application Support/DotUsageBar/config.json
/// 内容：{"python": "/opt/homebrew/bin/python3.14", "src_dir": "/path/to/dot-usage/src"}
enum InstallConfig {
    static var path: String {
        NSString(string: "~/Library/Application Support/DotUsageBar/config.json").expandingTildeInPath
    }

    static func load() -> [String: String] {
        guard let data = FileManager.default.contents(atPath: path),
              let obj = try? JSONSerialization.jsonObject(with: data),
              let dict = obj as? [String: Any] else { return [:] }
        var out: [String: String] = [:]
        for (k, v) in dict {
            if let s = v as? String, !s.isEmpty { out[k] = s }
        }
        return out
    }
}

// MARK: - 采集器调用

/// 跑采集命令（`python3 -m dotusage.cli collect`）。
///
/// **只跑已安装的 python 包**，不依赖某个源码目录路径：
/// 先找一个能 `import dotusage` 的 python，找不到再退到
/// 环境变量 DOT_USAGE_SRC_DIR 指定的目录（源码方式跑）。
enum Collector {
    /// 候选解释器。带 prefixArgs 的是「用 env 去 PATH 里找」。
    struct Candidate {
        let executable: String
        let prefixArgs: [String]
        init(_ executable: String, _ prefixArgs: [String] = []) {
            self.executable = executable
            self.prefixArgs = prefixArgs
        }
    }

    static let pythonCandidates: [Candidate] = [
        Candidate("/opt/homebrew/bin/python3"),
        Candidate("/usr/local/bin/python3"),
        Candidate("/usr/bin/env", ["python3"]),   // 按 PATH 找，能命中 pyenv / conda 之类
        Candidate(NSString(string: "~/.pyenv/shims/python3").expandingTildeInPath),
        Candidate("/usr/bin/python3"),             // 系统自带的 3.9，多半不合格，放最后
    ]

    /// 找到的可用解释器 + 它需要的环境变量
    struct Resolved {
        let python: String
        let prefixArgs: [String]
        let env: [String: String]
    }

    static func resolve() -> Resolved? {
        let base = ProcessInfo.processInfo.environment
        let install = InstallConfig.load()
        var candidates = pythonCandidates
        // 环境变量优先，其次 install.sh 写下来的配置
        let wantedPython = base["DOT_USAGE_PYTHON"].flatMap { $0.isEmpty ? nil : $0 }
            ?? install["python"]
        if let p = wantedPython {
            candidates.insert(Candidate(p), at: 0)
        }

        // 1) 已安装的包：python -c "import dotusage" 能过，且版本够新
        for c in candidates {
            if canRun(c, env: base) {
                return Resolved(python: c.executable, prefixArgs: c.prefixArgs, env: base)
            }
        }

        // 2) 兜底：源码目录（DOT_USAGE_SRC_DIR 或 install.sh 写的 src_dir）
        let src = base["DOT_USAGE_SRC_DIR"].flatMap { $0.isEmpty ? nil : $0 } ?? install["src_dir"]
        if let src {
            var env = base
            let existing = env["PYTHONPATH"].map { $0 + ":" } ?? ""
            env["PYTHONPATH"] = existing + src
            for c in candidates {
                if canRun(c, env: env) {
                    return Resolved(python: c.executable, prefixArgs: c.prefixArgs, env: env)
                }
            }
        }
        return nil
    }

    /// 这个解释器能不能用：能 import dotusage，且 Python ≥ 3.11
    /// （3.11 才有标准库 tomllib，读不了数据源配置）。
    private static func canRun(_ c: Candidate, env: [String: String]) -> Bool {
        if c.prefixArgs.isEmpty {
            guard FileManager.default.isExecutableFile(atPath: c.executable) else { return false }
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: c.executable)
        p.arguments = c.prefixArgs
            + ["-c", "import sys, dotusage; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"]
        p.environment = env
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            p.waitUntilExit()
            return p.terminationStatus == 0
        } catch {
            return false
        }
    }

    /// 跑一次采集。返回 (退出码, 输出)
    static func collect() -> (status: Int32, output: String) {
        guard let r = resolve() else {
            return (-1, "找不到能 import dotusage 的 Python 3.11+。装一下：pip install -e <仓库目录>；或设 DOT_USAGE_PYTHON / DOT_USAGE_SRC_DIR")
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: r.python)
        proc.arguments = r.prefixArgs + ["-m", "dotusage.cli", "collect"]
        proc.environment = r.env
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        do {
            try proc.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            proc.waitUntilExit()
            let out = String(data: data, encoding: .utf8) ?? ""
            return (proc.terminationStatus, out)
        } catch {
            return (-1, error.localizedDescription)
        }
    }
}

// MARK: - 状态

final class AppModel: ObservableObject {
    @Published var state: State?
    @Published var busy: Bool = false
    @Published var lastError: String = ""
    /// 面板视图：配额 / 模型 / 趋势
    @Published var viewMode: PanelTab = .quota
    /// 趋势图的动画进度（0→1，把线「画出来」）
    @Published var chartReveal: CGFloat = 1

    /// 状态变化时回调，用于刷新菜单栏标题
    var onChange: (() -> Void)?

    private let statePath = NSString(string: "~/.local/state/dot-usage/state.json").expandingTildeInPath

    init() {
        load()
    }

    /// 读不到 state.json —— 说明还没采集过
    var hasState: Bool { state != nil }

    var topTitle: String {
        guard let s = state else { return "—" }
        let rows = s.rows ?? []
        if let quota = rows.first(where: { $0.kind == "quota" }),
           let pct = quota.used_percent {
            return String(format: "%.0f%%", pct)
        }
        return String(rows.count)
    }

    var updatedAt: String {
        guard let s = state, let t = s.generated_at else { return "未采集" }
        return t
    }

    func load() {
        guard let data = FileManager.default.contents(atPath: statePath),
              let st = try? JSONDecoder().decode(State.self, from: data) else {
            state = nil
            onChange?()
            return
        }
        state = st
        IconStore.directory = IconStore.resolve(stateIconDir: st.icon_dir)
        onChange?()
    }

    /// 重新采集一次（跑已安装的 python 包）
    func refresh(silent: Bool = false) {
        guard !busy else { return }
        busy = true
        if !silent { lastError = "" }
        onChange?()

        let (status, out) = Collector.collect()
        if status != 0 {
            lastError = out.split(separator: "\n").last.map(String.init) ?? "采集失败"
        }
        // 落一行日志到 stderr：launchd 把 App 的 stderr 收进 menubar.err.log，
        // 出问题时不用猜到底是哪一步炸的。
        FileHandle.standardError.write(
            "dot-usage: collect status=\(status) \(out.split(separator: "\n").last.map(String.init) ?? "")\n"
                .data(using: .utf8)!
        )
        load()
        busy = false
        onChange?()
    }

    /// 趋势图进场动画：把 reveal 从 0 推到 1，线就一��画出来。
    ///
    /// 离屏渲染时关掉——ImageRenderer 抓不到动画中间态，
    /// 自查排版要看到的是**最终**画面。
    func startChartAnimation() {
        guard !RenderMode.isOffscreen else { return }
        chartReveal = 0
        DispatchQueue.main.async {
            withAnimation(.easeOut(duration: 0.8)) { self.chartReveal = 1 }
        }
    }

    func openPreview() {
        let path = ProcessInfo.processInfo.environment["DOT_USAGE_PREVIEW"]
            ?? NSString(string: "~/Tools/dot-usage/preview/card-preview.html").expandingTildeInPath
        if FileManager.default.fileExists(atPath: path) {
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        }
    }

    func openLog() {
        let path = NSString(string: "~/.local/state/dot-usage/dot-usage.log").expandingTildeInPath
        if FileManager.default.fileExists(atPath: path) {
            NSWorkspace.shared.open(URL(fileURLWithPath: path))
        }
    }

    func copySummary() {
        guard let s = state else { return }
        let text: String
        if viewMode == .model {
            text = (s.models ?? []).map {
                "\($0.model ?? "?")  \(s.windowLabel) \(compactTokens($0.today_tokens))  累计 \(compactTokens($0.total_tokens))  \($0.total_calls ?? 0)次"
            }.joined(separator: "\n")
        } else {
            text = s.displayRows.map { "\($0.label)  \($0.headline)  \($0.detail)" }.joined(separator: "\n")
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}


// MARK: - 趋势图

/// 把折线画平滑。
///
/// 直线段连起来太生硬，用 Catmull-Rom 样条转成三次贝塞尔：
/// 每个点用前后两个邻居算出控制柄，曲线就顺了，
/// 而且不会像样条拟合那样冲出数据范围。
func smoothPath(_ points: [CGPoint], tension: CGFloat = 0.2) -> Path {
    var path = Path()
    guard points.count > 1 else {
        if let p = points.first { path.move(to: p) }
        return path
    }
    path.move(to: points[0])
    for i in 0..<(points.count - 1) {
        let p0 = points[max(i - 1, 0)]
        let p1 = points[i]
        let p2 = points[i + 1]
        let p3 = points[min(i + 2, points.count - 1)]
        let c1 = CGPoint(
            x: p1.x + (p2.x - p0.x) * tension,
            y: p1.y + (p2.y - p0.y) * tension
        )
        let c2 = CGPoint(
            x: p2.x - (p3.x - p1.x) * tension,
            y: p2.y - (p3.y - p1.y) * tension
        )
        path.addCurve(to: p2, control1: c1, control2: c2)
    }
    return path
}

/// 每日 token 折线图（按模型多条线，手绘，不用图表库）。
///
/// 所有系列共用一套坐标——不同量级的模型画在一起才看得出谁在烧。
struct TrendChartView: View {
    let days: [String]
    let series: [TrendSeries]
    let height: CGFloat
    /// 0→1 的显示进度（用来把线「画出来」）
    var reveal: CGFloat = 1

    /// 折线的配色（冷暖交替，一眼能分开）
    private static let palette: [Color] = [
        Color(red: 0.20, green: 0.45, blue: 0.95),
        Color(red: 0.10, green: 0.65, blue: 0.40),
        Color(red: 0.55, green: 0.35, blue: 0.90),
        Color(red: 0.90, green: 0.25, blue: 0.25),
        Color(red: 0.95, green: 0.55, blue: 0.10),
        Color(red: 0.10, green: 0.70, blue: 0.72),
    ]

    private var maxValue: Double {
        let m = series.flatMap { $0.tokens ?? [] }.map { Double($0) }.max() ?? 1
        return max(m, 1)
    }

    private func points(_ vals: [Int], w: CGFloat, h: CGFloat) -> [CGPoint] {
        let n = max(days.count, 1)
        guard n > 1 else { return [] }
        return vals.enumerated().map { i, v in
            CGPoint(
                x: w * Double(i) / Double(n - 1),
                y: h * (1.0 - Double(v) / maxValue)
            )
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            GeometryReader { geo in
                let w = geo.size.width
                let h = geo.size.height
                ZStack(alignment: .topLeading) {
                    // 纵轴刻度：顶部标一下最大值，不然不知道线多高
                    Text(compactTokens(Int(maxValue)))
                        .font(.system(size: 8))
                        .foregroundStyle(.secondary)
                        .padding(.leading, 2)
                    // 横向参考线
                    ForEach(0..<3) { i in
                        let y = h * Double(i) / 2.0
                        Path { p in
                            p.move(to: CGPoint(x: 0, y: y))
                            p.addLine(to: CGPoint(x: w, y: y))
                        }
                        .stroke(Color.secondary.opacity(0.18), style: StrokeStyle(lineWidth: 0.5, dash: [2, 3]))
                    }
                    // 每条曲线
                    ForEach(Array(series.enumerated()), id: \.element.id) { idx, s in
                        let pts = points(s.tokens ?? [], w: w, h: h)
                        if pts.count > 1 {
                            smoothPath(pts)
                                .trimmedPath(from: 0, to: reveal)
                                .stroke(
                                    Self.palette[idx % Self.palette.count],
                                    style: StrokeStyle(lineWidth: 1.4, lineCap: .round, lineJoin: .round)
                                )
                        }
                    }
                }
            }
            .frame(height: height)

            // x 轴：首尾两个日期，中间不标（挤）
            HStack {
                Text(days.first ?? "")
                Spacer()
                Text(days.last ?? "")
            }
            .font(.system(size: 9))
            .foregroundStyle(.secondary)

            // 图例
            VStack(alignment: .leading, spacing: 2) {
                ForEach(Array(series.enumerated()), id: \.element.id) { idx, s in
                    HStack(spacing: 5) {
                        RoundedRectangle(cornerRadius: 1.5)
                            .fill(Self.palette[idx % Self.palette.count])
                            .frame(width: 9, height: 3)
                        Text(s.model ?? "?")
                            .font(.system(size: 9))
                            .lineLimit(1)
                        Spacer(minLength: 4)
                        Text(compactTokens(s.total))
                            .font(.system(size: 9, weight: .medium))
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    }
                }
            }
        }
    }
}

/// 累加：把「每天的值」变成「到这天为止一共多少」。
func runningSum(_ values: [Double]) -> [Double] {
    var out: [Double] = []
    var acc = 0.0
    for v in values {
        acc += v
        out.append(acc)
    }
    return out
}

/// 折线下方补到基线的闭合路径（用来填面积）。
func areaPath(_ points: [CGPoint], h: CGFloat) -> Path {
    var area = smoothPath(points)
    if let last = points.last, let first = points.first {
        area.addLine(to: CGPoint(x: last.x, y: h))
        area.addLine(to: CGPoint(x: first.x, y: h))
        area.closeSubpath()
    }
    return area
}

/// 累计曲线（单线 + 面积填充）。
///
/// 上面那张是「每天烧多少」，这张是「到今天为止一共烧了多少」——
/// 后者更能看出这段时间的烧钱速度。
struct CumulativeChartView: View {
    let values: [Double]
    let height: CGFloat
    var color: Color = Color(red: 0.20, green: 0.45, blue: 0.95)
    /// 0→1 的显示进度
    var reveal: CGFloat = 1
    /// 刻度/端点怎么写：token 还是金额
    var isMoney: Bool = false

    private var maxValue: Double { max(values.max() ?? 1, 0.0001) }

    private func axisLabel(_ v: Double) -> String {
        isMoney ? "¥\(compactMoney(v))" : compactTokens(Int(v))
    }

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            let h = geo.size.height
            let n = max(values.count, 1)
            let pts: [CGPoint] = n > 1 ? values.enumerated().map { i, v in
                CGPoint(
                    x: w * Double(i) / Double(n - 1),
                    y: h * (1.0 - v / maxValue)
                )
            } : []

            ZStack(alignment: .topLeading) {
                if pts.count > 1 {
                    // 面积
                    areaPath(pts, h: h)
                        .trimmedPath(from: 0, to: reveal)
                        .fill(
                            LinearGradient(
                                colors: [color.opacity(0.28), color.opacity(0.02)],
                                startPoint: .top, endPoint: .bottom
                            )
                        )
                    // 线
                    smoothPath(pts)
                        .trimmedPath(from: 0, to: reveal)
                        .stroke(color, style: StrokeStyle(lineWidth: 1.6, lineCap: .round, lineJoin: .round))
                    // 末端一个点
                    if let last = pts.last {
                        Circle()
                            .fill(color)
                            .frame(width: 3.5, height: 3.5)
                            .position(last)
                            .opacity(Double(reveal))
                    }
                }
                Text(axisLabel(maxValue))
                    .font(.system(size: 8))
                    .foregroundStyle(.secondary)
                    .padding(.leading, 2)
            }
        }
        .frame(height: height)
    }
}

/// 「趋势」tab 的内容：每日折线 + 累计曲线 + 累计价格
struct TrendPanelView: View {
    let daily: DailyTrend?
    let cost: CostSummary?
    /// 动画进度（0→1）
    var reveal: CGFloat = 1

    /// 累计 token 曲线（每天的量跑累加）
    private var cumulativeTokensCurve: [Double] {
        runningSum((daily?.by_day ?? []).map { Double($0) })
    }

    private var cumulativeTokens: Double {
        cumulativeTokensCurve.last ?? 0
    }

    /// 累计等效成本（同口径：30 天）
    private var cumulativeCost: Double {
        runningSum(daily?.by_day_cost ?? []).last ?? 0
    }

    var body: some View {
        let days = daily?.days ?? []
        let series = daily?.series ?? []
        if days.isEmpty || series.isEmpty {
            Text("还没有趋势数据，点下面的刷新")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                // 累计 token（大数字）· 等效成本放次要位置
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("累计 token")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                    Text(compactTokens(Int(cumulativeTokens)))
                        .font(.system(size: 17, weight: .bold))
                        .monospacedDigit()
                    Spacer()
                    Text("近\(days.count)天")
                        .font(.system(size: 9))
                        .foregroundStyle(.secondary)
                }
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Text("等效成本")
                        .font(.system(size: 9))
                        .foregroundStyle(.secondary)
                    Text("¥\(compactMoney(cumulativeCost))")
                        .font(.system(size: 11, weight: .semibold))
                        .monospacedDigit()
                    if cost?.estimated == true {
                        Text("按 API 单价折算")
                            .font(.system(size: 9))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                }

                // 每日消耗
                VStack(alignment: .leading, spacing: 3) {
                    Text("每日消耗")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(.secondary)
                    TrendChartView(days: days, series: series, height: 78, reveal: reveal)
                }

                // 累计 token 曲线（by_day 是每天的，这里跑累加）
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text("累计 token")
                            .font(.system(size: 9, weight: .medium))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text(compactTokens(Int(cumulativeTokens)))
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    }
                    CumulativeChartView(
                        values: cumulativeTokensCurve,
                        height: 46,
                        reveal: reveal
                    )
                }
            }
        }
    }
}

/// 金额压成短串：1234 → ¥1234 / 12345 → ¥1.23万
func compactMoney(_ n: Double?) -> String {
    guard let n, n > 0 else { return "—" }
    if n >= 10_000 { return String(format: "%.1f万", n / 10_000) }
    if n >= 1000 { return String(format: "%.0f", n) }
    return String(format: "%.1f", n)
}

// MARK: - 面板界面

/// 平台图标行
struct RowView: View {
    let item: UsageItem

    var body: some View {
        HStack(alignment: .top, spacing: 7) {
            if let img = IconStore.image(item.icon) {
                Image(nsImage: img)
                    .renderingMode(.template)
                    .resizable()
                    .frame(width: 15, height: 15)
            }
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(item.label)
                        .font(.system(size: 12, weight: .semibold))
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    Text(item.headline)
                        .font(.system(size: 13, weight: .bold))
                        .monospacedDigit()
                }
                if item.kind == "platform", let pct = item.used_percent {
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            RoundedRectangle(cornerRadius: 2).fill(Color.secondary.opacity(0.18))
                            RoundedRectangle(cornerRadius: 2)
                                .fill(Color.primary)
                                .frame(width: max(2, geo.size.width * pct / 100))
                        }
                    }
                    .frame(height: 4)
                }
                if !item.detail.isEmpty {
                    Text(item.detail)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
        }
        .padding(.vertical, 3)
    }
}

/// 模型行：模型名 + 窗口内的消耗 + 历史累计
struct ModelRowView: View {
    let item: ModelUsage
    /// 统计窗口标签（近24h / 近7天 / …），从 state.json 读
    var windowLabel: String = "今天"

    var body: some View {
        HStack(alignment: .top, spacing: 7) {
            if let img = IconStore.image(item.icon) {
                Image(nsImage: img)
                    .renderingMode(.template)
                    .resizable()
                    .frame(width: 15, height: 15)
            }
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(item.model ?? "")
                        .font(.system(size: 12, weight: .semibold))
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    Text("\(windowLabel) \(compactTokens(item.today_tokens))")
                        .font(.system(size: 12, weight: .bold))
                        .monospacedDigit()
                        .foregroundStyle((item.today_tokens ?? 0) > 0 ? Color.primary : Color.secondary)
                }
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(item.platform ?? "")
                    Spacer(minLength: 4)
                    Text("累计 \(compactTokens(item.total_tokens)) · \(item.total_calls ?? 0) 次")
                        .monospacedDigit()
                }
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .lineLimit(1)
            }
        }
        .padding(.vertical, 3)
    }
}

/// 自绘的「配额 / 模型」切换按钮
///
/// 不用系统的 segmented Picker：它是 AppKit 控件，
/// 离屏渲染（ImageRenderer）画不出来，自查排版时就是个黄块。
/// 自己画的话纯 SwiftUI，渲染正常，样式也能自己控。
struct SegmentedTab: View {
    @Binding var selection: PanelTab

    var body: some View {
        HStack(spacing: 0) {
            ForEach(PanelTab.allCases) { tab in
                Button {
                    selection = tab
                } label: {
                    Text(tab.rawValue)
                        .font(.system(size: 11, weight: selection == tab ? .semibold : .regular))
                        .foregroundStyle(selection == tab ? Color.primary : Color.secondary)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 4)
                        .background(
                            RoundedRectangle(cornerRadius: 5)
                                .fill(selection == tab ? Color.primary.opacity(0.15) : Color.clear)
                        )
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(2)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.12)))
    }
}

struct PanelView: View {
    @ObservedObject var model: AppModel

    /// 模型太多时只显示前 N 个，剩下的用一行提示
    private let maxModelRows = 12

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("AI 用量").font(.system(size: 13, weight: .bold))
                Spacer()
                Text(model.updatedAt).font(.system(size: 10)).foregroundStyle(.secondary)
            }

            SegmentedTab(selection: $model.viewMode)

            Divider()

            Group {
            if !model.hasState {
                // 读不到状态文件 = 还没采集过
                Text("还没有状态文件")
                    .font(.system(size: 12, weight: .semibold))
                Text("先跑一次 dot-usage collect")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            } else if model.viewMode == .quota {
                if let rows = model.state?.displayRows, !rows.isEmpty {
                    ForEach(rows) { RowView(item: $0) }
                } else {
                    Text("还没有数据，点下面的刷新")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            } else if model.viewMode == .trend {
                TrendPanelView(
                    daily: model.state?.daily,
                    cost: model.state?.cost,
                    reveal: model.chartReveal
                )
                .onAppear { model.startChartAnimation() }
            } else {
                let models = model.state?.models ?? []
                let totalTokens = models.reduce(0) { $0 + (Int($1.total_tokens ?? 0)) }
                if !models.isEmpty {
                    ForEach(models.prefix(maxModelRows)) {
                        ModelRowView(item: $0, windowLabel: model.state?.windowLabel ?? "今天")
                    }
                    if models.count > maxModelRows {
                        Text("还有 \(models.count - maxModelRows) 个模型，按累计排序在后面")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                            .padding(.top, 2)
                    }
                    // 全部历史的累计（模型面板底部带一份，方便直接看）
                    if let c = model.state?.cost?.total_cumulative, c > 0 {
                        Divider().padding(.vertical, 4)
                        HStack(spacing: 6) {
                            Text("全部历史累计")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                            Text(compactTokens(totalTokens))
                                .font(.system(size: 12, weight: .semibold))
                                .monospacedDigit()
                            Text("token")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                            Spacer()
                        }
                        HStack(spacing: 6) {
                            Text("等效成本")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                            Text("¥\(compactMoney(c))")
                                .font(.system(size: 12, weight: .semibold))
                                .monospacedDigit()
                            if model.state?.cost?.estimated == true {
                                Text("按 API 单价折算")
                                    .font(.system(size: 9))
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                        }
                    }
                } else {
                    Text("还没有模型数据，点下面的刷新")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }
            }
            .transition(.opacity)
            .animation(.easeInOut(duration: 0.16), value: model.viewMode)

            if !model.lastError.isEmpty {
                Text(model.lastError)
                    .font(.system(size: 10))
                    .foregroundStyle(.red)
                    .lineLimit(2)
            }

            Divider()
            HStack(spacing: 12) {
                Button {
                    model.refresh()
                } label: {
                    Label(model.busy ? "刷新中…" : "立即刷新", systemImage: "arrow.clockwise")
                }
                .disabled(model.busy)

                Button(action: { model.openPreview() }) {
                    Image(systemName: "rectangle.on.rectangle")
                }
                .help("打开卡片预览")

                Button(action: { model.copySummary() }) {
                    Image(systemName: "doc.on.doc")
                }
                .help("复制摘要")

                Button(action: { model.openLog() }) {
                    Image(systemName: "doc.text.magnifyingglass")
                }
                .help("打开日志")

                Spacer()
            }
            .font(.system(size: 11))
        }
        .padding(12)
        .frame(width: 340)
    }
}

// MARK: - AppKit 外壳

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private let model = AppModel()
    private var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // 离屏渲染面板快照（自查排版用，不进 GUI 流程）：
        //   DotUsageBar --render-panel <out.png> [quota|model]
        let args = CommandLine.arguments
        if let idx = args.firstIndex(of: "--render-panel"), idx + 1 < args.count {
            let out = args[idx + 1]
            let arg = idx + 2 < args.count ? args[idx + 2] : "quota"
            let mode: PanelTab
            switch arg {
            case "model": mode = .model
            case "trend": mode = .trend
            default: mode = .quota
            }
            model.viewMode = mode
            model.load()
            renderPanel(to: out)
            NSApp.terminate(nil)
            return
        }

        FileHandle.standardError.write("dot-usage: applicationDidFinishLaunching\n".data(using: .utf8)!)
        model.onChange = { [weak self] in self?.updateStatusTitle() }

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "chart.bar.fill", accessibilityDescription: "AI 用量")
            button.image?.isTemplate = true
            button.target = self
            button.action = #selector(togglePopover)
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }

        popover = NSPopover()
        popover.behavior = .transient
        popover.animates = false
        popover.contentViewController = NSHostingController(rootView: PanelView(model: model))

        // 自动刷新：15 分钟一次，和定时采集的节奏一致（两者互不依赖）
        timer = Timer.scheduledTimer(withTimeInterval: 900, repeats: true) { [weak self] _ in
            DispatchQueue.main.async { self?.model.refresh(silent: true) }
        }

        // 启动后先静默采一次：菜单栏里显示的应该是当下的数据，
        // 而不是最多 15 分钟前的 state.json。放到后台线程，别卡住界面。
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 0.5) {
            DispatchQueue.main.async { self.model.refresh(silent: true) }
        }

        updateStatusTitle()
        FileHandle.standardError.write("dot-usage: statusItem created=\(statusItem != nil) title=\(statusItem.button?.title ?? "-")\n".data(using: .utf8)!)
    }

    /// 把面板离屏渲染成 PNG（自查排版，不弹窗）
    @MainActor
    private func renderPanel(to path: String) {
        RenderMode.isOffscreen = true
        defer { RenderMode.isOffscreen = false }
        let view = PanelView(model: model)
            .frame(width: 340)
            .background(Color(nsColor: .windowBackgroundColor))
        let renderer = ImageRenderer(content: view)
        renderer.scale = 2
        guard let img = renderer.nsImage,
              let tiff = img.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else { return }
        try? png.write(to: URL(fileURLWithPath: path))
        FileHandle.standardError.write("dot-usage: panel rendered → \(path)\n".data(using: .utf8)!)
    }

    private func updateStatusTitle() {
        guard let button = statusItem.button else { return }
        button.title = model.topTitle
        button.toolTip = "AI 用量 · \(model.updatedAt)"
    }

    @objc private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
        } else {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            popover.contentViewController?.view.window?.makeKey()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        timer?.invalidate()
    }
}

@main
struct DotUsageBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate

    var body: some Scene {
        Settings {
            EmptyView()
        }
    }
}
