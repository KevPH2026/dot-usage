"""把卡片渲染成 HTML 预览。

用途：在没拿到 DOT API Key、或者推送之前，先肉眼确认每一屏的排版对不对。
预览按 Quote/0 的 296 × 152 基准画，3 倍放大方便看细节，
颜色刻意只用黑白灰，模拟墨水屏。多页并排显示，一眼看清设备会怎么轮播。
"""

from __future__ import annotations

import html
from pathlib import Path

from dotusage.util.icons import data_uri
from dotusage.cards.canvas import PAD, SCREEN_H, SCREEN_W, build_pages
from dotusage.core.schema import UsageRow, countdown, iso, now_ts


def _bar_html(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    return '<div class="bar"><div class="fill" style="width:%.1f%%"></div></div>' % pct


def _row_html(r: UsageRow) -> str:
    esc = html.escape
    if r.kind == "balance":
        uri = data_uri(r.icon)
        icon_html = (f'<img class="picon" src="{uri}"/>' if uri else "")
        return f"""<div class="row brow">
  {icon_html}
  <span class="bname">{esc(r.label)}</span>
  <span class="pspace"></span>
  <span class="bval">{esc(r.headline)}</span>
</div>"""
    if r.kind == "tibo":
        uri = data_uri(r.icon)
        icon_html = (f'<img class="ticon" src="{uri}"/>' if uri else '<span class="ticon"></span>')
        return f"""<div class="row trow">
  <div class="ttop">{icon_html}<span class="tname">{esc(r.label)}</span><span class="tspace"></span><span class="tval">{esc(r.headline)}</span></div>
  <div class="tdetail">{esc(r.detail)}</div>
  {f'<div class="tdetail">{esc(r.detail2_override)}</div>' if r.detail2_override else ''}
</div>"""
    if r.kind == "platform":
        uri = data_uri(r.icon)
        icon_html = (f'<img class="picon" src="{uri}"/>' if uri else '<span class="picon"></span>')
        bar = ""
        if r.used_percent is not None:
            pct = max(0.0, min(100.0, r.used_percent))
            bar = f'<div class="pbar"><div class="pfill" style="width:{pct:.1f}%"></div></div>'
        note = f'<span class="pnote">{esc(r.detail)}</span>' if r.detail else ""
        # 档位名（Premium+）太长就缩一档字号并放宽盒子，否则会顶出屏外
        pval_cls = "pval" if len(r.headline) <= 5 else "pval sm"
        return f"""<div class="row prow">
  <div class="ptop">
    {icon_html}
    <span class="pname">{esc(r.label)}</span>
    <span class="pspace"></span>
    <span class="{pval_cls}">{esc(r.headline)}</span>
  </div>
  <div class="pbot">{bar}{note}</div>
</div>"""
    if r.kind == "quota":
        reset = esc(countdown(r.resets_at) or "重置时间未知")
        return f"""<div class="row">
  <div class="line"><span class="name">{esc(r.label)}</span><span class="val">{r.used_percent:.0f}%</span></div>
  <div class="line2">{_bar_html(r.bar_width)}<span class="muted">{reset}</span></div>
</div>"""
    return f"""<div class="row">
  <div class="line"><span class="name">{esc(r.label)}</span><span class="val">{esc(r.headline)}</span></div>
  <div class="line2"><span class="muted small">{esc(r.detail)}</span></div>
</div>"""


def _screen_html(page: dict, scale: int) -> str:
    esc = html.escape
    data = page.get("data", {})
    title = esc(str(data.get("title") or "AI 用量"))
    stamp = esc(str(data.get("updated") or iso(now_ts())))
    clock = stamp[11:16]

    right = []
    total_pages = page.get("_page_total", 1)
    idx = page.get("_page_index", 0)
    if total_pages > 1:
        right.append(f"{idx + 1}/{total_pages}")
    right.append(clock)
    right_html = esc(" · ".join(right))

    rows = page.get("data", {}).get("rows", [])
    body = "\n".join(_row_html(_row_from_dict(r)) for r in rows) or '<div class="row"><span class="muted">暂无数据</span></div>'

    return f"""<div class="screenwrap">
  <div class="pagetag">第 {idx + 1} 屏 · {title}</div>
  <div class="frame"><div class="screen">
    <div class="head"><span class="title">{title}</span><span class="clock">{right_html}</span></div>
    <div class="rows">{body}</div>
  </div></div>
</div>"""


def _row_from_dict(d: dict) -> UsageRow:
    fields = {k: v for k, v in d.items() if k in UsageRow.__dataclass_fields__}
    return UsageRow(**fields)


def render_html(rows: list[UsageRow], all_rows: list[UsageRow] | None = None, scale: int = 3,
                max_rows: int = 4) -> str:
    pages = build_pages(rows, max_rows=max_rows)
    total = len(all_rows if all_rows is not None else rows)

    screens = []
    for i, page in enumerate(pages):
        page["_page_index"] = i
        page["_page_total"] = len(pages)
        screens.append(_screen_html(page, scale))

    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>AI 用量卡片预览 · Quote/0 296×152</title>
<style>
  @font-face {{
    font-family: "ChillKSans";
    src: url("file:///Applications/Dot..app/Wrapper/Dot.app/ChillKSans.otf") format("opentype");
    font-display: swap;
  }}
  :root {{ color-scheme: light; }}
  body, .screen {{ font-family: "ChillKSans", -apple-system, "PingFang SC", sans-serif; }}
  body {{ margin:0; padding:28px; background:#EDEDED; font-family:-apple-system,"PingFang SC","Helvetica Neue",sans-serif; }}
  .wrap {{ display:flex; gap:28px; align-items:flex-start; flex-wrap:wrap; }}
  .screenwrap {{ display:flex; flex-direction:column; gap:8px; }}
  .pagetag {{ font-size:13px; color:#555; font-weight:600; }}
  .frame {{ background:#3A3A3A; padding:14px; border-radius:16px; box-shadow:0 8px 26px rgba(0,0,0,.18); }}
  .screen {{ width:{SCREEN_W * scale}px; height:{SCREEN_H * scale}px; background:#FFFFFF; color:#000;
             padding:{PAD * scale}px; box-sizing:border-box; display:flex; flex-direction:column; gap:{6 * scale}px; }}
  .head {{ display:flex; justify-content:space-between; align-items:baseline; border-bottom:1px solid #D9D9D9; padding-bottom:{4 * scale}px; }}
  .title {{ font-size:{16 * scale / 3:.0f}px; font-weight:700; letter-spacing:.02em; }}
  .clock {{ font-size:{12 * scale / 3:.0f}px; color:#666; }}
  .rows {{ flex:1; display:flex; flex-direction:column; gap:{5 * scale}px; min-height:0; }}
  .row {{ display:flex; flex-direction:column; gap:{3 * scale}px; }}
  .row + .row {{ border-top:1px solid #EFEFEF; padding-top:{4 * scale}px; }}
  .line {{ display:flex; justify-content:space-between; align-items:baseline; gap:{6 * scale}px; }}
  .line2 {{ display:flex; align-items:center; gap:{6 * scale}px; }}
  .name {{ font-size:{16 * scale / 3:.0f}px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .val {{ font-size:{16 * scale / 3:.0f}px; font-weight:700; }}
  .muted {{ font-size:{12 * scale / 3:.0f}px; color:#666; white-space:nowrap; }}
  .small {{ font-size:{12 * scale / 3:.0f}px; }}
  .bar {{ width:{150 * scale / 3:.0f}px; height:{4 * scale / 3:.0f}px; background:#E9E9E9; border-radius:2px; overflow:hidden; }}
  .fill {{ height:100%; background:#000; }}
  .foot {{ display:flex; justify-content:space-between; font-size:{12 * scale / 3:.0f}px; color:#999; border-top:1px solid #E5E5E5; padding-top:{4 * scale}px; }}
  .prow {{ display:flex; flex-direction:column; gap:{2 * scale}px; }}
  .ptop {{ display:flex; flex-direction:row; align-items:center; gap:{6 * scale}px; }}
  .pbot {{ display:flex; flex-direction:row; align-items:center; gap:{8 * scale}px; padding-left:{28 * scale}px; }}
  .picon {{ width:{22 * scale}px; height:{22 * scale}px; display:block; flex:none; }}
  .pname {{ font-size:{16 * scale}px; font-weight:700; flex:none; white-space:nowrap; width:{76 * scale}px; overflow:hidden; text-overflow:ellipsis; }}
  .pspace {{ flex:1 1 auto; min-width:{4 * scale}px; }}
  .pbar {{ width:{120 * scale}px; height:{10 * scale}px; background:#E9E9E9; border-radius:{7 * scale}px; overflow:hidden; flex:none; }}
  .pfill {{ height:100%; background:#000; }}
  .pnote {{ font-size:{12 * scale}px; color:#666; flex:none; white-space:nowrap; }}
  .pval {{ font-size:{24 * scale}px; font-weight:700; flex:none; width:{60 * scale}px; text-align:right; }}
  .pval.sm {{ font-size:{14 * scale}px; width:{80 * scale}px; }}
  .trow {{ display:flex; flex-direction:column; gap:{4 * scale}px; }}
  .ttop {{ display:flex; flex-direction:row; align-items:center; gap:{8 * scale}px; }}
  .ticon {{ width:{26 * scale}px; height:{26 * scale}px; flex:none; }}
  .tname {{ font-size:{16 * scale}px; font-weight:700; flex:none; white-space:nowrap; }}
  .tspace {{ flex:1 1 auto; min-width:{4 * scale}px; }}
  .tval {{ font-size:{32 * scale}px; font-weight:700; flex:none; width:{72 * scale}px; text-align:right; }}
  .tdetail {{ font-size:{12 * scale}px; color:#666; padding-left:{34 * scale}px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .brow {{ display:flex; flex-direction:row; align-items:center; gap:{5 * scale}px; }}
  .bname {{ font-size:{14 * scale}px; font-weight:700; flex:none; white-space:nowrap; }}
  .bval {{ font-size:{18 * scale}px; font-weight:700; flex:none; }}
  .meta {{ font-size:13px; color:#666; line-height:1.7; max-width:320px; }}
  .meta h3 {{ margin:0 0 8px; font-size:14px; color:#333; }}
  .meta code {{ background:#E3E3E3; padding:1px 5px; border-radius:4px; font-size:12px; }}
</style></head>
<body><div class="wrap">
  {''.join(screens)}
  <div class="meta">
    <h3>Quote/0 预览（{SCREEN_W}×{SCREEN_H} px，{scale}× 放大）</h3>
    <p>一共 <b>{len(pages)}</b> 屏。设备 loop 列表里每多一个「Canvas API」内容项，就多一屏可轮播的内容；点屏幕也能手动翻。</p>
    <p>设备基准：<code>{SCREEN_W}×{SCREEN_H}</code>，内边距 <code>{PAD}px</code>，每屏最多 <code>{max_rows}</code> 行。</p>
    <p>共采集到 {total} 项数据。</p>
  </div>
</div></body></html>"""


def write_preview(rows: list[UsageRow], out: Path, all_rows: list[UsageRow] | None = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(rows, all_rows), encoding="utf-8")
    return out
