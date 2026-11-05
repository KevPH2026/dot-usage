# 数据源（Sources）

dot-usage 的每个工具都是一个「源」：**去哪儿读、读什么字段、算它属于哪个平台**。
源用 TOML 声明，接新工具**不用改 Python 代码**。

配置位置与优先级：

| 顺序 | 文件 | 说明 |
|---|---|---|
| 1 | `src/dotusage/sources/defaults.toml` | 包内置的默认源（作者本机那 8 个工具） |
| 2 | `~/.config/dot-usage/sources.toml` | 你的源。**同 `id` 覆盖内置项**（`options` 是深合并，只覆盖你写了的键） |
| 3 | `~/.config/dot-usage/sources.json` | 同上，JSON 写法；存在时优先于 `sources.toml` |

读取代码：`src/dotusage/sources/loader.py` → `load_config()`。
TOML 用 Python 3.11+ 标准库 `tomllib` 解析，零第三方依赖。

> `sources.toml` 里除了 `[[source]]`，还能放 sink 的开关（`[sink.<name>] enabled = true/false`）。
> 那是输出目标的事，见 [DESIGN.md](DESIGN.md#sink-是怎么可插拔的)。

---

## 1. 字段表

每个源是一段 `[[source]]`。

### source 级字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | ✅ | 唯一标识。同 id 会覆盖内置源 |
| `name` | string | | 显示名。卡片上不写 `platform` 时拿它顶包 |
| `platform` | string | | 归到哪个平台。卡片按平台合并，`platform` > `name` > `id` |
| `icon` | string | | 图标名，对应 `icons/png/<icon>.png`。找不到就静默不画图标 |
| `adapter` | string | | `sqlite_usage` / `jsonl_usage` / `builtin` / `official` |
| `builtin` | string | | `adapter = "builtin"` 时用哪个内置采集器（`codex` / `zcode` / `minimax` / `claude` / `opencode` / `hermes`） |
| `official` | string | | 要点名跑哪个官方配额 provider（`chatgpt` / `zai` / `minimax` / `grok` / `deepseek`） |
| `enabled` | bool | | 默认 `true`。`false` 就跳过这个源 |
| `options` | table | | 适配器自己的参数，见下 |

### options：sqlite_usage

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `db` | string | — | SQLite 文件路径，支持 `~` 和环境变量 |
| `sql` | string | — | 查询语句。**必须**带 `:since` 过滤 |
| `time_scale` | float | `1` | 取出来的时间戳单位换算。毫秒 → 秒填 `0.001` |
| `window_hours` | float | `24` | 只看最近 N 小时。`0` = 全部历史 |
| `fallback_model` | string | — | SQL 里没有模型名时用这个 |

`:since` 永远是**秒级**时间戳。数据库里存的是毫秒，就在 SQL 里写 `WHERE ts > :since * 1000`。
`time_scale` 只负责把**取出来**的时间换算回秒，两处各管一头，别搞混。

### options：jsonl_usage

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `glob` | string | — | 文件匹配，支持 `**`。如 `~/.claude/projects/**/*.jsonl` |
| `usage_path` | string | — | 先定位到用量所在的对象，token 字段再在它里面取 |
| `model` | string | — | 模型名路径 |
| `input` / `output` / `reasoning` | string | — | token 数路径 |
| `cache_read` / `cache_write` | string | — | 缓存 token 路径 |
| `total` | string | — | 总 token。不写就由 input+output+cache_read+cache_write 算 |
| `cost` | string | — | 花费路径 |
| `ts` | string | — | 时间路径（在**行根对象**上取，不在 `usage_path` 里面） |
| `window_hours` | float | `24` | 同上 |
| `max_files` | int | `400` | 一次最多扫多少个文件（按修改时间倒序取新的） |
| `fallback_model` | string | — | 取不到模型名时用这个 |

字段路径用点号：`a.b.c`；数组用 `a.0.b`。
时间是 ISO 字符串（`2026-10-29T08:03:04.862Z`）或秒/毫秒时间戳都行（大于 1e11 视为毫秒）。

### options：builtin / official

`builtin` 指定内置采集器名，`official` 指定官方 provider 名。

内置采集器额外认一个 option：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `window` | string | `24h` | 统计窗口：`24h` / `today` / `7d` / `30d` / `all`，也可以写任意小时数（`48h`） |

**为什么窗口要能配**：各工具自己的用量页范围都不一样。
ZCode 的用量统计只有「全部 / 7天 / 30天」，**没有「近 24h」**；
本地看板默认近 24h。窗口不同，数字必然对不上——想跟 ZCode 对齐，
把那个源的 `window` 改成 `7d` 或 `30d` 就行。卡片上会写清楚当前窗口
（近24h / 近7天 / …），不会含糊。

```toml
[[source]]
id = "zcode"
name = "ZCode"
platform = "Z.ai"
adapter = "builtin"
builtin = "zcode"

[source.options]
window = "7d"        # 跟 ZCode 用量统计页的「近 7 天」对齐
```

用户配置是**深合并**：只写 `window` 不会把内置配置里的其它 option 抹掉。

---

## 2. 通用适配器：sqlite_usage

**大多数 AI CLI 工具的用量都记在某个 SQLite 表里**，所以这个适配器能接下大部分工具。

完整可抄的例子：

```toml
[[source]]
id = "mytool"
name = "MyTool"
platform = "自家"
icon = "chatgpt"
adapter = "sqlite_usage"

[source.options]
db = "~/.mytool/data.db"
sql = """
    SELECT model            AS model,
           ts               AS ts,
           input_tokens     AS input,
           output_tokens    AS output,
           total_tokens     AS total,
           cost_usd         AS cost
    FROM usage
    WHERE ts > :since
"""
time_scale = 0.001
window_hours = 24
```

**列名不用跟适配器一致 —— 用 `AS` 改别名就行。**适配器认识的字段名：

| 别名 | 含义 |
|---|---|
| `model` | 模型名。缺了用 `fallback_model` |
| `input` / `output` / `reasoning` | 输入 / 输出 / 思考 token |
| `cache_read` / `cache_write` | 缓存读取 / 写入 token |
| `total` | 总 token。不写就自动求和 |
| `calls` | 次数。不写按 1 算 |
| `ts` | 时间戳。会乘 `time_scale` |
| `cost` | 花费（美元） |

适配器会把**同一个模型的多行合并**成一行，所以 SQL 里 `GROUP BY` 一下更省事。

作者本机 5 个 SQLite 工具的真实写法见
[`examples/sources.generic.example.toml`](../examples/sources.generic.example.toml)。

---

## 3. 通用适配器：jsonl_usage

每行一个 JSON 的会话日志（Claude Code 就是这种）。

完整可抄的例子：

```toml
[[source]]
id = "mytool2"
name = "MyTool2"
platform = "自家"
adapter = "jsonl_usage"

[source.options]
glob = "~/.mytool2/sessions/**/*.jsonl"
model = "message.model"
input = "message.usage.input_tokens"
output = "message.usage.output_tokens"
cache_read = "message.usage.cache_read_input_tokens"
cache_write = "message.usage.cache_creation_input_tokens"
ts = "timestamp"
window_hours = 24
```

日志长这样就能对上：

```json
{"type":"assistant","timestamp":"2026-10-02T05:53:59.122Z",
 "message":{"model":"claude-...","usage":{"input_tokens":12,"output_tokens":34}}}
```

用法要点：

- 行里没有用量字段就跳过，所以日志里塞别的东西也不影响。
- `usage_path` 适合「用量在一个嵌套对象里」的情况：先定位对象，token 路径相对它取。
- `ts` 在**行根对象**上取（`payload.info.last_token_usage.updated_at` 这种）。

---

## 4. 官方配额 provider

平台官方的配额接口不在通用适配器里 —— 它们各有各的鉴权和返回结构，
统一放在 `src/dotusage/official/providers.py`。

已有的五个：

| provider | 官方接口 | 给什么 |
|---|---|---|
| `chatgpt` | `chatgpt.com/backend-api/wham/usage` | 7 天 / 5 小时窗口的已用 % 与重置时刻 |
| `zai` | `api.z.ai/api/monitor/usage/quota/limit` | 5 小时 / 周窗口 |
| `minimax` | `www.minimaxi.com/v1/api/openplatform/coding_plan/remains` | 套餐剩余 |
| `deepseek` | `api.deepseek.com/user/balance` | 余额 |
| `grok` | `grok.com/rest/subscriptions` | 订阅档位（官方没有配额 % 接口） |

在 `sources.toml` 里点名才会跑：

```toml
[[source]]
id = "grok"
name = "Grok"
platform = "Grok"
icon = "grok"
adapter = "official"
official = "grok"
```

### 加自己的 provider

**加官方 provider 是要写代码的**（它得知道怎么鉴权、返回结构长什么样）。
写一个函数，在 `OFFICIAL_COLLECTORS` 里注册，再在 `sources.toml` 里点名：

```python
# src/dotusage/official/providers.py
def myvendor_usage() -> tuple[list[UsageRow], SourceReport]:
    data = _http_json("https://api.myvendor.com/v1/quota", headers={"Authorization": f"Bearer {myvendor_token()}"})
    rows = [UsageRow(source="myvendor", label="MyVendor", kind="quota",
                     used_percent=float(data["used"]), resets_at=to_epoch(data["reset_at"]), ...)]
    return rows, SourceReport("myvendor", ok=True)

OFFICIAL_COLLECTORS = [
    ("chatgpt", chatgpt_usage),
    ("myvendor", myvendor_usage),   # ← 加这一行
]
```

密钥**只从环境变量或 macOS 钥匙串读**（见 `src/dotusage/util/config.py` 的
`dot_api_key()` / `_keychain()`），不要写进代码、日志或卡片。

---

## 5. 本地调试

```bash
# 看当前生效了哪些源（id / 适配器 / 平台 / 官方配额 provider）
python3 -m dotusage.cli sources

# 第一次用：把默认源配置写出来（已存在就不覆盖）
python3 -m dotusage.cli sources --init

# 看每个源读到几行、报了什么错 —— 调源第一个该跑的
python3 -m dotusage.cli collect

# 自检：配置 / 密钥 / 设备 / 浏览器
python3 -m dotusage.cli doctor

# 看各输出目标（sink）的开关状态
python3 -m dotusage.cli sinks

# 打印当前状态摘要
python3 -m dotusage.cli show

# 打印会切成哪几屏、每屏放什么
python3 -m dotusage.cli pages

# 生成 HTML 预览（按 296×152 真实几何，不用推设备就能看排版）
python3 -m dotusage.cli preview --out ~/dot-usage-preview.html
```

不想装包就用 `PYTHONPATH=src python3 -m dotusage.cli ...`。

### 常见报错

| 报错 | 原因 | 怎么办 |
|---|---|---|
| `未找到 /path/to.db` | 路径不对 | 路径支持 `~` 和环境变量，确认文件真在 |
| `缺少 options.sql` | `sqlite_usage` 没写 SQL | 补 `sql` |
| `没有这个适配器：xxx` | `adapter` 拼错 | 只能是 `sqlite_usage` / `jsonl_usage` / `builtin` / `official` |
| `24h 内无记录（扫了 N 个文件）` | 时间窗口太窄，或路径没匹配上 | 先把 `window_hours` 设成 `0` 试 |
| `查询失败 …` | SQL 语法错或表名不对 | 单独用 `sqlite3` 跑一遍同样的 SQL |

---

## 6. 已知限制

- **通用 `jsonl_usage` 读不了 Codex。** 适配器只扫行内含 `"usage"` 或 `"tokens"` 字样的日志，
  而 Codex 的 rollout 把用量放在 `total_token_usage` 里，行内不含这两个词。
  Codex 请用内置的 `codex_rollouts` 适配器（`adapter = "builtin"`, `builtin = "codex"`）。
- **Claude Code 没有官方配额接口**，卡片上只有本地消耗，没有配额 % 和重置时间。
- **Grok 官方只给订阅档位**，档位 ≠ 配额百分比，卡片上会标出来。
- `icon` 找不到对应 PNG 时静默不画图标，不报错（`src/dotusage/util/icons.py`）。
