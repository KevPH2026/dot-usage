"""每日趋势：近 N 天按天 × 模型聚合 token 消耗。

**为什么单独一个模块**：`pulse.py` 聚合的是「近 24 小时按小时」，
那是给墨水屏的「用量脉搏」柱状图用的；这里要的是「近 30 天按天 × 模型」，
给菜单栏 App 的趋势图用——跟 ZCode 自己那个「每日 Token 趋势图」一个口径。

模型名归一化跟 `models.py` 走同一套（`_norm_model` / `_infer_platform`），
这样趋势图里的模型名和模型面板里的对得上。
"""

from __future__ import annotations

import datetime
import glob
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

HOME = Path.home()

#: 默认看多少天
DEFAULT_DAYS = 30

#: 趋势图最多画几条线（再多就糊了），按总量排序取前 N
MAX_SERIES = 6


def _day_key(ts: float) -> str:
    """时间戳 → 本地日期字符串（YYYY-MM-DD）。"""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _day_range(days: int, now: float | None = None) -> list[str]:
    """最近 N 天的日期列表（升序，含今天）。"""
    now = now if now is not None else time.time()
    today = datetime.date.fromtimestamp(now)
    return [(today - datetime.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


class _Store:
    """按「归一化模型名 → 日期」存 token 明细（输入/输出/缓存分开），同时记来源。

    为什么要分开存：算钱要用输入和输出的**不同**单价，
    只记总数就没法折算成本。
    """

    def __init__(self) -> None:
        self.days: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0})
        )
        self.sources: dict[str, set[str]] = defaultdict(set)
        self.raw: dict[str, str] = {}   # 归一化名 → 原始名

    def add(
        self,
        model: str,
        source: str,
        day: str,
        tokens: int,
        inp: int = 0,
        outp: int = 0,
        cache_read: int = 0,
        cache_write: int = 0,
        cache_included: bool = False,
    ) -> None:
        if tokens <= 0 and inp <= 0 and outp <= 0:
            return
        from dotusage.core.models import _norm_model

        name = _norm_model(model) or "unknown"
        slot = self.days[name][day]
        # cache 含在 input 里时，记「净输入」和「缓存」分开，
        # 算钱时才不会重复计费。
        if cache_included:
            slot["input"] += max(0, int(inp) - int(cache_read))
        else:
            slot["input"] += int(inp)
        slot["output"] += int(outp)
        slot["cache_read"] += int(cache_read)
        slot["cache_write"] += int(cache_write)
        slot["total"] += int(tokens)
        self.sources[name].add(source)
        self.raw.setdefault(name, str(model or "unknown"))


