"""「额度窗口」卡：两个窗口并排。

按用户给的参考样式来：

    ┌────────────────────────────────┐
    │  5 小时        │  每周  [重置]   │
    │  74%           │  41%           │
    │  · 16:30       │  · 11月5日      │
    │  ▬▬▬▬▬░░░░░    │  ▬▬▬▬░░░░░░░░   │
    └────────────────────────────────┘

墨水屏只有黑白两色，所以：没有圆角、没有蓝绿配色、没有药丸徽章；
**重置时刻就是「重置」这两个字要表达的东西**，直接标出来更省事。

两栏各自独立：
    左 = 最近的 5 小时窗口（谁先重置用谁）
    右 = 最近的周窗口（谁先重置用谁）
两个窗口可能来自不同平台，所以每栏都写清平台名。
"""

from __future__ import annotations

import html
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from dotusage.core.schema import UsageRow, now_ts

SCREEN_W = 296
SCREEN_H = 152
FONT_URL = "file:///Applications/Dot..app/Wrapper/Dot.app/ChillKSans.otf"

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

PLATFORM_SHORT = {
    "chatgpt": "ChatGPT",
    "codex": "Codex",
    "minimax": "MiniMax",
    "zai": "Z.ai",
    "zcode": "Z.ai",
    "grok": "Grok",
    "deepseek": "DeepSeek",
}


def _chrome() -> str | None:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    return shutil.which("chromium") or shutil.which("google-chrome")


def _when(ts: float, now: float) -> str:
    d = datetime.fromtimestamp(ts)
    n = datetime.fromtimestamp(now)
    days = (d.date() - n.date()).days
    hm = d.strftime("%H:%M")
    if days <= 0:
        return f"今天 {hm}"
    if days == 1:
        return f"明天 {hm}"
    if days < 7:
        return f"周{'一二三四五六日'[d.weekday()]} {hm}"
    return f"{d.month}月{d.day}日"


def _clock(ts: float, now: float) -> str:
    """重置时刻的紧凑写法：今天给 HH:MM，近的给「周一18:40」，远的给「11月4日」。"""
    d = datetime.fromtimestamp(ts)
    n = datetime.fromtimestamp(now)
    days = (d.date() - n.date()).days
    if days <= 0:
        return d.strftime("%H:%M")
    if days < 7:
        return f"周{'一二三四五六日'[d.weekday()]}{d.strftime('%H:%M')}"
    return f"{d.month}月{d.day}日"


def pick_windows(rows: list[UsageRow], now: float | None = None) -> dict:
    """按平台整理 5 小时窗口 + 周窗口。

    每个平台一行：
        MiniMax   81% · 15:00   │ 23% · 周一00:00
        ChatGPT      —           │ 90% · 周三18:40
    同一个平台同一窗口可能有多个模型（MiniMax 的 M Plan / video），
    官方给的重置时刻一样，按平台+窗口去重、留用量最大的。
    """
    now = now if now is not None else now_ts()
    best: dict[tuple[str, str], dict] = {}
    for r in rows:
        if r.kind != "quota" or not r.resets_at:
            continue
        platform = PLATFORM_SHORT.get(r.source, r.source)
        is_hour = "小时" in (r.window or "")
        win = "hour" if is_hour else "week"
        key = (platform, win)
        item = {
            "platform": platform,
            "win": win,
            "pct": r.used_percent or 0.0,
            "reset": r.resets_at,
            "when": _when(r.resets_at, now),
            "clock": _clock(r.resets_at, now),
        }
        old = best.get(key)
        if old is None or item["pct"] > old["pct"]:
            best[key] = item

    order: list[str] = []
    for (platform, _win), it in best.items():
        if platform not in order:
            order.append(platform)
    # 排序：按周窗口用量降序，没周窗口的排后面
    def sort_key(p: str):
        w = best.get((p, "week"))
        return -(w["pct"] if w else -1)
    order.sort(key=sort_key)

    table = []
    for platform in order:
        h = best.get((platform, "hour"))
        w = best.get((platform, "week"))
        if not h and not w:
            continue
        table.append({"platform": platform, "hour": h, "week": w})
    return {"table": table, "now": now}


