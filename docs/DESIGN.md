# 设计说明

面向开源读者的一份短设计文档。只讲**为什么这么做**和**怎么扩展**。

---

## 1. 分层

```
┌──────────────────────────────────────────────────────────┐
│ macos/  menubar/DotUsageBar.swift                        │
│         SwiftUI + NSStatusItem，只读 snapshot.json        │
│         完全不知道 DOT 墨水屏存在                        │
└──────────────────────────────────────────────────────────┘
                            ↑ 读同一个快照文件
┌──────────────────────────────────────────────────────────┐
│ sinks/    base.py     Sink 基类 + 注册表（可插拔）          │
│           dot.py      DOT Open API（推图 / 推文本 / 切屏）  │
│           dot_app.py  DOT App 自家接口（建内容项）          │
│           ← 输出目标可插拔：加新的 sink 不用改 core        │
└──────────────────────────────────────────────────────────┘
                            ↑ 卡片对象
┌──────────────────────────────────────────────────────────┐
│ cards/    usage.py / window.py / reset_list.py            │
│           core/pulse.py / core/projects.py / canvas.py   │
│           ← 把 UsageRow 变成一张能看的屏                  │
└──────────────────────────────────────────────────────────┘
                            ↑ 统一数据模型
┌──────────────────────────────────────────────────────────┐
│ core/     schema.py    UsageRow（唯一的行模型）            │
│           aggregate.py 合并平台、算「什么时候烧完」         │
│           models.py / pulse.py / projects.py / tibo.py     │
└──────────────────────────────────────────────────────────┘
                            ↑ 原始行
┌──────────────────────────────────────────────────────────┐
│ sources/  base.py      SourceSpec（一个源的声明）          │
│           loader.py    读 sources.toml，合并去重            │
│           adapters/    sqlite_usage / jsonl_usage / …      │
│           official/    平台官方配额接口                    │
└──────────────────────────────────────────────────────────┘
```

| 层 | 目录 | 职责 | 不该干的事 |
|---|---|---|---|
| core | `src/dotusage/core/` | 定义 `UsageRow`、合并平台、算派生指标 | 不碰文件、不发请求 |
| sources | `src/dotusage/sources/` | 读本机日志/数据库、问官方接口 | 不做展示 |
| cards | `src/dotusage/cards/` | 把行变成一张能看的屏（HTML / PNG / 画板 JSON） | 不采集、不推送 |
| sinks | `src/dotusage/sinks/` | 把卡片送到输出目标 | 不解释数据 |
| macos | `menubar/` | 本地随时看的菜单栏面板 | 不知道墨水屏存在 |

**分层的唯一硬规则**：`core` 不依赖 `cards`/`sinks`，`cards` 不依赖 `sinks`。
上层可以往下调，反过来不行。加一个新的输出目标时，core 一行都不用改。

### sink 是怎么可插拔的

`sinks/base.py` 定义了 `Sink` 基类，一个 sink 只要有三件事：

```python
class MySink(Sink):
    name = "myoutput"          # 配置里用它开关
    label = "我的输出"
    needs_secret = True        # doctor 会提示要配密钥

    def enabled(self, cfg: dict) -> bool: ...   # 默认读 sources.toml 的 [sink.myoutput] enabled
    def push(self, rows, cfg, state, force=False) -> str: ...  # 返回一句给人看的话
```

写好 `register(MySink())` 就能用。开关写在 `sources.toml`：

```toml
[sink.dot]
enabled = true

[sink.myoutput]
enabled = false
```

目前真正对外的输出目标只有**墨水屏**一种；
`local` sink 只负责把状态写成本地文件（它必须第一个跑，后面的 sink 和桌面端都靠这个文件）。

---

## 2. 数据流

```
本机日志/数据库 ─┐
                 ├─→ sources ─→ [UsageRow 一行一个平台/模型]
官方配额接口  ──┘                     │
                                     ├─→ aggregate ─→ 合并平台 + 算「烧完时间」
                                     │
                    ┌────────────────┼────────────────┐
                    ↓                ↓                ↓
              cards/usage.py   cards/window.py   core/pulse.py ...
                    │                │                │
                    └────────────┬───┴────────────────┘
                                 ↓
                    sinks/dot.py ─→ DOT 墨水屏
                                 ↓
                    ~/.local/state/dot-usage/state.json
                                 ↓
                    menubar/DotUsageBar.swift（读同一个文件）
```

四个阶段，各自的产物：

| 阶段 | 输入 | 输出 | 代码 |
|---|---|---|---|
| 采集 | 日志 / 数据库 / 官方接口 | `list[UsageRow]` + `list[SourceReport]` | `src/dotusage/sources/` |
| 聚合 | `UsageRow` 列表 | 合并后的平台行、模型维度、脉搏、项目账 | `src/dotusage/core/aggregate.py` 等 |
| 卡片 | `UsageRow` + 聚合结果 | 画板 JSON / PNG 字节 / 文本 | `src/dotusage/cards/` |
| 推送 | 卡片 | 设备上的内容项 | `src/dotusage/sinks/dot.py` |

