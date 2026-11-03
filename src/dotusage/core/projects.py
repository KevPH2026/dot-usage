"""「哪个项目在吃你的额度」。

现有卡片回答的是「我用了多少」，这张回答的是**「是谁在用」**——
按工作目录（Codex 的 session_meta.cwd）把消耗拆账。

为什么这个 aha：
    你以为你在「用 AI」，其实是某个项目在悄悄吃你的额度。
    实测 Codex 累计 8.25B token 里，DTC 2 占 42.6%，
    而 34% 掉在一次性的临时聊天目录里——那部分等于白烧。

数据：
    累计 → codex_totals.json 缓存里每个文件的总量（按文件 mtime 增量维护）
    今日 → 一次 codex_scan() 扫描，按文件聚合

项目名取路径最后两段：
    ~/work/DTC 2                      → DTC 2
    ~/work/Codex/2026-10-26/new-chat → Codex/new-chat
"""

from __future__ import annotations

import html
import json
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

from dotusage.core.schema import now_ts

HOME = Path.home()
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


def _project_of(cwd: str) -> str:
    """工作目录 → 项目名。

    ~/work/DTC 2                       → DTC 2
    ~/work/Codex/2026-10-26/new-chat   → Codex/new-chat
    ~/work/Model Benchmark              → Model Benchmark
    """
    parts = [x for x in cwd.split("/") if x and not x.startswith(".")]
    if len(parts) >= 2 and parts[0] == "Users":
        parts = parts[2:]                       # 去掉 /Users/<名字>
    parts = [x for x in parts if x != "Documents"]   # 去掉 Documents
    parts = [x for x in parts if not _is_date(x)]     # 去掉日期段
    if not parts:
        return "其他"
    if len(parts) == 1:
        return parts[0]
    return "/".join(parts[-2:])


def _is_date(s: str) -> bool:
    return len(s) == 10 and s[4] == "-" and s[7] == "-" and s[:4].isdigit()


def _cwd_map() -> dict[str, str]:
    """文件 → 工作目录。

    cwd 在 rollout 文件的**第一行** session_meta 里，
    而那一行很长（base_instructions），所以要整行读，不能只读头部几 KB。
    结果写回 codex_totals.json 缓存（按 mtime 增量）。
    """
    from dotusage.core.models import CODEX_CACHE, _load_codex_cache

    cache = _load_codex_cache()
    out: dict[str, str] = {}
    dirty = False
    for path, entry in cache.items():
        cwd = entry.get("w")
        if cwd:
            out[path] = cwd
            continue
        try:
            with open(path, "rb") as fh:
                line = fh.readline()
            d = json.loads(line)
            if d.get("type") == "session_meta":
                cwd = (d.get("payload") or {}).get("cwd")
        except Exception:  # noqa: BLE001
            cwd = None
        if cwd:
            entry["w"] = cwd
            out[path] = cwd
            dirty = True
    if dirty:
        try:
            CODEX_CACHE.write_text(json.dumps({"files": cache}, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return out


def collect_projects(top: int = 4) -> dict:
    """按项目聚合消耗。

    返回 {"rows": [{name, total, today, sessions}], "total": int, "note": str}
    """
    from dotusage.core.models import codex_scan

    cwd_map = _cwd_map()
    cache: dict = {}
    try:
        from dotusage.core.models import CODEX_CACHE, _load_codex_cache

        cache = _load_codex_cache()
    except Exception:  # noqa: BLE001
        pass

    today_by_file = codex_scan(now_ts() - 86400)["by_file"]

    agg: dict[str, dict] = defaultdict(lambda: {"total": 0, "today": 0, "sessions": 0})
    for path, entry in cache.items():
        cwd = cwd_map.get(path)
        if not cwd:
            continue
        name = _project_of(cwd)
        a = agg[name]
        a["total"] += int(entry.get("t") or 0)
        a["sessions"] += 1
    for path, v in today_by_file.items():
        cwd = cwd_map.get(path)
        if not cwd:
            continue
        agg[_project_of(cwd)]["today"] += int(v.get("tok") or 0)

    rows = [{"name": k, **v} for k, v in agg.items() if v["total"] > 0]
    rows.sort(key=lambda r: -r["total"])
    total = sum(r["total"] for r in rows)

    note = ""
    if rows:
        top_name, top_v = rows[0]["name"], rows[0]["total"]
        note = f"{top_name} 占 {top_v / total * 100:.0f}%"
        if len(rows) > top:
            note += f" · 另有 {len(rows) - top} 个项目"
    return {"rows": rows[:top], "total": total, "all_count": len(rows), "note": note}


def _compact(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.0f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return f"{int(n)}"


def build_html(data: dict, title: str = "项目烧钱榜") -> str:
    esc = html.escape
    rows = data.get("rows") or []
    total = data.get("total") or 0
    peak = max((r["total"] for r in rows), default=1) or 1

    bars = []
    for r in rows:
        pct = r["total"] / peak * 100
        bars.append(
            f'<div class="row"><span class="nm" title="{esc(r["name"])}">{esc(r["name"])}</span>'
            f'<span class="track"><span class="fill" style="width:{pct:.1f}%"></span></span>'
            f'<span class="val">{esc(_compact(r["total"]))}</span></div>'
        )
    body = "".join(bars) or '<div class="row"><span class="nm">还没有记录</span></div>'

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
.row{{display:flex;align-items:center;gap:5px;height:19px;}}
.nm{{font-size:12px;font-weight:600;width:84px;flex:none;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;}}
.track{{width:100px;height:11px;background:#fff;border:1px solid #000;flex:none;display:inline-block;}}
.fill{{display:block;height:100%;background:#000;}}
.val{{font-size:13px;font-weight:700;width:42px;text-align:right;flex:none;}}
.note{{flex:none;font-size:12px;color:#3a3a3a;margin-top:3px;white-space:nowrap;}}
</style></head><body><div class="screen">
<div class="head"><span class="t">{esc(title)}</span><span class="c">累计 {esc(_compact(total))}</span></div>
<div class="rule"></div>
<div class="rows">{body}</div>
<div class="note">{esc(data.get("note") or "")}</div>
</div></body></html>"""


def render_png(data: dict, title: str = "项目烧钱榜") -> bytes:
    chrome = _chrome()
    if not chrome:
        raise RuntimeError("没找到 Chrome/Chromium")
    text = build_html(data, title)
    tmpdir = Path(tempfile.mkdtemp(prefix="dot-proj-"))
    html_file = tmpdir / "proj.html"
    png_file = tmpdir / "proj.png"
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