def _cell(item: dict | None) -> str:
    """一个窗口格子：百分比 + 重置时刻（+ 一条细进度条）。"""
    esc = html.escape
    if not item:
        return '<div class="cell"><span class="v">—</span><span class="t"></span></div>'
    pct = max(0.0, min(100.0, item["pct"]))
    return f"""<div class="cell">
  <span class="line"><span class="v">{pct:.0f}%</span><span class="t">· {esc(item["clock"])}</span></span>
  <span class="bar"><span class="bf" style="width:{pct:.1f}%"></span></span>
</div>"""


def build_html(data: dict, now: float | None = None) -> str:
    esc = html.escape
    now = data.get("now") or (now if now is not None else now_ts())
    clock = datetime.fromtimestamp(now).strftime("今天 %H:%M")
    table = data.get("table") or []

    rows = []
    for r in table:
        rows.append(
            f'<div class="row"><span class="pf">{esc(r["platform"])}</span>'
            f'{_cell(r.get("hour"))}{_cell(r.get("week"))}</div>'
        )
    body = "".join(rows) or '<div class="row"><span class="pf">暂无配额窗口</span></div>'

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@font-face{{font-family:"ChillKSans";src:url("{FONT_URL}") format("opentype");}}
html,body{{margin:0;padding:0;width:{SCREEN_W}px;height:{SCREEN_H}px;overflow:hidden;background:#fff;}}
.screen{{width:{SCREEN_W}px;height:{SCREEN_H}px;box-sizing:border-box;padding:7px 10px;
  display:flex;flex-direction:column;font-family:"ChillKSans",sans-serif;color:#000;background:#fff;}}
.head{{display:flex;justify-content:space-between;align-items:baseline;flex:none;}}
.t{{font-size:15px;font-weight:700;letter-spacing:.4px;}}
.c{{font-size:12px;color:#1f1f1f;font-weight:700;}}
.rule{{height:1px;background:#000;margin:4px 0 5px;flex:none;}}
.hdr{{display:flex;flex:none;font-size:11px;color:#3a3a3a;padding-bottom:2px;}}
.hdr .pf{{width:54px;flex:none;}}
.hdr .h5{{width:102px;flex:none;border-left:1px solid #000;padding-left:8px;}}
.hdr .hw{{width:102px;flex:none;border-left:1px solid #000;padding-left:8px;}}
.rows{{flex:1;display:flex;flex-direction:column;justify-content:center;gap:4px;min-height:0;}}
.row{{display:flex;align-items:flex-start;flex:none;}}
.pf{{font-size:12px;font-weight:700;width:54px;flex:none;padding-top:2px;}}
.cell{{width:102px;flex:none;border-left:1px solid #000;padding-left:8px;
  display:flex;flex-direction:column;gap:2px;}}
.line{{display:flex;align-items:baseline;gap:2px;white-space:nowrap;}}
.v{{font-size:15px;font-weight:700;}}
.t{{font-size:11px;color:#3a3a3a;}}
.bar{{display:block;height:4px;border:1px solid #000;}}
.bf{{display:block;height:100%;background:#000;}}
</style></head><body><div class="screen">
<div class="head"><span class="t">额度窗口</span><span class="c">{esc(clock)}</span></div>
<div class="rule"></div>
<div class="hdr"><span class="pf"></span><span class="h5">5 小时</span><span class="hw">每周</span></div>
<div class="rows">{body}</div>
</div></body></html>"""


def render_png(data: dict, now: float | None = None) -> bytes:
    chrome = _chrome()
    if not chrome:
        raise RuntimeError("没找到 Chrome/Chromium")
    text = build_html(data, now)
    tmpdir = Path(tempfile.mkdtemp(prefix="dot-win-"))
    html_file = tmpdir / "win.html"
    png_file = tmpdir / "win.png"
    html_file.write_text(text, encoding="utf-8")
    try:
        subprocess.run(
            [
                chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
                f"--screenshot={png_file}", f"--window-size={SCREEN_W},{SCREEN_H}",
                "--default-background-color=FFFFFFFF", html_file.as_uri(),
            ],
            capture_output=True, timeout=40, check=False,
        )
        png = png_file.read_bytes()
        from dotusage.cards.usage import to_1bit

        return to_1bit(png)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