def collect_daily(days: int = DEFAULT_DAYS, now: float | None = None) -> dict:
    """近 N 天按天 × 模型聚合。

    返回：
        {
          "days":   ["2026-10-07", ...],          # 升序，固定 N 天
          "series": [{"model","platform","icon","tokens":[...]}],
          "by_day": [n1, n2, ...],                 # 每天合计
        }
    """
    now = now if now is not None else time.time()
    since = now - days * 86400
    store = _Store()

    # ---- ZCode：db 里直接按天 GROUP BY，最准 ----
    db = HOME / ".zcode" / "cli" / "db" / "db.sqlite"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for day, model, tok, inp, outp, cread in con.execute(
                "select date(started_at/1000,'unixepoch','localtime'), model_id, "
                "sum(input_tokens+output_tokens), sum(input_tokens), sum(output_tokens), "
                "sum(cache_read_input_tokens) from model_usage "
                "where started_at > ? group by 1,2",
                (int(since * 1000),),
            ):
                store.add(str(model), "zcode", str(day), int(tok or 0),
                          int(inp or 0), int(outp or 0), int(cread or 0),
                          cache_included=True)
            con.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- MiniMax Code ----
    db = HOME / ".minimax" / "sqlite.db"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for day, model, tok, inp, outp, cread, cwrite in con.execute(
                "select date(ts/1000,'unixepoch','localtime'), model, "
                "sum(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens), "
                "sum(input_tokens), sum(output_tokens), sum(cache_read_tokens), "
                "sum(cache_write_tokens) from token_usage where ts > ? group by 1,2",
                (int(since * 1000),),
            ):
                store.add(str(model), "minimax", str(day), int(tok or 0),
                          int(inp or 0), int(outp or 0), int(cread or 0), int(cwrite or 0))
            con.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- Hermes ----
    db = HOME / ".hermes" / "state.db"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for day, model, tok, inp, outp, cread, cwrite in con.execute(
                "select date(s.ended_at,'unixepoch','localtime'), u.model, "
                "sum(u.input_tokens+u.output_tokens+u.cache_read_tokens+u.cache_write_tokens), "
                "sum(u.input_tokens), sum(u.output_tokens), sum(u.cache_read_tokens), "
                "sum(u.cache_write_tokens) "
                "from session_model_usage u left join sessions s on s.id=u.session_id "
                "where s.ended_at > ? group by 1,2",
                (since,),
            ):
                store.add(str(model), "hermes", str(day), int(tok or 0),
                          int(inp or 0), int(outp or 0), int(cread or 0), int(cwrite or 0))
            con.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- OpenCode（token 在 JSON 里，只能逐行解析）----
    db = HOME / ".local" / "share" / "opencode" / "opencode.db"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for ts, data in con.execute(
                "select time_created, data from message where time_created > ?",
                (int(since * 1000),),
            ):
                try:
                    d = json.loads(data)
                except Exception:  # noqa: BLE001
                    continue
                if d.get("role") != "assistant":
                    continue
                t = d.get("tokens") or {}
                ca = t.get("cache") or {}
                store.add(
                    str(d.get("modelID") or "unknown"), "opencode", _day_key(ts / 1000),
                    (t.get("input") or 0) + (t.get("output") or 0)
                    + (ca.get("read") or 0) + (ca.get("write") or 0),
                    int(t.get("input") or 0), int(t.get("output") or 0),
                    int(ca.get("read") or 0), int(ca.get("write") or 0),
                )
            con.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- Claude Code（本地会话 jsonl）----
    for f in glob.glob(str(HOME / ".claude/projects/**/*.jsonl"), recursive=True):
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    u = (d.get("message") or {}).get("usage")
                    if not isinstance(u, dict):
                        continue
                    ts = d.get("timestamp")
                    if not ts:
                        continue
                    try:
                        ep = datetime.datetime.fromisoformat(
                            str(ts).replace("Z", "+00:00")
                        ).timestamp()
                    except Exception:  # noqa: BLE001
                        continue
                    store.add(
                        str(d.get("model") or "unknown"), "claude", _day_key(ep),
                        (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
                        + (u.get("cache_creation_input_tokens") or 0)
                        + (u.get("cache_read_input_tokens") or 0),
                        int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                        int(u.get("cache_read_input_tokens") or 0),
                        int(u.get("cache_creation_input_tokens") or 0),
                    )
        except Exception:  # noqa: BLE001
            continue

    # ---- Codex（和 models.py 走同一个尾部扫描，两边口径一致）----
    try:
        from dotusage.core.models import codex_events_detail

        for ts, model, inp, outp, cread, total, _path in codex_events_detail(since):
            store.add(str(model or "unknown"), "codex", _day_key(ts or now),
                      int(total or 0), int(inp or 0), int(outp or 0), int(cread or 0),
                      cache_included=True)
    except Exception:  # noqa: BLE001
        pass

    # ---- 组装成契约友好的形状 ----
    from dotusage.core.models import _infer_platform

    day_list = _day_range(days, now)
    from dotusage.sources.pricing import estimate

    series = []
    for name, by_day in store.days.items():
        srcs = sorted(store.sources.get(name, ()))
        platform, icon = _infer_platform(name, srcs[0] if srcs else "")
        vals = []
        costs = []
        for d in day_list:
            slot = by_day.get(d) or {}
            vals.append(int(slot.get("total") or 0))
            c = estimate(
                name,
                int(slot.get("input") or 0),
                int(slot.get("output") or 0),
                int(slot.get("cache_read") or 0),
                cache_included=False,
            )
            costs.append(round(c, 4) if c is not None else None)
        series.append({
            "model": name,
            "platform": platform,
            "icon": icon,
            "tokens": vals,
            "cost": costs,
            "total": sum(vals),
            "total_cost": round(sum(c for c in costs if c), 2),
        })

    series.sort(key=lambda s: -s["total"])
    series = series[:MAX_SERIES]
    by_day_total = [sum(s["tokens"][i] for s in series) for i in range(len(day_list))]
    by_day_cost = [
        round(sum((s["cost"][i] or 0) for s in series), 4) for i in range(len(day_list))
    ]

    return {
        "days": day_list,
        "series": series,
        "by_day": by_day_total,
        "by_day_cost": by_day_cost,
        "total_cost": round(sum(s["total_cost"] for s in series), 2),
        "currency": "CNY",
    }
