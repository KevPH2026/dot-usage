"""统计窗口。

**为什么需要它**：各工具自己的用量页时间范围都不一样——
ZCode 的用量统计只有「全部 / 7天 / 30天」，没有「近 24h」；
本地看板默认只看近 24h。两边对不上，十有八九是窗口不同。

所以窗口要能配，而且卡上要写清楚到底是哪个窗口，
不然看到两个不一样的数字谁也说不清差在哪。

配置写法（sources.toml 里每个 source 的 options）：

    [source.options]
    window = "24h"      # 近 24 小时（滚动窗口，默认）
    window = "today"    # 今天（本地 0 点起）
    window = "7d"       # 近 7 天
    window = "30d"      # 近 30 天
    window = "all"      # 全部历史
    window = "48h"      # 任意小时数
"""

from __future__ import annotations

import time

DAY = 86400.0

_ALIASES = {
    "24h": "24h", "1d": "24h", "day": "24h",
    "today": "today", "today0": "today", "midnight": "today",
    "7d": "7d", "week": "7d",
    "30d": "30d", "month": "30d",
    "all": "all", "0": "all", "none": "all", "total": "all",
}

_LABELS = {
    "24h": "近24h",
    "today": "今天",
    "7d": "近7天",
    "30d": "近30天",
    "all": "全部",
}


def today_start(now: float | None = None) -> float:
    """今天 0 点（本地时区）的秒级时间戳。"""
    t = time.localtime(now if now is not None else time.time())
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def parse_window(spec: str | None, now: float | None = None) -> tuple[float, str]:
    """返回 (窗口起点秒级时间戳, 窗口标签)。

    起点是 0 表示不限（全部历史）。
    """
    raw = (spec or "24h").strip().lower()
    key = _ALIASES.get(raw)
    if key is None and raw.endswith("h"):
        # 任意小时数：48h / 72h
        try:
            hours = float(raw[:-1])
        except ValueError:
            hours = 0.0
        if hours > 0:
            t = now if now is not None else time.time()
            return t - hours * 3600.0, f"近{raw[:-1]}h" if hours != int(hours) else f"近{int(hours)}h"
    key = key or "24h"

    if key == "today":
        return today_start(now), _LABELS["today"]
    if key == "7d":
        t = now if now is not None else time.time()
        return t - 7 * DAY, _LABELS["7d"]
    if key == "30d":
        t = now if now is not None else time.time()
        return t - 30 * DAY, _LABELS["30d"]
    if key == "all":
        return 0.0, _LABELS["all"]
    t = now if now is not None else time.time()
    return t - DAY, _LABELS["24h"]


def window_for(spec, now: float | None = None) -> tuple[float, str]:
    """从 SourceSpec 取窗口。spec 为 None 或没配 options 时用默认 24h。"""
    opts = getattr(spec, "options", None) or {}
    return parse_window(opts.get("window"), now=now)
