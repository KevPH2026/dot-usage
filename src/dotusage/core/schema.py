"""统一用量模型。

所有采集器（Codex / ZCode / MiniMax / Claude Code / OpenCode / Hermes）
都把各自口径的数据归一化成 UsageRow，卡片渲染器只认这一种结构。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


def now_ts() -> float:
    return time.time()


def to_epoch(value) -> float:
    """把各种时间写法统一成 epoch 秒。

    Codex / Hermes 的时间戳是 ISO8601 字符串，Codex 的 rate_limits 是
    Unix 浮点，两个都见过，所以两种都得认。
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0.0
        try:
            return float(s)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return 0.0


def iso(ts: float | None = None) -> str:
    ts = now_ts() if ts is None else ts
    return datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d %H:%M:%S")


def compact(n: float | None) -> str:
    """把 token 数压成人眼能扫的短字符串。"""
    if n is None:
        return "-"
    n = float(n)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{int(n)}"


def countdown(resets_at: float | None) -> str:
    """距离重置还有多久。"""
    if not resets_at:
        return ""
    delta = resets_at - now_ts()
    if delta <= 0:
        return "即将重置"
    d, rem = divmod(int(delta), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d > 0:
        return f"{d}天{h}小时后重置" if h else f"{d}天后重置"
    if h > 0:
        return f"{h}小时{m}分后重置"
    return f"{max(m, 1)}分钟后重置"


def countdown_short(resets_at: float | None) -> str:
    """极简倒计时：42分 / 6天 / 1时37分。

    墨水屏上「42分钟后重置」这种长句会把整行挤爆，
    只留数字，单位省掉「后重置」。
    """
    if not resets_at:
        return ""
    delta = resets_at - now_ts()
    if delta <= 0:
        return "即将"
    d, rem = divmod(int(delta), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d > 0:
        return f"{d}天"
    if h > 0:
        return f"{h}时{m}分" if m else f"{h}时"
    return f"{max(m, 1)}分"


def window_label(minutes: int | None) -> str:
    if not minutes:
        return ""
    if minutes % 1440 == 0:
        return f"{minutes // 1440}天窗口"
    if minutes % 60 == 0:
        return f"{minutes // 60}小时窗口"
    return f"{minutes}分钟窗口"


@dataclass
class UsageRow:
    """一行卡片内容。

    kind=quota       → 有配额百分比和重置时间（Codex 订阅）
    kind=consumption → 只有消耗量（本地 CLI / API 计费）
    """

    source: str
    label: str
    model: str = ""
    kind: str = "consumption"   # quota | consumption | balance
    used_percent: float | None = None
    window: str = ""
    resets_at: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    calls: int = 0
    updated_at: float = field(default_factory=now_ts)
    note: str = ""
    path: str = ""
    # 手工指定右侧显示值（汇总行用，比如 "3.5B / 120K"）
    headline_override: str = ""
    # 手工指定说明文字（余额行用，比如 "充值 ¥9.44"）
    detail_override: str = ""
    # 第二行说明（Tibo 行用：上次重置 / 概率与窗口）
    detail2_override: str = ""
    # 平台图标名（对应 icons/png/<name>.png）
    icon: str = ""

    @property
    def headline(self) -> str:
        if self.headline_override:
            return self.headline_override
        if self.kind == "quota" and self.used_percent is not None:
            return f"{self.used_percent:.0f}%"
        return compact(self.total_tokens)

    @property
    def detail(self) -> str:
        if self.detail_override:
            return self.detail_override
        bits: list[str] = []
        if self.kind == "quota":
            if self.window:
                bits.append(self.window)
            cd = countdown(self.resets_at)
            if cd:
                bits.append(cd)
        elif self.kind == "consumption":
            # 消耗行写清楚是哪个窗口的数——ZCode 自己的统计是 7天/30天，
            # 本地默认近24h，窗口不同数字就对不上，标签必须说清楚。
            bits.append(self.window or "近24h")
        if self.cost_usd:
            bits.append(f"${self.cost_usd:.2f}")
        if self.calls:
            bits.append(f"{self.calls}次")
        if self.note:
            bits.append(self.note)
        return " · ".join(bits)

    @property
    def bar_width(self) -> float:
        if self.kind == "quota" and self.used_percent is not None:
            return max(0.0, min(100.0, self.used_percent))
        if self.total_tokens <= 0:
            return 0.0
        # 消耗量没有天然上限，用相对最大值做对比条
        return 0.0

    @property
    def uid(self) -> str:
        """每行唯一标识。

        同一平台的不同窗口（5 小时 / 周）label 是一样的，
        所以唯一键必须把 kind、window、重置时间都带上——
        否则 SwiftUI 的 ForEach 会把两行当成同一个，表现为重复+丢行。
        """
        return f"{self.source}|{self.label}|{self.kind}|{self.window}|{self.resets_at}|{self.note}|{self.icon}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["uid"] = self.uid
        d["headline"] = self.headline
        d["detail"] = self.detail
        d["bar_width"] = self.bar_width
        d["updated_at_iso"] = iso(self.updated_at)
        return d


@dataclass
class SourceReport:
    name: str
    ok: bool = True
    rows: int = 0
    note: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