采集和推送之间隔着一个文件（快照），所以**菜单栏 App 和墨水屏互不依赖**：
菜单栏读快照，墨水屏推内容项，谁挂了都不影响谁。

---

## 3. 为什么图片卡要转 1bit

墨水屏只有黑白两色，没有灰。

设备拿到灰阶图会自己做抖动（dithering）——本来连续的灰阶被拆成黑白点阵，
小字立刻糊成一团噪点。所以图片卡在推之前统一转成 **1bit 纯黑白**：

```python
# src/dotusage/cards/usage.py:125
def to_1bit(png: bytes) -> bytes: ...
```

灰阶版保留了一个对照实现（`to_gray_png()`），只在本地预览用，**不推设备**。

HTML → PNG 走本机 Chrome headless（Chrome / Chromium / Edge 任选其一），
不引入任何渲染库 —— 整个项目零第三方依赖。

---

## 4. 为什么只有第一个内容项 refreshNow

DOT 的内容项推送接口带一个 `refreshNow` 字段，意思是「立刻重刷屏幕」。

设备上七张卡 = 七个内容项。如果每个都带 `refreshNow: true`，
设备会每 1 秒重刷一次 —— 屏幕闪得没法看。

所以规则是：**只给第一个内容项 `refreshNow`，其余等设备自己轮播到时再显示**。

```python
# src/dotusage/sinks/dot.py:393
"refreshNow": idx == 0,
```

副作用是：改完数据想立刻看到效果，要手动切屏：

```bash
python3 -m dotusage.cli switch
```

---

## 5. 状态文件契约

`src/dotusage/util/config.py` 里定义。**配置**和**状态**分开放：

| 路径 | 内容 |
|---|---|
| `~/.config/dot-usage/config.json` | 配置。可提交，**不含密钥** |
| `~/.config/dot-usage/sources.toml` | 源声明 + sink 开关。同 id 覆盖内置源 |
| `~/.local/state/dot-usage/state.json` | 状态。每次采集覆盖 |
| `~/.local/state/dot-usage/dot_state.json` | DOT 推送状态（上次推了什么） |
| `~/.local/state/dot-usage/dot-usage.log` | 运行日志 |

### state.json

由 `src/dotusage/sinks/local.py` 的 `build_state()` 组装、`local` sink 写出。
两个消费者共用：墨水屏推送和菜单栏 App。

| 字段 | 类型 | 内容 |
|---|---|---|
| `version` | int | 契约版本号（现在是 1）。桌面端靠它判断是不是老格式 |
| `generated_at` | string | 采集时间（ISO8601） |
| `generated_at_epoch` | float | 同一时间的秒级时间戳 |
| `icon_dir` | string | 图标目录的绝对路径，桌面端找图标用 |
| `window` | string | 消耗数据的统计窗口标签（近24h / 近7天 / …）。各源窗口不一致时为空串 |
| `platforms` | list | 按平台合并后的一行一平台。**卡片和菜单栏用这个** |
| `models` | list | 按模型维度：窗口内（默认近 24h）+ 历史累计 |
| `pulse` | object | 近 48 小时的消耗节奏（按小时） |
| `projects` | object | 按工作目录拆的账 |
| `daily` | object | 近 30 天按天 × 模型的消耗与折算成本（菜单栏「趋势」tab） |
| `cost` | object | 等效成本：当前窗口 + 全历史累计 + 按模型明细 |
| `sources` | list | 每个源的状态：读到几行、报了什么错 |
| `rows` | list | 原始明细（一行一个平台/模型/窗口），排查用 |

**契约要点**：`platforms` 是消费方真正读的段，形状稳定；
`rows` 是原始明细，加字段不影响它；
`sources` 是自检段，`doctor` 靠它报错；
`version` 是版本号，字段有 breaking 变化时必须 +1。

