"""把卡片渲染成 296×152 的 PNG，走 DOT 的 Image API。

画板（Canvas）版是设备原生渲染，文字最锐利，但元素间距受框架限制，
一屏只能舒舒服���放 2 个平台。

图片版是自己画像素，可以把**所有平台压进一屏**——适合当总览屏。
两者可以同时挂在 loop 列表里轮播。

渲染链路：
    拼 HTML（用 DOT 自己的字体 ChillKSans.otf，尺寸就是设备像素）
      → Chrome headless 精确截 296×152（@2x = 592×304）
      → 灰阶化（墨水屏只有黑白灰）
      → base64 → POST /api/authV2/open/device/{id}/image
"""

from __future__ import annotations

import base64
import html
import shutil
import subprocess
import tempfile
from pathlib import Path

from dotusage.core.schema import UsageRow, compact, iso, now_ts

SCREEN_W = 296
SCREEN_H = 152
FONT_URL = "file:///Applications/Dot..app/Wrapper/Dot.app/ChillKSans.otf"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

CHROME_CANDIDATES = [
    CHROME,
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def _chrome() -> str | None:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    return shutil.which("chromium") or shutil.which("google-chrome")


def _icon_data_uri(name: str) -> str:
    from dotusage.util.icons import data_uri

    return data_uri(name)


# 图片版名字宽度有限，长名字用短名
SHORT_LABELS = {
    "DeepSeek 余额": "DeepSeek",
    "Z.ai 工具额度": "工具额度",
}


def overview_rows(rows: list[UsageRow], limit: int = 5) -> list[UsageRow]:
    """总览图放哪些行。

    - **不含 Tibo**（Tibo 单独一屏）
    - 平台行优先（aggregate 已经按配额紧张度排好序），再补余额行
    - 296×152 一屏最多塞 5 行：标题 28px + 5×19px + 行距，正好不裁
    """
    plats = [r for r in rows if r.kind == "platform"]
    rest = [r for r in rows if r.kind not in ("platform", "tibo")]
    return (plats + rest)[:limit]


def _row_html(r: UsageRow) -> str:
    esc = html.escape
    label = SHORT_LABELS.get(r.label, r.label)
    icon = ""
    if r.icon:
        uri = _icon_data_uri(r.icon)
        if uri:
            icon = f'<img class="ic" src="{uri}"/>'
    if r.used_percent is not None:
        pct = max(0.0, min(100.0, r.used_percent))
        bar = (
            f'<span class="track"><span class="fill" style="width:{pct:.1f}%"></span></span>'
        )
        head = f"{pct:.0f}%"
    else:
        bar = '<span class="spacer"></span>'
        head = esc(r.headline)
    # 短数字用大字号；档位名（Premium+）太长就缩一档，免得被裁掉
    hd_cls = "hd" if len(head) <= 5 else "hd sm"
    detail = esc(r.detail) if r.detail else ""
    return f"""<div class="row">{icon}<span class="nm">{esc(label)}</span>{bar}<span class="{hd_cls}">{head}</span><span class="dt">{detail}</span></div>"""


def build_html(rows: list[UsageRow], title: str = "AI 用量") -> str:
    esc = html.escape
    stamp = iso(now_ts())
    body = "\n".join(_row_html(r) for r in rows) or '<div class="row"><span class="nm">暂无数据</span></div>'
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@font-face{{font-family:"ChillKSans";src:url("{FONT_URL}") format("opentype");}}
html,body{{margin:0;padding:0;width:{SCREEN_W}px;height:{SCREEN_H}px;overflow:hidden;background:#fff;}}
.screen{{width:{SCREEN_W}px;height:{SCREEN_H}px;box-sizing:border-box;padding:7px 8px;
  display:flex;flex-direction:column;font-family:"ChillKSans",sans-serif;color:#000;background:#fff;}}
.head{{display:flex;justify-content:space-between;align-items:baseline;flex:none;}}
.t{{font-size:15px;font-weight:700;letter-spacing:.4px;}}
.c{{font-size:11px;color:#3a3a3a;}}
.rule{{height:1px;background:#ccc;margin:3px 0 4px;flex:none;}}
.rows{{flex:1;display:flex;flex-direction:column;justify-content:center;gap:2px;min-height:0;padding-bottom:2px;}}
.row{{display:flex;align-items:center;gap:5px;height:19px;}}
.ic{{width:16px;height:16px;flex:none;}}
.nm{{font-size:13px;font-weight:700;width:62px;flex:none;overflow:hidden;white-space:nowrap;}}
.track{{width:58px;height:11px;background:#fff;border:1px solid #000;overflow:hidden;flex:none;display:inline-block;}}
.fill{{display:block;height:100%;background:#000;}}
.hd{{font-size:17px;font-weight:700;width:42px;text-align:right;flex:none;}}
.hd.sm{{font-size:12px;width:56px;letter-spacing:-0.2px;}}
.dt{{font-size:11px;color:#3a3a3a;flex:1;text-align:right;padding-left:8px;white-space:nowrap;overflow:hidden;}}
.spacer{{width:58px;flex:none;}}
.foot{{display:flex;justify-content:space-between;font-size:8px;color:#aaa;flex:none;margin-top:2px;}}
</style></head><body><div class="screen">
<div class="head"><span class="t">{esc(title)}</span><span class="c">{esc(stamp[:16])}</span></div>
<div class="rule"></div>
<div class="rows">{body}</div>
</div></body></html>"""


def to_1bit(png: bytes) -> bytes:
    """把图转成**纯黑白**（1bit）。

    为什么：墨水屏只有黑白两色，设备会按 ditherType 把图抖动成点阵。
    如果送的是带抗锯齿的灰阶图，小字会被抖成一团噪点，看不清。
    直接送 1bit，设备那边就没得抖，**笔画是硬边**。
    """
    from PIL import Image
    import io

    im = Image.open(io.BytesIO(png)).convert("1")
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_png(rows: list[UsageRow], title: str = "AI 用量") -> bytes:
    """渲染成 PNG 字节。失败抛 RuntimeError。"""
    chrome = _chrome()
    if not chrome:
        raise RuntimeError("没找到 Chrome/Chromium，无法渲染图片版卡片")
    html_text = build_html(rows, title)
    tmpdir = Path(tempfile.mkdtemp(prefix="dot-usage-img-"))
    html_file = tmpdir / "card.html"
    png_file = tmpdir / "card.png"
    html_file.write_text(html_text, encoding="utf-8")
    try:
        subprocess.run(
            [
                chrome,
                "--headless",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-device-scale-factor=2",
                f"--screenshot={png_file}",
                f"--window-size={SCREEN_W},{SCREEN_H}",
                html_file.as_uri(),
            ],
            capture_output=True,
            timeout=40,
            check=False,
        )
        if not png_file.exists():
            raise RuntimeError("截图失败")
        return png_file.read_bytes()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def to_gray_png(png: bytes) -> bytes:
    """转成灰阶（墨水屏是黑白灰）。没有 Pillow 就原样返回。"""
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(png)).convert("L")
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return png


def render_image_b64(rows: list[UsageRow], title: str = "AI 用量", one_bit: bool = True,
                     exclude_kinds: tuple[str, ...] = ("tibo",)) -> str:
    """图片版只放**消耗**。Tibo 重置是独立的卡片，不混进来。

    默认转成**纯黑白 1bit**：墨水屏只有黑白两色，
    送灰阶图过去设备要抖动，11~13px 的小字会被抖成噪点。
    """
    rows = [r for r in rows if r.kind not in exclude_kinds]
    png = render_png(rows, title)
    if one_bit:
        png = to_1bit(png)
    return base64.b64encode(png).decode("ascii")
