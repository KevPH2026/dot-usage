"""按**模型**维度统计 token 消耗：今天（近 24h）+ 历史累计。

和 aggregate.py 是两种视角，别混：

    平台视角（墨水屏卡片 / 面板默认）  ChatGPT 配额用了 77%
    模型视角（面板切过去看）          gpt-6-astra 今天烧 12.3M，累计 6.2B

归并口径：**按模型名归并**，平台由模型名推断（GLM-5.3 无论从 ZCode 还是
Hermes 走都是同一个模型；Claude Code 里挂的 glm-5.2 也算 GLM，不算 Claude）。

各来源怎么算「历史累计」：

    zcode / minimax / hermes / opencode   SQLite 直接 GROUP BY 求和，全量准
    claude                                jsonl 全量扫（244KB，随便扫）
    codex                                 ~/.codex/sessions 有 5GB / 667 个 rollout 文件，
                                          全量扫太慢 → 用**增量缓存**：
                                          每个文件记一次 (mtime, size, 累计 token, 模型)，
                                          文件没变就直接用缓存值，只有变了的才重扫。

Codex 的「累计」用的是 **last_token_usage 增量求和**，不是 total_token_usage：
后者是会话累计值，fork 出来的子会话会把父会话的历史再算一遍，虚高。
增量求和 = 账号真实被计费的 token（fork 会重发上下文，那部分确实又计了一次费）。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from dotusage.core.schema import now_ts, to_epoch

HOME = Path.home()
DAY = 86400.0

CODEX_CACHE = HOME / ".local" / "state" / "dot-usage" / "codex_totals.json"
# 缓存里存了每个 rollout 文件用的模型，codex_events 靠它做归属
_CODEX_CACHE: dict = {}

# 模型名前缀 → (平台名, 图标名)。图标只对有真品牌 logo 的平台给，
# 其余留空（面板里就不画图标），别拿别的家的 logo 凑数。
MODEL_PLATFORM_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("gpt-", "codex-", "chatgpt-", "o1", "o3", "o4"), "ChatGPT", "chatgpt"),
    (("glm-", "chatglm", "z-ai"), "Z.ai", "zai"),
    (("minimax", "abab"), "MiniMax", "minimax"),
    (("kimi", "moonshot"), "Kimi", ""),
    (("claude", "sonnet", "opus", "haiku"), "Claude Code", ""),
    (("doubao",), "豆包", ""),
    (("grok",), "Grok", "grok"),
    (("deepseek",), "DeepSeek", "deepseek"),
]


def _norm_model(model: str) -> str:
    """归一化模型名：去掉 provider 前缀（minimax/MiniMax-M3 → MiniMax-M3），统一小写做 key。"""
    m = str(model or "").strip()
    if "/" in m:
        m = m.split("/")[-1]
    return m


def _infer_platform(model: str, source: str = "") -> tuple[str, str]:
    """模型名 → (平台名, 图标名)。认不出来就退回来源名。"""
    m = _norm_model(model).lower()
    for prefixes, platform, icon in MODEL_PLATFORM_RULES:
        for p in prefixes:
            if m.startswith(p):
                return platform, icon
    fallback = {
        "openai-codex": ("ChatGPT", "chatgpt"),
        "zai": ("Z.ai", "zai"),
        "zcode": ("Z.ai", "zai"),
        "codex": ("ChatGPT", "chatgpt"),
        "minimax": ("MiniMax", "minimax"),
        "custom:minimax": ("MiniMax", "minimax"),
        "claude": ("Claude Code", ""),
        "opencode": ("OpenCode", ""),
        "hermes": ("Hermes", ""),
    }
    return fallback.get(source, (source or "其他", ""))


@dataclass
class ModelUsage:
    """一个模型在一段时间里的 token 用量。"""

    platform: str
    model: str
    icon: str = ""
    # 数据来源（zcode / codex / minimax …）。算钱时要用：
    # 有些源的 input_tokens 含缓存读，有些不含，语义不一样。
    source: str = ""
    today_tokens: int = 0
    today_calls: int = 0
    today_cost: float = 0.0
    total_tokens: int = 0
    total_calls: int = 0
    total_cost: float = 0.0
    # 输入/输出/缓存读的 token 数（累计）。
    # 算钱要用：输入和输出单价不一样，只记总数折不出价。
    total_input: int = 0
    total_output: int = 0
    total_cache_read: int = 0
    # estimated = True 表示这个成本是按官方 API 单价**折算**的，
    # 不是账单上的钱（订阅制工具本来没有按次计费）。
    estimated: bool = False
    last_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "model": self.model,
            "source": self.source,
            "icon": self.icon,
            "today_tokens": self.today_tokens,
            "today_calls": self.today_calls,
            "today_cost": round(self.today_cost, 4),
            "total_tokens": self.total_tokens,
            "total_calls": self.total_calls,
            "total_cost": round(self.total_cost, 4),
            "total_input": self.total_input,
            "total_output": self.total_output,
            "total_cache_read": self.total_cache_read,
            "estimated": self.estimated,
            "last_at": self.last_at,
        }


def _merge(bucket: dict[str, ModelUsage], source: str, model: str,
           *, today_tokens=0, today_calls=0, today_cost=0.0,
           total_tokens=0, total_calls=0, total_cost=0.0, last_at=0.0,
           total_input=0, total_output=0, total_cache_read=0) -> None:
    """按归一化后的模型名累加。"""
    name = _norm_model(model) or "unknown"
    key = name.lower()
    u = bucket.get(key)
    if u is None:
        platform, icon = _infer_platform(name, source)
        u = ModelUsage(platform=platform, model=name, icon=icon, source=source)
        bucket[key] = u
    elif source and not u.source:
        u.source = source
    u.today_tokens += today_tokens
    u.today_calls += today_calls
    u.today_cost += today_cost
    u.total_tokens += total_tokens
    u.total_calls += total_calls
    u.total_cost += total_cost
    u.total_input += total_input
    u.total_output += total_output
    u.total_cache_read += total_cache_read
    if last_at:
        u.last_at = max(u.last_at, last_at)


def _rows(db: Path, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


# --------------------------------------------------------------------------
# ZCode（Z.ai / GLM Coding Plan）
# --------------------------------------------------------------------------
def _zcode(bucket: dict) -> None:
    db = HOME / ".zcode" / "cli" / "db" / "db.sqlite"
    if not db.exists():
        return
    since_ms = int((now_ts() - DAY) * 1000)
    # input_tokens 已含缓存命中部分，所以总量就是 input + output
    for r in _rows(
        db,
        "SELECT model_id,"
        " SUM(input_tokens + output_tokens) AS tok,"
        " COUNT(*) AS calls,"
        " SUM(CASE WHEN started_at > ? THEN input_tokens + output_tokens ELSE 0 END) AS tok24,"
        " SUM(CASE WHEN started_at > ? THEN 1 ELSE 0 END) AS calls24,"
        " SUM(input_tokens) AS inp,"
        " SUM(output_tokens) AS outp,"
        " SUM(cache_read_input_tokens) AS cread,"
        " MAX(started_at) AS last_at"
        " FROM model_usage GROUP BY model_id",
        (since_ms, since_ms),
    ):
        _merge(
            bucket, "zcode", str(r["model_id"] or "unknown"),
            today_tokens=int(r["tok24"] or 0),
            today_calls=int(r["calls24"] or 0),
            total_tokens=int(r["tok"] or 0),
            total_calls=int(r["calls"] or 0),
            last_at=float(r["last_at"] or 0) / 1000,
            total_input=int(r["inp"] or 0),
            total_output=int(r["outp"] or 0),
            total_cache_read=int(r["cread"] or 0),
        )


# --------------------------------------------------------------------------
# MiniMax Code
# --------------------------------------------------------------------------
def _minimax(bucket: dict) -> None:
    db = HOME / ".minimax" / "sqlite.db"
    if not db.exists():
        return
    since_ms = int((now_ts() - DAY) * 1000)
    for r in _rows(
        db,
        "SELECT model,"
        " SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens) AS tok,"
        " COUNT(*) AS calls, SUM(cost_usd) AS cost, MAX(ts) AS last_at,"
        " SUM(CASE WHEN ts > ? THEN input_tokens + output_tokens + cache_read_tokens + cache_write_tokens ELSE 0 END) AS tok24,"
        " SUM(CASE WHEN ts > ? THEN 1 ELSE 0 END) AS calls24,"
        " SUM(CASE WHEN ts > ? THEN cost_usd ELSE 0 END) AS cost24"
        " FROM token_usage GROUP BY model",
        (since_ms, since_ms, since_ms),
    ):
        _merge(
            bucket, "minimax", str(r["model"] or "unknown"),
            today_tokens=int(r["tok24"] or 0),
            today_calls=int(r["calls24"] or 0),
            today_cost=float(r["cost24"] or 0),
            total_tokens=int(r["tok"] or 0),
            total_calls=int(r["calls"] or 0),
            total_cost=float(r["cost"] or 0),
            last_at=float(r["last_at"] or 0) / 1000,
        )


# --------------------------------------------------------------------------
# Hermes（跨 provider 的账本）
# --------------------------------------------------------------------------
def _hermes(bucket: dict) -> None:
    db = HOME / ".hermes" / "state.db"
    if not db.exists():
        return
    since = now_ts() - DAY
    # session_model_usage 是按会话聚合的，没有逐次时间戳，
    # 所以「今天」用 sessions.ended_at 判断（会话落在 24h 内就算今天）
    sql = (
        "SELECT u.billing_provider AS provider, u.model AS model,"
        " SUM(u.input_tokens + u.output_tokens + u.cache_read_tokens + u.cache_write_tokens) AS tok,"
        " SUM(u.api_call_count) AS calls, SUM(u.actual_cost_usd) AS cost,"
        " SUM(CASE WHEN s.ended_at > ? THEN u.input_tokens + u.output_tokens"
        "          + u.cache_read_tokens + u.cache_write_tokens ELSE 0 END) AS tok24,"
        " SUM(CASE WHEN s.ended_at > ? THEN u.api_call_count ELSE 0 END) AS calls24,"
        " MAX(s.ended_at) AS last_at"
        " FROM session_model_usage u"
        " LEFT JOIN sessions s ON s.id = u.session_id"
        " GROUP BY u.billing_provider, u.model"
    )
    for r in _rows(db, sql, (since, since)):
        _merge(
            bucket, str(r["provider"] or "hermes"), str(r["model"] or "unknown"),
            today_tokens=int(r["tok24"] or 0),
            today_calls=int(r["calls24"] or 0),
            total_tokens=int(r["tok"] or 0),
            total_calls=int(r["calls"] or 0),
            total_cost=float(r["cost"] or 0),
            last_at=float(r["last_at"] or 0),
        )


# --------------------------------------------------------------------------
# OpenCode
# --------------------------------------------------------------------------
def _opencode(bucket: dict) -> None:
    db = HOME / ".local" / "share" / "opencode" / "opencode.db"
    if not db.exists():
        return
    since_ms = int((now_ts() - DAY) * 1000)
    # role 在 data JSON 里，SQL 过滤不了；消息总量不大（8 千条），全量拉自己筛
    for r in _rows(db, "SELECT time_created, data FROM message"):
        try:
            d = json.loads(r["data"])
        except Exception:  # noqa: BLE001
            continue
        if d.get("role") != "assistant":
            continue
        t = d.get("tokens") or {}
        cache = t.get("cache") or {}
        tok = (int(t.get("input") or 0) + int(t.get("output") or 0)
               + int(cache.get("read") or 0) + int(cache.get("write") or 0))
        if tok <= 0:
            continue
        cost = float(d.get("cost") or 0)
        created = float(r["time_created"] or 0) / 1000
        is_today = created >= since_ms / 1000
        _merge(
            bucket, "opencode", str(d.get("modelID") or "?"),
            today_tokens=tok if is_today else 0,
            today_calls=1 if is_today else 0,
            today_cost=cost if is_today else 0.0,
            total_tokens=tok,
            total_calls=1,
            total_cost=cost,
            last_at=created,
        )


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------
def _claude(bucket: dict) -> None:
    root = HOME / ".claude" / "projects"
    if not root.exists():
        return
    cutoff = now_ts() - DAY
    try:
        files = list(root.rglob("*.jsonl"))
    except Exception:  # noqa: BLE001
        return
    for f in files:
        try:
            with f.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    msg = d.get("message") or {}
                    u = msg.get("usage")
                    if not isinstance(u, dict):
                        continue
                    ts = to_epoch(d.get("timestamp"))
                    tok = (int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
                           + int(u.get("cache_creation_input_tokens") or 0)
                           + int(u.get("cache_read_input_tokens") or 0))
                    if tok <= 0:
                        continue
                    is_today = bool(ts and ts >= cutoff)
                    _merge(
                        bucket, "claude", str(msg.get("model") or "unknown"),
                        today_tokens=tok if is_today else 0,
                        today_calls=1 if is_today else 0,
                        total_tokens=tok,
                        total_calls=1,
                        last_at=ts or 0,
                    )
        except Exception:  # noqa: BLE001
            continue


# --------------------------------------------------------------------------
# Codex —— 大头，5GB / 667 个 rollout 文件，用增量缓存
# --------------------------------------------------------------------------
def _codex_model_in(line: bytes) -> str:
    """从 turn_context 行里抠出模型名（只认 payload.model，不猜）。"""
    try:
        d = json.loads(line)
        pl = d.get("payload") or {}
        m = pl.get("model")
        if m:
            return str(m)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _codex_scan(path: Path) -> tuple[int, int, str, int, int, int]:
    """扫一个 rollout 文件，返回 (累计 token, 累计请求数, 模型名, 输入, 输出, 缓存读)。

    只对含 token_count / turn_context 的行做 JSON 解析，其余行只做字节包含判断。
    模型名取**最后一次** turn_context（会话中途可能换过模型）。

    输入/输出/缓存要分开记：算钱要用不同的单价，只记总数折不出价。
    """
    total = 0
    calls = 0
    model = ""
    inp = 0
    outp = 0
    cread = 0
    try:
        with path.open("rb") as fh:
            buf = b""
            while True:
                chunk = fh.read(8 << 20)
                if not chunk:
                    break
                buf += chunk
                lines = buf.split(b"\n")
                buf = lines.pop()
                for ln in lines:
                    if b'"turn_context"' in ln:
                        m = _codex_model_in(ln)
                        if m:
                            model = m
                    if b'"token_count"' not in ln:
                        continue
                    try:
                        d = json.loads(ln)
                    except Exception:  # noqa: BLE001
                        continue
                    last = _codex_last(d)
                    total += int(last.get("total_tokens") or 0)
                    inp += int(last.get("input_tokens") or 0)
                    outp += int(last.get("output_tokens") or 0)
                    cread += int(last.get("cached_input_tokens") or 0)
                    calls += 1
            if buf:
                if b'"turn_context"' in buf:
                    m = _codex_model_in(buf)
                    if m:
                        model = m
                if b'"token_count"' in buf:
                    try:
                        d = json.loads(buf)
                        last = _codex_last(d)
                        total += int(last.get("total_tokens") or 0)
                        inp += int(last.get("input_tokens") or 0)
                        outp += int(last.get("output_tokens") or 0)
                        cread += int(last.get("cached_input_tokens") or 0)
                        calls += 1
                    except Exception:  # noqa: BLE001
                        pass
    except Exception:  # noqa: BLE001
        pass
    return total, calls, model, inp, outp, cread


def _codex_last(d: dict) -> dict:
    pl = d.get("payload") or {}
    info = pl.get("info") or {}
    return info.get("last_token_usage") or pl.get("last_token_usage") or {}


def _cache_entry_ok(entry) -> bool:
    """缓存条目必须是**新格式**才算数。

    踩过的坑：早期版本往同一份缓存里写过另一种结构——
    `t` 是 0、`c` 存的是**工作目录**、模型名在 `d`。
    老代码 `int(e.get("c") or 0)` 遇到这种直接抛 ValueError，
    而 collect_model_usage 又是 `except Exception: continue`，
    结果整个 codex 聚合静默中断，面板上只剩几千 K 的零碎数字。

    所以这里显式校验格式：不对就当没有，重扫一遍。
    """
    if not isinstance(entry, dict):
        return False
    if not isinstance(entry.get("t"), int):
        return False
    if not isinstance(entry.get("c"), int):
        return False
    if not isinstance(entry.get("d"), str) or not entry.get("d"):
        return False
    return True


def _codex(bucket: dict) -> None:
    root = HOME / ".codex" / "sessions"
    if not root.exists():
        return
    try:
        files = sorted(root.rglob("rollout-*.jsonl"))
    except Exception:  # noqa: BLE001
        return
    if not files:
        return

    # ---- 增量缓存：只重扫变了的文件 ----
    cache: dict = {}
    try:
        if CODEX_CACHE.exists():
            cache = json.loads(CODEX_CACHE.read_text(encoding="utf-8")).get("files") or {}
    except Exception:  # noqa: BLE001
        cache = {}

    changed = 0
    for p in files:
        key = str(p)
        try:
            st = p.stat()
        except OSError:
            continue
        old = cache.get(key)
        if (
            old
            and _cache_entry_ok(old)
            and int(old.get("m", 0)) == int(st.st_mtime)
            and int(old.get("s", 0)) == st.st_size
        ):
            continue
        tok, calls, model, inp, outp, cread = _codex_scan(p)
        cache[key] = {
            "m": st.st_mtime, "s": st.st_size,
            "t": tok, "c": calls, "d": model,
            "i": inp, "o": outp, "r": cread,
        }
        changed += 1

    global _CODEX_CACHE
    _CODEX_CACHE = cache
    if changed:
        try:
            CODEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
            CODEX_CACHE.write_text(json.dumps({"files": cache}, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # ---- 累计：按模型汇总缓存 ----
    total_by_model: dict[str, dict] = defaultdict(
        lambda: {"tok": 0, "calls": 0, "inp": 0, "outp": 0, "cread": 0}
    )
    for p in files:
        e = cache.get(str(p))
        if not e or not _cache_entry_ok(e):
            continue
        m = str(e.get("d") or "") or "unknown"
        a = total_by_model[m]
        a["tok"] += int(e.get("t") or 0)
        a["calls"] += int(e.get("c") or 0)
        a["inp"] += int(e.get("i") or 0)
        a["outp"] += int(e.get("o") or 0)
        a["cread"] += int(e.get("r") or 0)

    # ---- 今天：只扫最近活跃的文件 ----
    cutoff = now_ts() - DAY
    recent = []
    for p in files:
        try:
            if p.stat().st_mtime >= cutoff:
                recent.append(p)
        except OSError:
            continue
    recent = recent[-40:]
    today = _codex_today_scan(recent, cache)

    for m, a in total_by_model.items():
        t = today.get(m, {"tok": 0, "calls": 0})
        _merge(
            bucket, "codex", m,
            today_tokens=int(t["tok"]),
            today_calls=int(t["calls"]),
            total_tokens=int(a["tok"]),
            total_calls=int(a["calls"]),
            total_input=int(a["inp"]),
            total_output=int(a["outp"]),
            total_cache_read=int(a["cread"]),
        )


def _load_codex_cache() -> dict:
    try:
        if CODEX_CACHE.exists():
            return json.loads(CODEX_CACHE.read_text(encoding="utf-8")).get("files") or {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def codex_events_detail(cutoff_ts: float, tail_bytes: int = 4 * 1024 * 1024):
    """跟 `codex_events` 一样，但产出**带输入/输出/缓存拆分**的明细。

    产出 (时间戳, 模型, 输入, 输出, 缓存读, 总量, 文件路径)。
    算钱要用：输入和输出的单价不一样，只知道总量折不出价。
    """
    root = HOME / ".codex" / "sessions"
    if not root.exists():
        return
    try:
        files = sorted(root.rglob("rollout-*.jsonl"))
    except Exception:  # noqa: BLE001
        return
    cache = _CODEX_CACHE or _load_codex_cache()
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff_ts:
            continue
        model = str((cache.get(str(p)) or {}).get("d") or "") or "unknown"
        try:
            with p.open("rb") as fh:
                if st.st_size > tail_bytes:
                    fh.seek(st.st_size - tail_bytes)
                buf = fh.read()
        except Exception:  # noqa: BLE001
            continue
        for ln in buf.split(b"\n"):
            if b'"token_count"' not in ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            ts = to_epoch(d.get("timestamp"))
            if ts and ts < cutoff_ts:
                continue
            pl = d.get("payload") or {}
            info = pl.get("info") or {}
            last = info.get("last_token_usage") or pl.get("last_token_usage") or {}
            inp = int(last.get("input_tokens") or 0)
            outp = int(last.get("output_tokens") or 0)
            cread = int(last.get("cached_input_tokens") or 0)
            total = int(last.get("total_tokens") or 0) or (inp + outp)
            yield ts, model, inp, outp, cread, total, str(p)


def codex_events(cutoff_ts: float, tail_bytes: int = 4 * 1024 * 1024):
    """扫「cutoff 之后」的 Codex token_count 事件。

    关键：**只读文件尾部**。~/.codex/sessions 有 500+ 个文件、
    近 24h 活跃的就有 98 个（合计 2.5GB），整份读一遍要几十秒。
    会话文件是追加写的，最近的事件一定在尾部，读尾 4MB 就够。

    产出 (时间戳, token 数, 该文件用的模型)。
    """
    for ts, model, _inp, _outp, _cread, total, path in codex_events_detail(cutoff_ts, tail_bytes):
        yield ts, total, model, path


_SCAN_CACHE: dict = {}


def codex_scan(cutoff_ts: float) -> dict:
    """一次扫描，同时按**模型**和按**文件**聚合。

    结果缓存 60 秒——脉搏卡和项目卡都要用，别扫两遍。
    """
    key = round(cutoff_ts / 60)
    hit = _SCAN_CACHE.get(key)
    if hit:
        return hit
    by_model: dict[str, dict] = defaultdict(lambda: {"tok": 0, "calls": 0})
    by_file: dict[str, dict] = defaultdict(lambda: {"tok": 0, "calls": 0})
    for ts, tok, model, path in codex_events(cutoff_ts):
        a = by_model[model]
        a["tok"] += tok
        a["calls"] += 1
        b = by_file[path]
        b["tok"] += tok
        b["calls"] += 1
    out = {"by_model": {k: dict(v) for k, v in by_model.items()},
           "by_file": {k: dict(v) for k, v in by_file.items()}}
    _SCAN_CACHE.clear()
    _SCAN_CACHE[key] = out
    return out


def _codex_today_scan(paths: list[Path], cache: dict) -> dict[str, dict]:
    """近 24h 的增量（按模型分）。走共享的尾部扫描。"""
    cutoff = now_ts() - DAY
    return codex_scan(cutoff)["by_model"]


# --------------------------------------------------------------------------
def collect_model_usage() -> list[ModelUsage]:
    """收集所有来源的模型维度用量。"""
    bucket: dict[str, ModelUsage] = {}
    for fn in (_zcode, _minimax, _hermes, _opencode, _claude, _codex):
        try:
            fn(bucket)
        except Exception as e:  # noqa: BLE001
            # 别静默吞：一个源挂了却什么都不说，出来的数就是错的，
            # 而且没人知道错在哪（踩过：codex 因为缓存格式不一致整个中断，
            # 面板上只剩几千 K，看着像「没怎么用」）。
            from dotusage.util import config as cfgmod

            cfgmod.log_line(f"model_usage: {fn.__name__} 失败：{type(e).__name__}: {e}")
            continue
    _fill_estimated_cost(bucket.values())
    out = [u for u in bucket.values() if u.total_tokens > 0 or u.today_tokens > 0]
    out.sort(key=lambda u: (u.today_tokens, u.total_tokens), reverse=True)
    return out


def _fill_estimated_cost(usages) -> None:
    """给**没有真实计费**的模型补上「按官方 API 单价折算」的累计成本。

    订阅制工具（M Plan / GLM Coding Plan / ChatGPT）本地日志里没有钱，
    但「这些 token 按量买要多少钱」是个有用的参考，所以折算一个数。
    折算出来的标 `estimated=True`，界面上要标清楚不是账单。
    """
    from dotusage.sources.pricing import estimate

    from dotusage.sources.pricing import CACHE_INCLUSIVE_SOURCES

    for u in usages:
        if u.total_cost and u.total_cost > 0:
            continue  # 有真实计费（minimax/opencode/hermes 的表里带 cost）
        c = estimate(
            u.model, u.total_input, u.total_output, u.total_cache_read,
            cache_included=u.source in CACHE_INCLUSIVE_SOURCES,
        )
        if c is None or c <= 0:
            continue
        u.total_cost = c
        u.estimated = True
