"""各来源的采集器。

每个采集器只做一件事：把某个工具的本地数据读出来，归一化成 UsageRow。
读不到就返回空 + SourceReport(error=...)，绝不抛异常打断整体流程。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from dotusage.core.schema import SourceReport, UsageRow, now_ts, to_epoch, window_label

HOME = Path.home()
DAY = 86400


def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return None


def _query(db: Path, sql: str, args: tuple = ()) -> list[tuple]:
    """只读连接跑一条查询。库被锁 / 不存在 / 损坏都直接返回空。"""
    if not db.exists():
        raise FileNotFoundError(str(db))
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    try:
        con.row_factory = sqlite3.Row
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _tail_token_count(path: Path, tail_bytes: int = 8 * 1024 * 1024) -> tuple[dict | None, float]:
    """只读文件尾部找最后一个 token_count 事件。

    Codex 的 rollout 文件动辄几百 MB，全量扫会卡死；
    token_count 事件在文件里持续追加，所以读尾部就够了。
    事件结构是 {"type":"event_msg","payload":{"type":"token_count",...}}。
    """
    last = None
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()  # 丢掉被截断的半行
            for raw in fh:
                if b'"token_count"' not in raw:
                    continue
                try:
                    d = json.loads(raw.decode("utf-8", errors="ignore"))
                except Exception:
                    continue
                payload = d.get("payload")
                if isinstance(payload, dict) and payload.get("type") == "token_count":
                    last = d
                elif d.get("type") == "token_count":
                    last = {"timestamp": d.get("timestamp"), "payload": d}
    except Exception:
        return None, 0.0
    if not last:
        return None, 0.0
    ts = to_epoch(last.get("timestamp"))
    if not ts:
        ts = path.stat().st_mtime
    return last, ts


def _tail_token_events(path: Path, tail_bytes: int = 32 * 1024 * 1024) -> list[dict]:
    """读文件尾部，把所有 token_count 事件解析出来。

    Codex 的事件里有两个口径：
      - info.total_token_usage  = 本会话**累计**（跨事件一直变大）
      - info.last_token_usage   = 距上一个事件的**增量**（实测等于相邻 total 之差）

    所以算一段时间的消耗必须累加 last_token_usage，累加 total 会把历史重复计一遍。
    """
    events: list[dict] = []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()  # 丢掉被截断的半行
            for raw in fh:
                if b'"token_count"' not in raw:
                    continue
                try:
                    d = json.loads(raw.decode("utf-8", errors="ignore"))
                except Exception:
                    continue
                payload = d.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info") or {}
                events.append(
                    {
                        "ts": to_epoch(d.get("timestamp")),
                        "total": info.get("total_token_usage") or {},
                        "last": info.get("last_token_usage") or {},
                        "limits": payload.get("rate_limits") or {},
                    }
                )
    except Exception:
        return events
    return events


# --------------------------------------------------------------------------
# Codex —— 唯一带「配额百分比 + 重置时刻」的来源
# --------------------------------------------------------------------------
def collect_codex() -> tuple[list[UsageRow], SourceReport]:
    rep = SourceReport("codex")
    root = HOME / ".codex" / "sessions"
    if not root.exists():
        rep.ok = False
        rep.error = "未找到 ~/.codex/sessions"
        return [], rep

    try:
        files = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception as e:  # noqa: BLE001
        rep.ok = False
        rep.error = f"扫描失败 {e}"
        return [], rep

    fresh_cut = now_ts() - 7 * DAY
    per_file: list[tuple[Path, list[dict]]] = []
    for f in files[:12]:
        try:
            if f.stat().st_mtime < fresh_cut:
                break
        except OSError:
            continue
        evs = _tail_token_events(f)
        if evs:
            per_file.append((f, evs))

    if not per_file:
        rep.ok = False
        rep.error = "没有 token_count 事件"
        return [], rep

    all_events: list[tuple[float, dict, Path]] = []
    for f, evs in per_file:
        for e in evs:
            all_events.append((e["ts"], e, f))
    all_events.sort(key=lambda x: x[0], reverse=True)

    # 配额行取「最新且带 rate_limits」的事件（rate_limits 跟着最新请求回）
    best_ts, best_ev, best_file = all_events[0]
    for ts, e, f in all_events:
        lim = e.get("limits") or {}
        if lim.get("primary") or lim.get("secondary"):
            best_ts, best_ev, best_file = ts, e, f
            break

    limits = best_ev.get("limits") or {}
    last_usage = best_ev.get("last") or {}

    rows: list[UsageRow] = []

    # 配额窗口：primary / secondary 各代表一个窗口（周配额 + 5 小时窗口）
    for slot in ("primary", "secondary"):
        w = limits.get(slot)
        if not isinstance(w, dict):
            continue
        used = w.get("used_percent")
        if used is None:
            continue
        resets_at = to_epoch(w.get("resets_at"))
        minutes = w.get("window_minutes")
        rows.append(
            UsageRow(
                source="codex",
                label="Codex",
                model="",
                kind="quota",
                used_percent=float(used),
                window=window_label(minutes),
                resets_at=resets_at or None,
                total_tokens=int(last_usage.get("total_tokens") or 0),
                input_tokens=int(last_usage.get("input_tokens") or 0),
                output_tokens=int(last_usage.get("output_tokens") or 0),
                cache_read_tokens=int(last_usage.get("cached_input_tokens") or 0),
                updated_at=best_ts,
                path=str(best_file),
            )
        )

    # 消耗量：累加各会话事件的「增量」，只算 24h 内的
    # 注意：total_token_usage 是会话累计值，累加它会把历史重复计一遍（虚高好几倍）
    day_cut = now_ts() - DAY
    agg = {"inp": 0, "outp": 0, "cread": 0, "total": 0, "calls": 0, "last": 0.0}
    for ts, e, _f in all_events:
        if ts < day_cut:
            continue
        last = e.get("last") or {}
        agg["inp"] += int(last.get("input_tokens") or 0)
        agg["outp"] += int(last.get("output_tokens") or 0)
        agg["cread"] += int(last.get("cached_input_tokens") or 0)
        agg["total"] += int(last.get("total_tokens") or 0)
        agg["calls"] += 1
        agg["last"] = max(agg["last"], ts)

    if agg["total"] > 0:
        rows.append(
            UsageRow(
                source="codex",
                label="Codex 24h",
                model="",
                kind="consumption",
                input_tokens=agg["inp"],
                output_tokens=agg["outp"],
                cache_read_tokens=agg["cread"],
                total_tokens=agg["total"],
                updated_at=agg["last"] or now_ts(),
                note=f"{agg['calls']} 次请求",
                path=str(best_file),
            )
        )

    plan = limits.get("plan_type")
    if plan and rows:
        rows[0].note = str(plan)

    rep.rows = len(rows)
    return rows, rep


# --------------------------------------------------------------------------
# ZCode（Z.ai / GLM Coding Plan）—— 本地会话库里的真实用量
# --------------------------------------------------------------------------
def collect_zcode() -> tuple[list[UsageRow], SourceReport]:
    rep = SourceReport("zcode")
    db = HOME / ".zcode" / "cli" / "db" / "db.sqlite"
    if not db.exists():
        rep.ok = False
        rep.error = "未找到 ~/.zcode/cli/db/db.sqlite"
        return [], rep

    since = int((now_ts() - DAY) * 1000)
    sql = """
        SELECT model_id,
               COUNT(*)                      AS calls,
               SUM(input_tokens)             AS inp,
               SUM(output_tokens)            AS outp,
               SUM(reasoning_tokens)         AS reas,
               SUM(cache_creation_input_tokens) AS ccreate,
               SUM(cache_read_input_tokens)  AS cread,
               SUM(computed_total_tokens)    AS tot,
               MAX(started_at)               AS last
        FROM model_usage
        WHERE started_at > ?
        GROUP BY model_id
        ORDER BY tot DESC
    """
    try:
        raw = _query(db, sql, (since,))
    except Exception as e:  # noqa: BLE001
        rep.ok = False
        rep.error = f"查询失败 {e}"
        return [], rep

    rows: list[UsageRow] = []
    for r in raw:
        tot = int(r["tot"] or 0)
        if tot <= 0:
            continue
        rows.append(
            UsageRow(
                source="zcode",
                label=f"ZCode · {r['model_id']}",
                model=str(r["model_id"]),
                kind="consumption",
                input_tokens=int(r["inp"] or 0),
                output_tokens=int(r["outp"] or 0),
                cache_read_tokens=int(r["cread"] or 0),
                cache_write_tokens=int(r["ccreate"] or 0),
                reasoning_tokens=int(r["reas"] or 0),
                total_tokens=tot,
                calls=int(r["calls"] or 0),
                updated_at=(r["last"] or 0) / 1000,
                path=str(db),
            )
        )
    rep.rows = len(rows)
    return rows, rep


# --------------------------------------------------------------------------
# MiniMax Code —— 官方 token_usage 表
# --------------------------------------------------------------------------
def collect_minimax() -> tuple[list[UsageRow], SourceReport]:
    rep = SourceReport("minimax")
    db = HOME / ".minimax" / "sqlite.db"
    if not db.exists():
        rep.ok = False
        rep.error = "未找到 ~/.minimax/sqlite.db"
        return [], rep

    since = int((now_ts() - DAY) * 1000)
    sql = """
        SELECT model,
               COUNT(*)                    AS calls,
               SUM(input_tokens)           AS inp,
               SUM(output_tokens)          AS outp,
               SUM(reasoning_tokens)       AS reas,
               SUM(cache_read_tokens)      AS cread,
               SUM(cache_write_tokens)     AS cwrite,
               SUM(cost_usd)               AS cost,
               MAX(ts)                     AS last
        FROM token_usage
        WHERE ts > ?
        GROUP BY model
        ORDER BY (SUM(input_tokens)+SUM(output_tokens)) DESC
    """
    try:
        raw = _query(db, sql, (since,))
    except Exception as e:  # noqa: BLE001
        rep.ok = False
        rep.error = f"查询失败 {e}"
        return [], rep

    rows: list[UsageRow] = []
    for r in raw:
        total = int(r["inp"] or 0) + int(r["outp"] or 0)
        if total <= 0:
            continue
        rows.append(
            UsageRow(
                source="minimax",
                label=f"MiniMax · {r['model']}",
                model=str(r["model"]),
                kind="consumption",
                input_tokens=int(r["inp"] or 0),
                output_tokens=int(r["outp"] or 0),
                cache_read_tokens=int(r["cread"] or 0),
                cache_write_tokens=int(r["cwrite"] or 0),
                reasoning_tokens=int(r["reas"] or 0),
                total_tokens=total,
                cost_usd=float(r["cost"] or 0) or None,
                calls=int(r["calls"] or 0),
                updated_at=(r["last"] or 0) / 1000,
                path=str(db),
            )
        )
    rep.rows = len(rows)
    if not rows:
        rep.note = "24h 内无记录"
    return rows, rep


# --------------------------------------------------------------------------
# Claude Code —— 本地会话 jsonl
# --------------------------------------------------------------------------
def collect_claude() -> tuple[list[UsageRow], SourceReport]:
    rep = SourceReport("claude")
    root = HOME / ".claude" / "projects"
    if not root.exists():
        rep.ok = False
        rep.error = "未找到 ~/.claude/projects"
        return [], rep

    cutoff = now_ts() - DAY
    agg: dict[str, dict] = defaultdict(lambda: {"inp": 0, "outp": 0, "ccreate": 0, "cread": 0, "calls": 0, "last": 0.0})
    scanned = 0
    try:
        files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:400]
    except Exception as e:  # noqa: BLE001
        rep.ok = False
        rep.error = f"扫描失败 {e}"
        return [], rep

    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        scanned += 1
        try:
            with f.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    msg = d.get("message") or {}
                    u = msg.get("usage")
                    if not isinstance(u, dict):
                        continue
                    ts = to_epoch(d.get("timestamp"))
                    if ts and ts < cutoff:
                        continue
                    model = str(msg.get("model") or "unknown")
                    a = agg[model]
                    a["inp"] += int(u.get("input_tokens") or 0)
                    a["outp"] += int(u.get("output_tokens") or 0)
                    a["ccreate"] += int(u.get("cache_creation_input_tokens") or 0)
                    a["cread"] += int(u.get("cache_read_input_tokens") or 0)
                    a["calls"] += 1
                    a["last"] = max(a["last"], ts)
        except Exception:
            continue

    rows: list[UsageRow] = []
    for model, a in agg.items():
        total = a["inp"] + a["outp"] + a["ccreate"] + a["cread"]
        if total <= 0:
            continue
        rows.append(
            UsageRow(
                source="claude",
                label=f"Claude Code · {model}",
                model=model,
                kind="consumption",
                input_tokens=a["inp"],
                output_tokens=a["outp"],
                cache_read_tokens=a["cread"],
                cache_write_tokens=a["ccreate"],
                total_tokens=total,
                calls=a["calls"],
                updated_at=a["last"] or now_ts(),
                note="走中转，无配额",
                path=str(root),
            )
        )
    rows.sort(key=lambda r: r.total_tokens, reverse=True)
    rep.rows = len(rows)
    if not rows:
        rep.note = f"24h 内无会话（扫了 {scanned} 个文件）"
    return rows, rep


# --------------------------------------------------------------------------
# OpenCode —— 466MB 的 sqlite 里挑出最近 24h
# --------------------------------------------------------------------------
def collect_opencode() -> tuple[list[UsageRow], SourceReport]:
    rep = SourceReport("opencode")
    db = HOME / ".local" / "share" / "opencode" / "opencode.db"
    if not db.exists():
        rep.ok = False
        rep.error = "未找到 opencode.db"
        return [], rep

    since = int((now_ts() - DAY) * 1000)
    sql = "SELECT time_created, data FROM message WHERE time_created > ?"
    try:
        raw = _query(db, sql, (since,))
    except Exception as e:  # noqa: BLE001
        rep.ok = False
        rep.error = f"查询失败 {e}"
        return [], rep

    agg: dict[tuple[str, str], dict] = defaultdict(lambda: {"inp": 0, "outp": 0, "reas": 0, "cread": 0, "cwrite": 0, "cost": 0.0, "calls": 0, "last": 0.0})
    for r in raw:
        d = _read_json_line(r["data"])
        if not d:
            continue
        if d.get("role") != "assistant":
            continue
        t = d.get("tokens") or {}
        cache = t.get("cache") or {}
        key = (str(d.get("providerID") or "?"), str(d.get("modelID") or "?"))
        a = agg[key]
        a["inp"] += int(t.get("input") or 0)
        a["outp"] += int(t.get("output") or 0)
        a["reas"] += int(t.get("reasoning") or 0)
        a["cread"] += int(cache.get("read") or 0)
        a["cwrite"] += int(cache.get("write") or 0)
        a["cost"] += float(d.get("cost") or 0)
        a["calls"] += 1
        a["last"] = max(a["last"], float(r["time_created"] or 0) / 1000)

    rows: list[UsageRow] = []
    for (provider, model), a in agg.items():
        total = a["inp"] + a["outp"] + a["cread"] + a["cwrite"]
        if total <= 0:
            continue
        rows.append(
            UsageRow(
                source="opencode",
                label=f"OpenCode · {model}",
                model=model,
                kind="consumption",
                input_tokens=a["inp"],
                output_tokens=a["outp"],
                cache_read_tokens=a["cread"],
                cache_write_tokens=a["cwrite"],
                reasoning_tokens=a["reas"],
                total_tokens=total,
                cost_usd=a["cost"] or None,
                calls=a["calls"],
                updated_at=now_ts(),
                note=provider,
                path=str(db),
            )
        )
    rows.sort(key=lambda r: r.total_tokens, reverse=True)
    rep.rows = len(rows)
    if not rows:
        rep.note = "24h 内无记录"
    return rows, rep


def _read_json_line(s: str):
    try:
        return json.loads(s)
    except Exception:
        return None


# --------------------------------------------------------------------------
# Hermes —— 跨 provider 的统一账本（兜底）
# --------------------------------------------------------------------------
def collect_hermes() -> tuple[list[UsageRow], SourceReport]:
    rep = SourceReport("hermes")
    db = HOME / ".hermes" / "state.db"
    if not db.exists():
        rep.ok = False
        rep.error = "未找到 ~/.hermes/state.db"
        return [], rep

    since = now_ts() - DAY
    sql = """
        SELECT billing_provider, model,
               SUM(api_call_count)   AS calls,
               SUM(input_tokens)     AS inp,
               SUM(output_tokens)    AS outp,
               SUM(cache_read_tokens) AS cread,
               SUM(cache_write_tokens) AS cwrite,
               SUM(reasoning_tokens) AS reas,
               SUM(estimated_cost_usd) AS cost,
               MAX(last_seen)        AS last
        FROM session_model_usage
        WHERE last_seen > ?
        GROUP BY billing_provider, model
        ORDER BY (SUM(input_tokens)+SUM(output_tokens)) DESC
    """
    try:
        raw = _query(db, sql, (since,))
    except Exception as e:  # noqa: BLE001
        rep.ok = False
        rep.error = f"查询失败 {e}"
        return [], rep

    rows: list[UsageRow] = []
    for r in raw:
        provider = str(r["billing_provider"] or "?")
        total = int(r["inp"] or 0) + int(r["outp"] or 0)
        if total <= 0:
            continue
        rows.append(
            UsageRow(
                source="hermes",
                label=f"{provider} · {r['model']}",
                model=str(r["model"]),
                kind="consumption",
                input_tokens=int(r["inp"] or 0),
                output_tokens=int(r["outp"] or 0),
                cache_read_tokens=int(r["cread"] or 0),
                cache_write_tokens=int(r["cwrite"] or 0),
                reasoning_tokens=int(r["reas"] or 0),
                total_tokens=total,
                cost_usd=float(r["cost"] or 0) or None,
                calls=int(r["calls"] or 0),
                updated_at=float(r["last"] or now_ts()),
                note="Hermes",
                path=str(db),
            )
        )
    rep.rows = len(rows)
    if not rows:
        rep.note = "24h 内无记录"
    return rows, rep


COLLECTORS = [
    ("codex", collect_codex),
    ("zcode", collect_zcode),
    ("minimax", collect_minimax),
    ("claude", collect_claude),
    ("opencode", collect_opencode),
    ("hermes", collect_hermes),
]


def collect_all() -> tuple[list[UsageRow], list[SourceReport]]:
    rows: list[UsageRow] = []
    reports: list[SourceReport] = []
    for name, fn in COLLECTORS:
        try:
            r, rep = fn()
        except Exception as e:  # noqa: BLE001
            r, rep = [], SourceReport(name, ok=False, error=f"异常 {e}")
        rows.extend(r)
        reports.append(rep)
    return rows, reports


def collect_all_with_official() -> tuple[list[UsageRow], list[SourceReport]]:
    """本地采集 + 官方接口采集 + Tibo 重置监测。

    官方源优先：同一个源既有本地日志又有官方接口时（比如 Codex），
    以官方口径为准，本地口径降级成「补充」。
    """
    from dotusage.official.providers import collect_official
    from dotusage.core.tibo import collect_tibo

    # 走插件化加载器：内置采集器 + 用户配置里定义的源
    from .loader import collect_sources, official_provider_names

    rows, reports = collect_sources()
    off_rows, off_reports = collect_official(official_provider_names())

    tibo_row, tibo_rep = collect_tibo()
    if tibo_row is not None:
        off_rows = off_rows + [tibo_row]
    off_reports = off_reports + [tibo_rep]

    # 官方源覆盖到的 source，本地同名行不再重复展示
    covered = {r.source for r in off_rows}
    local_only = [r for r in rows if r.source not in covered]

    # ChatGPT 官方接口返回的就是 Codex 订阅的那个配额窗口，
    # 两者同源，别在卡上显示两遍一样的百分比：官方优先，本地配额行让位。
    if any(r.source == "chatgpt" for r in off_rows):
        local_only = [r for r in local_only if not (r.source == "codex" and r.kind == "quota")]

    return local_only + off_rows, reports + off_reports


if __name__ == "__main__":
    rs, reps = collect_all()
    for rep in reps:
        print(f"{rep.name:10} ok={rep.ok} rows={rep.rows} {rep.error or rep.note}")
    print("-" * 60)
    for r in sorted(rs, key=lambda x: (x.kind != "quota", -x.total_tokens)):
        print(f"{r.kind:11} {r.label:28} {r.headline:>8}  {r.detail}")


# --------------------------------------------------------------------------
# 内置采集器注册表
# --------------------------------------------------------------------------
_BUILTINS = {
    "codex": collect_codex,
    "zcode": collect_zcode,
    "minimax": collect_minimax,
    "claude": collect_claude,
    "opencode": collect_opencode,
    "hermes": collect_hermes,
}


def collect_builtin(name: str, spec=None):
    """按名字跑内置采集器。spec 只用来改 source 名和标题。"""
    fn = _BUILTINS.get(name)
    if fn is None:
        from ..core.schema import SourceReport

        rep = SourceReport(name)
        rep.ok = False
        rep.error = f"没有这个内置采集器：{name}"
        return [], rep
    rows, rep = fn()
    if spec is not None:
        for r in rows:
            r.source = spec.id
        rep.name = spec.id
    return rows, rep
