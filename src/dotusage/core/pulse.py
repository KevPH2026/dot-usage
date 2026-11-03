"""「用量脉搏」：把 24 小时的 token 消耗画成一条心电图。

为什么做这个：
    现有的卡是**仪表盘** —— 数字准，但没人会因为一块表而"哦"一下。
    同样一批数据换个画法，能看出「我什么时候在烧」这种自己没意识到的习惯：
    上午一个峰、下午一个峰、深夜还有一根 → 那根深夜的柱子就是凌晨还在写代码的证据。

数据：和 models.py 同源（各家本地库），只是按**小时**聚合而不是按模型。

画法：24 根竖条，黑白，e-ink 最擅长这种线稿；
卡片上再配一句自动算出来的洞察（高峰时刻 + 主力平台占比）。
"""

from __future__ import annotations

import glob
import html
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HOME = Path.home()
DAY = 86400.0
SCREEN_W = 296
SCREEN_H = 152
FONT_URL = "file:///Applications/Dot..app/Wrapper/Dot.app/ChillKSans.otf"

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def _chrome() -> str | None:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    return shutil.which("chromium") or shutil.which("google-chrome")


def _hour_of(ts: float) -> int:
    return int(time.localtime(ts).tm_hour)


def _ago_index(ts: float, now: float) -> int:
    """「几小时前」的索引：0 = 当前小时，1 = 1 小时前 …

    不能用「钟点」当 key——那样昨天和今天会撞进同一个桶，
    算「比昨天同期」就废了。
    """
    return max(0, int((now - ts) // 3600))


def collect_hourly(hours: int = 24) -> dict[int, dict]:
    """近 N 小时按小时聚合 token 消耗。

    返回 {小时: {"tokens": int, "by_source": {来源: token}}}
    """
    now = time.time()
    since = now - hours * 3600
    buckets: dict[int, dict] = defaultdict(lambda: {"tokens": 0, "by_source": defaultdict(int)})

    def put(ts: float, src: str, tok: int):
        if ts < since or tok <= 0:
            return
        idx = _ago_index(ts, now)
        buckets[idx]["tokens"] += tok
        buckets[idx]["by_source"][src] += tok

    # ---- ZCode（GLM / MiniMax 走 z.ai）----
    db = HOME / ".zcode" / "cli" / "db" / "db.sqlite"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for ts, tok in con.execute(
                "select started_at, input_tokens+output_tokens from model_usage where started_at > ?",
                (int(since * 1000),),
            ):
                put(ts / 1000, "zcode", tok)
            con.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- MiniMax Code ----
    db = HOME / ".minimax" / "sqlite.db"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for ts, tok in con.execute(
                "select ts, input_tokens+output_tokens+cache_read_tokens+cache_write_tokens "
                "from token_usage where ts > ?",
                (int(since * 1000),),
            ):
                put(ts / 1000, "minimax", tok)
            con.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- OpenCode ----
    db = HOME / ".local" / "share" / "opencode" / "opencode.db"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for ts, data in con.execute(
                "select time_created, data from message where time_created > ?", (int(since * 1000),)
            ):
                try:
                    d = json.loads(data)
                except Exception:  # noqa: BLE001
                    continue
                if d.get("role") != "assistant":
                    continue
                t = d.get("tokens") or {}
                ca = t.get("cache") or {}
                put(
                    ts / 1000,
                    "opencode",
                    (t.get("input") or 0) + (t.get("output") or 0)
                    + (ca.get("read") or 0) + (ca.get("write") or 0),
                )
            con.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- Hermes（按会话结束时间，粗但够用）----
    db = HOME / ".hermes" / "state.db"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            for ts, tok in con.execute(
                "select s.ended_at, sum(u.input_tokens+u.output_tokens+u.cache_read_tokens+u.cache_write_tokens) "
                "from session_model_usage u left join sessions s on s.id=u.session_id "
                "where s.ended_at > ? group by s.ended_at",
                (since,),
            ):
                put(ts or 0, "hermes", int(tok or 0))
            con.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- Claude Code ----
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
                    import datetime

                    try:
                        ep = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
                    except Exception:  # noqa: BLE001
                        continue
                    put(
                        ep,
                        "claude",
                        (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
                        + (u.get("cache_creation_input_tokens") or 0)
                        + (u.get("cache_read_input_tokens") or 0),
                    )
        except Exception:  # noqa: BLE001
            continue

    # ---- Codex（和 models.py 走同一个尾部扫描，两边口径一致）----
    try:
        from dotusage.core.models import codex_events

        now = time.time()
        for ts, tok, model, _path in codex_events(since):
            put(ts or now, "codex", tok)
    except Exception:  # noqa: BLE001
        pass

    return dict(buckets)


SRC_LABEL = {
    "codex": "Codex",
    "zcode": "Z.ai",
    "minimax": "MiniMax",
    "opencode": "OpenCode",
    "claude": "Claude",
    "hermes": "Hermes",
}


def _compact(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.0f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return f"{int(n)}"


def insight(hourly: dict[int, dict]) -> tuple[str, str, float]:
    """算洞察。返回 (今日总量短串, 洞察句, 比昨天同期百分比)。"""
    now_hour = _hour_of(time.time())
    today = {i: v for i, v in hourly.items() if i < 24}
    if not today:
        return "—", "还没有用量记录", 0.0
    total = sum(v["tokens"] for v in today.values())
    by_src: dict[str, int] = defaultdict(int)
    for v in today.values():
        for s, t in v["by_source"].items():
            by_src[s] += t
    top = sorted(today.items(), key=lambda kv: -kv[1]["tokens"])[:2]
    peaks = "、".join(f"{(now_hour - i) % 24:02d}:00" for i, _ in top if _["tokens"] > 0)
    if by_src:
        src, tok = max(by_src.items(), key=lambda kv: kv[1])
        share = tok / total * 100 if total else 0
        name = SRC_LABEL.get(src, src)
        note = f"峰值 {peaks} · {name} 占 {share:.0f}%"
    else:
        note = f"峰值 {peaks}"
    prev = sum(v["tokens"] for i, v in hourly.items() if 24 <= i < 48)
    delta = ((total - prev) / prev * 100) if prev else 0.0
    return _compact(total), note, delta


def build_html(hourly: dict[int, dict], title: str = "用量脉搏") -> str:
    """24 根竖条 + 一条洞察（同比放在标题栏）。"""
    esc = html.escape
    now_h = _hour_of(time.time())
    idxs = list(range(23, -1, -1))  # 23 小时前 → 现在
    vals = [hourly.get(i, {}).get("tokens", 0) for i in idxs]
    peak = max(vals) or 1
    total, note, delta = insight(hourly)

    bars = []
    for v in vals:
        h = max(1, int(v / peak * 56)) if v else 1
        fill = "#000" if v else "#e2e2e2"
        bars.append(f'<span class="b" style="height:{h}px;background:{fill}"></span>')
    bar_html = "".join(bars)

    delta_txt = ""
    if delta:
        arrow = "↑" if delta >= 0 else "↓"
        delta_txt = f" {arrow}{abs(delta):.0f}%"
    elif sum(vals) > 0:
        delta_txt = " —"

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@font-face{{font-family:"ChillKSans";src:url("{FONT_URL}") format("opentype");}}
html,body{{margin:0;padding:0;width:{SCREEN_W}px;height:{SCREEN_H}px;overflow:hidden;background:#fff;}}
.screen{{width:{SCREEN_W}px;height:{SCREEN_H}px;box-sizing:border-box;padding:7px 8px;
  display:flex;flex-direction:column;font-family:"ChillKSans",sans-serif;color:#000;background:#fff;}}
.head{{display:flex;justify-content:space-between;align-items:baseline;flex:none;}}
.t{{font-size:15px;font-weight:700;letter-spacing:.4px;}}
.c{{font-size:13px;color:#1f1f1f;font-weight:700;}}
.rule{{height:1px;background:#d0d0d0;margin:3px 0 4px;flex:none;}}
.chart{{flex:none;height:58px;display:flex;align-items:flex-end;gap:3px;}}
.b{{width:7px;display:block;flex:none;}}
.axis{{flex:none;display:flex;justify-content:space-between;font-size:10px;color:#555;margin-top:2px;}}
.rule2{{height:1px;background:#d0d0d0;margin:5px 0 4px;flex:none;}}
.note{{flex:none;font-size:12px;color:#3a3a3a;white-space:nowrap;}}
</style></head><body><div class="screen">
<div class="head"><span class="t">{esc(title)}</span><span class="c">今日 {esc(total)}{esc(delta_txt)}</span></div>
<div class="rule"></div>
<div class="chart">{bar_html}</div>
<div class="axis"><span>{(now_h - 23) % 24:02d}:00</span><span>{(now_h - 12) % 24:02d}:00</span><span>{now_h:02d}:00</span></div>
<div class="rule2"></div>
<div class="note">{esc(note)}</div>
</div></body></html>"""


def from_snapshot(snap: dict) -> dict[int, dict]:
    """从 snapshot.json 的 pulse 段还原成 hourly 结构。"""
    p = snap.get("pulse") or {}
    out: dict[int, dict] = {}
    for h in p.get("hours") or []:
        out[int(h["i"])] = {"tokens": int(h.get("t") or 0), "by_source": dict(h.get("src") or {})}
    return out


def render_png(hourly: dict[int, dict], title: str = "用量脉搏", one_bit: bool = True) -> bytes:
    chrome = _chrome()
    if not chrome:
        raise RuntimeError("没找到 Chrome/Chromium")
    text = build_html(hourly, title)
    tmpdir = Path(tempfile.mkdtemp(prefix="dot-pulse-"))
    html_file = tmpdir / "pulse.html"
    png_file = tmpdir / "pulse.png"
    html_file.write_text(text, encoding="utf-8")
    try:
        subprocess.run(
            [
                chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
                f"--screenshot={png_file}", f"--window-size={SCREEN_W},{SCREEN_H}",
                "--default-background-color=FFFFFFFF", html_file.as_uri(),
            ],
            capture_output=True,
            timeout=40,
            check=False,
        )
        png = png_file.read_bytes()
        if one_bit:
            from dotusage.cards.usage import to_1bit

            png = to_1bit(png)
        return png
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def snapshot_payload(hourly: dict[int, dict]) -> dict:
    """给 snapshot.json 用的紧凑结构（供面板/飞书复用）。"""
    total, note, delta = insight(hourly)
    return {
        "total": total,
        "note": note,
        "delta": round(delta, 1),
        "hours": [
            {
                "i": i,
                "t": hourly.get(i, {}).get("tokens", 0),
                "src": dict(hourly.get(i, {}).get("by_source", {})),
            }
            for i in range(48)   # 存 48 小时：后 24 小时用来算「比昨天同期」
        ],
    }
