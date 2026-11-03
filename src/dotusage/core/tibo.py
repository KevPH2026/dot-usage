"""Tibo 重置监测。

背景：OpenAI Codex/ChatGPT 负责人 Thibault Sottiaux（X: @thsottiaux）
会在 X 上宣布「广域额度重置」，社区管这叫 **Tibo reset**。

数据源用社区追踪站 **codex-reset.com** 的公开 API（它自己去抓 X 并做了分类）：

| 端点 | 内容 |
|---|---|
| `/api/feed` | 最新推文 + 每条的 `kind` 分类 + reset 信号 |
| `/api/timeline` | 历史重置事件线（type/summary/url/scope/confidence） |
| `/api/forecast` | 重置概率预测（24h/48h）、节奏、上次重置时间 |

**判定不能只看关键词**：Tibo 也会发 "Not enough resets"、"I demand a reset" 这种闲聊。
追踪站已经用分类器（reset-regex-v7）分好了 kind：

- `candidate` → 真正的重置宣布（"today is a reset"、"it's reset day today"）
- `signal`   → 预告（"Lands by midnight PST"）
- `other`    → 闲聊，别算

重要前提（社区共识）：**没有 Tibo 帖 ≠ 平台没重置**（OpenAI 也会静默重置）。
所以卡片上 NO 的含义是「今天没有官宣」，不是「今天一定没重置」。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from dotusage.core.schema import SourceReport, UsageRow, CST, now_ts, to_epoch

BASE = "https://codex-reset.com/api"
TIMEOUT = 20

# 真正算「重置」的推文分类
RESET_KINDS = {"candidate", "reset", "announced"}


def _get(path: str) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Accept": "application/json", "User-Agent": "dot-usage/1.0"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="ignore"))


def _local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d")


def collect_tibo() -> tuple[UsageRow | None, SourceReport]:
    rep = SourceReport("tibo")
    try:
        feed = _get("/feed")
    except Exception as e:  # noqa: BLE001
        rep.ok = False
        rep.error = f"取 feed 失败 {str(e)[:80]}"
        return None, rep

    tweets = feed.get("tweets") or []
    now = now_ts()
    today = _local_day(now)

    # 今天有没有官宣重置
    today_hits = [
        t
        for t in tweets
        if str(t.get("kind")) in RESET_KINDS
        and t.get("at")
        and _local_day(to_epoch(t.get("at"))) == today
    ]

    # 历史最近一次重置（往前找最近的 candidate / signal）
    recent: list[tuple[float, dict]] = []
    for t in tweets:
        if str(t.get("kind")) in RESET_KINDS and t.get("at"):
            recent.append((to_epoch(t.get("at")), t))
    recent.sort(key=lambda x: x[0], reverse=True)

    last_at, last_tweet = (recent[0] if recent else (0.0, {}))

    # timeline 里的事件（社区交叉验证）
    last_event = None
    try:
        tl = _get("/timeline")
        evs = [e for e in (tl.get("events") or []) if str(e.get("type")) == "reset"]
        evs.sort(key=lambda e: to_epoch(e.get("announced_at")) or 0, reverse=True)
        if evs:
            last_event = evs[0]
    except Exception:
        pass

    # 事件时间以 timeline 为准（社区核对过），没有就用推文
    ev_at = to_epoch(last_event.get("announced_at")) if last_event else 0.0
    reset_at = ev_at or last_at
    summary = ""
    if last_event and last_event.get("summary"):
        summary = str(last_event["summary"])
    elif last_tweet.get("text"):
        summary = str(last_tweet["text"])

    url = ""
    if last_event and last_event.get("url"):
        url = str(last_event["url"])
    elif last_tweet.get("url"):
        url = str(last_tweet["url"])

    # 预测
    prob24 = prob48 = None
    cadence = ""
    window = ""
    try:
        fc = _get("/forecast")
        probs = fc.get("probabilities") or {}
        prob24 = probs.get("rounded_24h")
        prob48 = probs.get("rounded_48h")
        tw = fc.get("time_window") or {}
        window = str(tw.get("label") or "")
        cad = fc.get("cadence") or {}
        cad_days = cad.get("recent_median_days")
        if cad_days:
            cadence = f"近期间隔中位 {cad_days} 天"
    except Exception:
        pass

    reset_today = bool(today_hits)

    # 明细文案
    if reset_today:
        t = today_hits[0]
        detail_bits = [f"今天 {datetime.fromtimestamp(to_epoch(t.get('at')), CST).strftime('%H:%M')} 官宣"]
        if summary:
            detail_bits.append(summary[:40])
    else:
        detail_bits = []
        if reset_at:
            hrs = (now - reset_at) / 3600
            when = datetime.fromtimestamp(reset_at, CST).strftime("%m-%d %H:%M")
            detail_bits.append(f"上次 {when}（{hrs:.0f}小时前）")

    detail2_bits = []
    if prob24 is not None:
        detail2_bits.append(f"24h {prob24}%")
    if prob48 is not None:
        detail2_bits.append(f"48h {prob48}%")
    if window:
        detail2_bits.append(f"窗口 {window}")

    # 时间窗口换成北京时间（Tibo 的窗口是 UTC）
    window_bj = window
    if window:
        try:
            nums = [int(x) for x in window.replace("AM", "").replace("PM", "").replace(":", " ").split() if x.isdigit()]
            if len(nums) >= 2:
                window_bj = f"窗口 {nums[0] + 8:02d}-{nums[1] + 8:02d} 北京"
        except Exception:
            window_bj = f"窗口 {window}"
    detail2_bits = [x.replace(f"窗口 {window}", window_bj) if window_bj != window else x for x in detail2_bits]

    headline = "YES" if reset_today else "NO"

    row = UsageRow(
        source="tibo",
        label="Tibo 重置了吗",
        model="",
        kind="tibo",
        used_percent=None,
        headline_override=headline,
        detail_override=" · ".join(detail_bits),
        detail2_override=" · ".join(detail2_bits),
        updated_at=now,
        note=url,
        icon="tibo",
        path="codex-reset.com/api/feed",
    )

    rep.rows = 1
    if feed.get("stale"):
        rep.note = "追踪站数据偏旧"
    return row, rep