> `window` 是为了治「数字对不上」加的：统计窗口现在每个源都能单独配
> （见 [SOURCES.md](SOURCES.md#optionsbuiltin--official)），
> 桌面端和卡片都得知道当前是哪个窗口，不能写死「今天」——
> 窗口一改还写「今天」就是在说谎。

状态是**覆盖写**的，没有历史。历史用量靠各工具自己的日志留着。
旧版本把快照叫 `snapshot.json`，现在统一叫 `state.json`；
读的时候会自动回退去读旧文件，迁移期不用手动搬。

---

## 6. 扩展点

### 加一个数据源（不改 Python）

写一段 TOML 放进 `~/.config/dot-usage/sources.toml`：

```toml
[[source]]
id = "mytool"
name = "MyTool"
platform = "自家"
adapter = "sqlite_usage"

[source.options]
db = "~/.mytool/data.db"
sql = "SELECT model AS model, ts AS ts, total_tokens AS total FROM usage WHERE ts > :since"
time_scale = 0.001
window_hours = 24
```

内置两个通用适配器：`sqlite_usage`（一条 SQL）和 `jsonl_usage`（一份字段路径）。
详见 [SOURCES.md](SOURCES.md)。

### 加一个官方配额 provider（要写代码）

平台官方接口的鉴权和返回结构各不相同，统一放在 `src/dotusage/official/providers.py`：
写一个 `() -> tuple[list[UsageRow], SourceReport]` 的函数，注册进 `OFFICIAL_COLLECTORS`，
再在 `sources.toml` 里用 `official = "<名字>"` 点名。详见 [SOURCES.md](SOURCES.md#4-官方配额-provider)。

### 加一张卡

在 `src/dotusage/cards/`（或 `core/`，取决于它是不是通用逻辑）加一个模块，
暴露 `build_html()` / `render_png()`，或者返回画板 JSON。
然后在 `src/dotusage/sinks/dot.py:428`（`push_dot_smart()`）里挂上对应的内容项。

排版约束写在 `cards/canvas.py:66`：296×152 最多 4 行，多一行就压页脚。

### 加一个输出目标（不改 core）

继承 `sinks/base.py` 的 `Sink`，实现 `push()`，再 `register()` 进去：

```python
# src/dotusage/sinks/myoutput.py
from dotusage.sinks.base import Sink, register

class MyOutput(Sink):
    name = "myoutput"
    label = "我的输出"

    def push(self, rows, cfg, state, force=False) -> str:
        ...   # 把 rows 送到你想去的地方
        return "✓ 我的输出"

register(MyOutput())
```

开关写在 `sources.toml` 的 `[sink.myoutput] enabled`。
core 和 cards 都不知道 sink 的存在 —— 这是分层的意义。

---

## 7. 已知限制

| 限制 | 说明 |
|---|---|
| 官方配额只有五家 | ChatGPT / z.ai / MiniMax / DeepSeek / Grok。Claude Code 官方没给配额接口 |
| Grok 只给档位 | xAI 没有配额 % 接口，卡上标的是订阅档位 |
| 通用 jsonl 适配器读不了 Codex | 它的行过滤要求行内含 `"usage"` / `"tokens"`，而 Codex 用 `total_token_usage`。Codex 走内置 `codex_rollouts` 适配器 |
| 图标目录有三层兜底 | `util/config.py:icon_dir()`：环境变量 `DOT_USAGE_ICON_DIR` → 包旁边的 `icons/png` → `~/Tools/dot-usage/icons/png`。菜单栏 App 侧同样有兜底链（`macos/DotUsageBar.swift`） |
| 菜单栏 App 的兜底路径写死家目录 | `macos/DotUsageBar.swift` 里 `~/Tools/dot-usage/icons/png` 和 `~/Tools/dot-usage/preview/card-preview.html` 是最后兜底，装到别处时靠前两层（环境变量 / 包内） |
| 价格是**折算值**不是账单 | 订阅制工具（M Plan / GLM Coding Plan / ChatGPT）本地日志里没有钱，所以用官方公开单价折算「等效 API 价」。单价会过时，改 `sources.toml` 的 `[pricing.<模型>]` 覆盖 |
| 单价按**最长前缀**匹配 | `glm-5.3-flash` 不会被 `glm-5.3` 抢走，`gpt-6.1-sol-900k` 走 `gpt-6.1-sol` 的价。美元价格按汇率折成人民币 |
| **Python 必须 3.11+** | 数据源配置用标准库 `tomllib` 读，3.9/3.10 没有它。踩过的坑：解释器是 `/usr/bin/python3`（3.9）时，配置被静默读成空，采集还「成功」，只是没数据源。现在 `sources/loader.py` 读不到 `tomllib` 直接报错退出，`cli.py` 入口也会拦一道；`install.sh` 自动挑 3.11+ 的解释器并写进 `launchd/*.plist`（`__PYTHON__` 占位） |
| 命令行入口有两个 | `python -m dotusage …`（有 `__main__.py`）、`python -m dotusage.cli …`、以及装完后的 `dot-usage` 命令，三种都能用 |
| 菜单栏 App 的解释器靠文件传参 | 这个 App 由 launchd **直接跑二进制**（不经 `open`），所以 `EnvironmentVariables` 生效；同时 `install.sh` 还写一份 `~/Library/Application Support/DotUsageBar/config.json`（`python` + `src_dir`）作为兜底——手动从 Finder 打开时走的是 LaunchServices，拿不到 launchd 的环境变量 |
| 菜单栏 App 的候选解释器有上限 | `macos/DotUsageBar.swift` 里 `Collector.pythonCandidates` 依次试：`DOT_USAGE_PYTHON` / 安装配置里的 python → homebrew → `/usr/local` → `env python3`（按 PATH 找，能命中 pyenv/conda）→ `~/.pyenv/shims/python3` → 系统 3.9。每个候选都要**既能 import dotusage 又 ≥ 3.11** 才算数 |
