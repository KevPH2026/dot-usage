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
    /// 原始明细
    let rows: [UsageItem]?
    /// 按平台合并后的一行一平台，优先显示
    let platforms: [UsageItem]?
    /// 按模型维度：今天（近 24h）+ 历史累计
    let models: [ModelUsage]?

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
    let today_tokens: Int?
    let today_calls: Int?
    let today_cost: Double?
    let total_tokens: Int?
    let total_calls: Int?
    let total_cost: Double?

    var id: String { (platform ?? "?") + "|" + (model ?? "?") }
}

/// 面板视图切换
enum PanelTab: String, CaseIterable, Identifiable {
    case quota = "配额"
    case model = "模型"

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
    /// 面板视图：配额 / 模型
    @Published var viewMode: PanelTab = .quota

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
                "\($0.model ?? "?")  今天 \(compactTokens($0.today_tokens))  累计 \(compactTokens($0.total_tokens))  \($0.total_calls ?? 0)次"
            }.joined(separator: "\n")
        } else {
            text = s.displayRows.map { "\($0.label)  \($0.headline)  \($0.detail)" }.joined(separator: "\n")
        }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
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

/// 模型行：模型名 + 今天的消耗 + 历史累计
struct ModelRowView: View {
    let item: ModelUsage

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
                    Text("今天 \(compactTokens(item.today_tokens))")
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
            } else {
                let models = model.state?.models ?? []
                if !models.isEmpty {
                    ForEach(models.prefix(maxModelRows)) { ModelRowView(item: $0) }
                    if models.count > maxModelRows {
                        Text("还有 \(models.count - maxModelRows) 个模型，按累计排序在后面")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                            .padding(.top, 2)
                    }
                } else {
                    Text("还没有模型数据，点下面的刷新")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                }
            }

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
            let mode: PanelTab = (idx + 2 < args.count && args[idx + 2] == "model") ? .model : .quota
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
