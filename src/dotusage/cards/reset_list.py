"""「官方重置时间」卡。

消耗卡上现在写的是**推算**的「5 小时后烧完」（反推窗口消耗速率算出来的），
但那是我的估计。官方给的**重置时刻**是另一回事，得单独一张卡摆出来：

    ChatGPT  周  90%   11-03 09:20
    MiniMax  5h 72%   今天 15:00
    Z.ai     周  55%   11-04 01:03

排序按**离现在最近的排前面** —— 看着就知道下一个什么时候恢复。
底部再给一句「下一个：MiniMax 5小时 · 今天 15:00（3 时 23 分后）」。

数据：采集层已经拿到每个窗口的 resets_at（官方下发的时间戳），
这里只做展示，不再做任何推算。
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
    """重置时刻 → 人话：今天 15:00 / 明天 09:12 / 周三 09:20 / 11-04 01:03"""
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
    return f"{d.strftime('%m-%d')} {hm}"


def _countdown(ts: float, now: float) -> str:
    h = (ts - now) / 3600
    if h < 1:
        return f"{max(int((ts - now) / 60), 1)} 分后"
    if h < 48:
        return f"{h:.0f} 时 {int(h % 1 * 60)} 分后"
    return f"{h / 24:.1f} 天后"


def reset_rows(rows: list[UsageRow], now: float | None = None) -> list[dict]:
    """从原始配额行里抽出「平台 + 窗口 + 官方重置时刻」。

    同一平台同一窗口可能有多个模型（MiniMax 的 M Plan / video），
    官方给的重置时刻是一样的，按平台+窗口去重、保留用量最大的那个。
    """
    now = now if now is not None else now_ts()
    best: dict[tuple[str, str], dict] = {}
    for r in rows:
        if r.kind != "quota" or not r.resets_at:
            continue
        platform = PLATFORM_SHORT.get(r.source, r.source)
        win = r.window.replace("窗口", "")
        if "小时" in win:
            win = win.replace("小时", "h")
        elif "天" in win:
            win = "周"
        key = (platform, win)
        item = {
            "platform": platform,
            "window": win,
            "pct": r.used_percent,
            "reset": r.resets_at,
            "when": _when(r.resets_at, now),
        }
        old = best.get(key)
        if old is None or (item["pct"] or 0) > (old["pct"] or 0):
            best[key] = item
    out = sorted(best.values(), key=lambda x: x["reset"])
    for item in out:
        item["countdown"] = _countdown(item["reset"], now)
    return out


def build_html(items: list[dict], now: float | None = None) -> str:
    esc = html.escape
    now = now if now is not None else now_ts()
    clock = datetime.fromtimestamp(now).strftime("今天 %H:%M")

    rows = []
    for it in items:
        pct = f"{it['pct']:.0f}%" if it["pct"] is not None else "—"
        rows.append(
            f'<div class="row"><span class="nm">{esc(it["platform"])}</span>'
            f'<span class="wn">{esc(it["window"])}</span>'
            f'<span class="pc">{esc(pct)}</span>'
            f'<span class="tm">{esc(it["when"])}</span></div>'
        )
    body = "".join(rows) or '<div class="row"><span class="nm">没有配额窗口</span></div>'

    note = ""
    if items:
        nxt = items[0]
        note = f"下一个：{nxt['platform']} {nxt['window']} · {nxt['when']}（{nxt['countdown']}）"

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@font-face{{font-family:"ChillKSans";src:url("{FONT_URL}") format("opentype");}}
html,body{{margin:0;padding:0;width:{SCREEN_W}px;height:{SCREEN_H}px;overflow:hidden;background:#fff;}}
.screen{{width:{SCREEN_W}px;height:{SCREEN_H}px;box-sizing:border-box;padding:7px 8px;
  display:flex;flex-direction:column;font-family:"ChillKSans",sans-serif;color:#000;background:#fff;}}
.head{{display:flex;justify-content:space-between;align-items:baseline;flex:none;}}
.t{{font-size:15px;font-weight:700;letter-spacing:.4px;}}
.c{{font-size:13px;color:#1f1f1f;font-weight:700;}}
.rule{{height:1px;background:#d0d0d0;margin:3px 0 4px;flex:none;}}
.rows{{flex:1;display:flex;flex-direction:column;justify-content:center;gap:3px;min-height:0;}}
.row{{display:flex;align-items:baseline;gap:5px;height:19px;}}
.nm{{font-size:13px;font-weight:700;width:58px;flex:none;overflow:hidden;white-space:nowrap;}}
.wn{{font-size:12px;color:#3a3a3a;width:26px;flex:none;}}
.pc{{font-size:13px;font-weight:700;width:34px;text-align:right;flex:none;}}
.tm{{font-size:12px;flex:1;text-align:right;white-space:nowrap;}}
.note{{flex:none;font-size:12px;color:#3a3a3a;margin-top:3px;white-space:nowrap;}}
</style></head><body><div class="screen">
<div class="head"><span class="t">官方重置时间</span><span class="c">{esc(clock)}</span></div>
<div class="rule"></div>
<div class="rows">{body}</div>
<div class="note">{esc(note)}</div>
</div></body></html>"""


def render_png(items: list[dict], now: float | None = None) -> bytes:
    chrome = _chrome()
    if not chrome:
        raise RuntimeError("没找到 Chrome/Chromium")
    text = build_html(items, now)
    tmpdir = Path(tempfile.mkdtemp(prefix="dot-reset-"))
    html_file = tmpdir / "reset.html"
    png_file = tmpdir / "reset.png"
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
