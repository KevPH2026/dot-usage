"""把 UsageRow 渲染成 DOT Canvas payload。

几何基准：Quote/0 的 Canvas 布局是 **296 × 152 px**（2.66 吋墨水屏），
不是网页尺寸。所有字号、间距都按这个尺寸算。

官方约束（dot-canvas-designer 规范）：
- windowData.default 必须是数组；元素只有 div / span / img
- 元素数 <= 80，嵌套 <= 16，字符串 <= 4000 字符
- props.tw 写 Tailwind 风格类，props.style 写确定性 CSS
- 最外层不加 padding（设备布局留白），要全出血用 layoutFull
- 字号只用公开词表：text-{12,14,16,18,24,28,32}-chillduansans / text-pixel-*

排版策略：3 行封顶。
1) 配额行最优先（那才是「重置时间」的主角）：名称 + 百分比 / 细进度条 + 重置倒计时
2) 其余是消耗行：名称 + token 量 / 近 24h 明细
3) 页脚：更新时间 + 还有几项没显示
"""

from __future__ import annotations

from dotusage.core.aggregate import aggregate
from dotusage.util.icons import data_uri
from dotusage.core.schema import UsageRow, compact, countdown, iso, now_ts

MAX_ROWS = 4   # 每页行数上限（296×152 实测 5 行会压到页脚，4 行是安全值）
MAX_PAGES = 6

BAR_HEIGHT = "12px"
TRACK_BG = "#E9E9E9"
FILL_BG = "#000000"
DIVIDER = "#D9D9D9"
MUTED = "#3a3a3a"   # 墨水屏上浅灰会糊，深一点才看得清

SCREEN_W = 296
SCREEN_H = 152
PAD = 6


def summary_rows(rows: list[UsageRow]) -> list[UsageRow]:
    """把各来源的 token 合成一页「构成明细」。"""
    if not rows:
        return []
    inp = sum(r.input_tokens for r in rows)
    outp = sum(r.output_tokens for r in rows)
    cread = sum(r.cache_read_tokens for r in rows)
    cwrite = sum(r.cache_write_tokens for r in rows)
    calls = sum(r.calls for r in rows)
    cost = sum(r.cost_usd or 0 for r in rows)
    total = sum(r.total_tokens for r in rows)
    last = max((r.updated_at for r in rows), default=now_ts())

    out = [
        UsageRow(source="sum", label="总 token", model="", kind="consumption",
                 total_tokens=total, updated_at=last,
                 headline_override=compact(total),
                 detail_override=(" ".join([x for x in [f"{calls}次调用" if calls else "",
                                                       f"${cost:.2f}" if cost else ""] if x]))),
        UsageRow(source="sum", label="输入 / 输出", model="", kind="consumption",
                 input_tokens=inp, output_tokens=outp, total_tokens=inp + outp, updated_at=last,
                 headline_override=f"{compact(inp)} / {compact(outp)}",
                 detail_override=f"缓存 {compact(cread)} 另计"),
    ]
    return out


def build_pages(
    rows: list[UsageRow],
    max_rows: int | None = None,
    max_pages: int | None = None,
    device_note: str = "",
    include_summary: bool = False,
    only_tibo: bool = False,
) -> list[dict]:
    """把全部数据切成多页。

    - 第 1 页：配额优先（重置时间才是主角），空位用消耗量补
    - 中间页：剩余消耗行
    - 最后一页：token 构成 + 花费
    """
    per = max_rows or MAX_ROWS
    cap = max_pages or MAX_PAGES

    quota = [r for r in rows if r.kind == "quota"]
    cons = [r for r in rows if r.kind == "consumption"]
    balances = [r for r in rows if r.kind == "balance"]
    # 配额行按「用得最狠」排前面——最紧张的窗口最先看到；
    # 用量相同的按重置时间近的排前面
    quota.sort(key=lambda r: (-(r.used_percent or 0), r.resets_at or 9e18))
    cons.sort(key=lambda r: r.total_tokens, reverse=True)

    plat = aggregate(rows)
    platforms = [r for r in plat if r.kind == "platform"]
    balances = [r for r in plat if r.kind == "balance"]
    summary = summary_rows(rows) if include_summary else []

    # 所有内容行统一排队，按每屏行数切
    # 实测 296×152 最多 4 行：标题区 ~24px + 页脚 ~16px + 4 行 × ~26px ≈ 144px
    # 放 5 行最后一行会压到页脚上（预览里能看到重叠）
    content: list[UsageRow] = []
    content.extend([r for r in plat if r.kind == "tibo"])
    content.extend(platforms)
    content.extend(balances)
    content.extend(summary)

    groups: list[tuple[str, list[UsageRow]]] = []

    # 平台行竖排两行 → 一屏 2 个；最后一屏有富余就把余额行接上，别让余额自己占一屏
    tibo_rows = [r for r in content if r.kind == "tibo"]
    plat_rows = [r for r in content if r.kind == "platform"]
    other_rows = [r for r in content if r.kind not in ("platform", "tibo")]
    # 画板只放 Tibo 屏（总览交给图片版）：Tibo 是二元事件，必须稳定可见，
    # 不能靠轮换——轮换模式下它可能几十分钟都轮不到。
    if only_tibo:
        if not tibo_rows:
            return []
        return [
            build_canvas(
                tibo_rows[:1], rows, device_note=device_note,
                title="Tibo 重置监测", page_index=0, page_total=1, max_rows=per,
            )
        ]

    if tibo_rows:
        groups.append(("Tibo 重置监测", tibo_rows[:1]))
    plat_groups = [plat_rows[i : i + 2] for i in range(0, len(plat_rows), 2)]
    if plat_groups and other_rows:
        # 平台行竖排占两行高，余量要按「行高」算不是按「条数」算
        used = sum(2 if r.kind == "platform" else 1 for r in plat_groups[-1])
        room = per - used
        if room > 0:
            plat_groups[-1] = plat_groups[-1] + other_rows[:room]
            other_rows = other_rows[room:]
    for g in plat_groups:
        title = "配额与额度" if any(r.kind == "balance" for r in g) else "配额"
        groups.append((title, g))


    for i in range(0, len(other_rows), per):
        chunk = other_rows[i : i + per]
        title = "额度与消耗" if any(r.kind == "balance" for r in chunk) else "构成明细"
        groups.append((title, chunk))

    total = len(groups)
    return [
        build_canvas(
            g_rows, rows, device_note=device_note, title=g_title,
            page_index=i, page_total=total, max_rows=per,
        )
        for i, (g_title, g_rows) in enumerate(groups)
    ]



def select_rows(rows: list[UsageRow]) -> list[UsageRow]:
    """卡片上显示哪几行。

    配额行优先（重置时间才是重点），其次按消耗量倒序，
    同一来源同一模型只保留一条。
    """
    quota = [r for r in rows if r.kind == "quota"]
    quota.sort(key=lambda r: (r.resets_at or 0) or 9e18)
    cons = [r for r in rows if r.kind == "consumption"]
    cons.sort(key=lambda r: r.total_tokens, reverse=True)
    balances = [r for r in rows if r.kind == "balance"]

    seen: set[tuple[str, str]] = set()
    picked: list[UsageRow] = []
    for r in quota + cons + balances:
        key = (r.source, r.label, r.kind, r.window)
        if key in seen:
            continue
        seen.add(key)
        picked.append(r)
        if len(picked) >= MAX_ROWS:
            break
    return picked


def _span(text: str, tw: str, style: dict | None = None) -> dict:
    props: dict = {"tw": tw}
    if style:
        props["style"] = style
    props["children"] = text
    return {"type": "span", "props": props}


def _icon(name: str, size: int = 18) -> dict:
    """平台图标。图标是 SF Symbols 渲染的单色 PNG，内嵌成 data URI。"""
    uri = data_uri(name)
    if not uri:
        return {"type": "span", "props": {"tw": f"w-[{size}px] shrink-0", "children": ""}}
    return {
        "type": "img",
        "props": {
            "tw": f"w-[{size}px] h-[{size}px] shrink-0",
            "src": uri,
        },
    }


def _divider() -> dict:
    return {
        "type": "div",
        "props": {
            "tw": "w-full shrink-0",
            "style": {"height": "1px", "backgroundColor": DIVIDER},
            "children": "",
        },
    }


def _bar(percent: float, width: int = 150, height: int | None = None) -> dict:
    """真进度条：轨道 + 填充，宽度写死百分比（不依赖设备端算术）。"""
    pct = max(0.0, min(100.0, percent))
    return {
        "type": "div",
        "props": {
            "tw": "shrink-0 overflow-hidden",
            "style": {
                "width": f"{width}px",
                "height": f"{height or BAR_HEIGHT}",
                "backgroundColor": TRACK_BG,
                "borderRadius": "2px",
            },
            "children": {
                "type": "div",
                "props": {
                    "tw": "h-full",
                    "style": {
                        "width": f"{pct:.1f}%",
                        "backgroundColor": FILL_BG,
                        "borderRadius": "2px",
                    },
                    "children": "",
                },
            },
        },
    }


def _quota_row(r: UsageRow) -> dict:
    """配额行：名称 + 百分比，下面是进度条 + 重置倒计时。"""
    reset = countdown(r.resets_at) or "重置时间未知"
    return {
        "type": "div",
        "props": {
            "tw": "flex flex-col gap-[3px] shrink-0",
            "children": [
                {
                    "type": "div",
                    "props": {
                        "tw": "flex flex-row items-baseline justify-between gap-[6px] min-w-0",
                        "children": [
                            _span(
                                r.label,
                                "text-16-chillduansans font-bold min-w-0",
                                {"overflow": "hidden", "textOverflow": "ellipsis", "whiteSpace": "nowrap"},
                            ),
                            _span(f"{r.used_percent:.0f}%", "text-16-chillduansans font-bold shrink-0"),
                        ],
                    },
                },
                {
                    "type": "div",
                    "props": {
                        "tw": "flex flex-row items-center gap-[6px] min-w-0",
                        "children": [
                            _bar(r.bar_width),
                            _span(
                                reset,
                                "text-12-chillduansans shrink-0",
                                {"color": MUTED},
                            ),
                        ],
                    },
                },
            ],
        },
    }


def _consumption_row(r: UsageRow) -> dict:
    """消耗行：名称 + token 量，下面一行明细。"""
    return {
        "type": "div",
        "props": {
            "tw": "flex flex-col gap-[2px] shrink-0",
            "children": [
                {
                    "type": "div",
                    "props": {
                        "tw": "flex flex-row items-baseline justify-between gap-[6px] min-w-0",
                        "children": [
                            _span(
                                r.label,
                                "text-16-chillduansans min-w-0",
                                {"overflow": "hidden", "textOverflow": "ellipsis", "whiteSpace": "nowrap"},
                            ),
                            _span(r.headline, "text-16-chillduansans font-bold shrink-0"),
                        ],
                    },
                },
                _span(
                    r.detail,
                    "text-12-chillduansans min-w-0",
                    {"color": MUTED, "overflow": "hidden", "textOverflow": "ellipsis", "whiteSpace": "nowrap"},
                ),
            ],
        },
    }


def _platform_row(r: UsageRow) -> dict:
    """平台行：竖排两行。

    上行 = 图标 + 平台名 + 大百分比
    下行 = 粗进度条 + 重置倒计时

    296px 宽放不下横排的五个元素（名、条、倒计时、数值、图标），
    竖排两行每个元素都能给足尺寸，缩略预览也看得清。
    """
    # 大数字：百分比用大字，档位名（Premium+）太长就缩一档
    # 实测 ChillKSans：Premium+ 在 16px 下宽 73px，14px 下宽 64px
    head_text = r.headline
    head_cls = "text-24-chillduansans font-bold shrink-0"
    head_w = "60px"
    if len(head_text) > 5:
        head_cls = "text-14-chillduansans font-bold shrink-0"
        head_w = "80px"

    top = {
        "type": "div",
        "props": {
            "tw": "flex flex-row items-center gap-[6px] shrink-0",
            "children": [
                _icon(r.icon, 22),
                _span(
                    r.label,
                    "text-16-chillduansans font-bold shrink-0",
                    {"width": "76px", "overflow": "hidden", "textOverflow": "ellipsis", "whiteSpace": "nowrap"},
                ),
                {"type": "div", "props": {"tw": "flex-1 min-w-[4px]", "children": ""}},
                _span(
                    head_text,
                    head_cls,
                    {"width": head_w, "textAlign": "right"},
                ),
            ],
        },
    }

    bottom_children: list[dict] = []
    if r.used_percent is not None:
        bottom_children.append(_bar(r.used_percent, width=120, height=10))
    else:
        bottom_children.append({"type": "div", "props": {"tw": "flex-1", "children": ""}})
    if r.detail:
        bottom_children.append(
            _span(
                r.detail,
                "text-12-chillduansans shrink-0",
                {"color": MUTED, "whiteSpace": "nowrap"},
            )
        )
    bottom = {
        "type": "div",
        "props": {
            "tw": "flex flex-row items-center gap-[8px] shrink-0 pl-[28px]",
            "children": bottom_children,
        },
    }

    return {
        "type": "div",
        "props": {
            "tw": "flex flex-col gap-[2px] shrink-0 w-full",
            "children": [top, bottom],
        },
    }


def _balance_row(r: UsageRow) -> dict:
    """余额行：图标 + 名字 + 数值，比平台行紧凑（余额不是配额，没条也没倒计时）。"""
    children: list[dict] = [_icon(r.icon, 18)]
    children.append(
        _span(
            r.label,
            "text-14-chillduansans font-bold shrink-0",
            {"overflow": "hidden", "textOverflow": "ellipsis", "whiteSpace": "nowrap"},
        )
    )
    children.append({"type": "div", "props": {"tw": "flex-1 min-w-[6px]", "children": ""}})
    children.append(_span(r.headline, "text-18-chillduansans font-bold shrink-0"))
    return {
        "type": "div",
        "props": {"tw": "flex flex-row items-center gap-[5px] shrink-0", "children": children},
    }


def _tibo_row(r: UsageRow) -> dict:
    """Tibo 重置监测行：纯纵向文本，全部左对齐。

    刻意不用 justify-between / flex-1 spacer：
    Canvas 渲染器对「靠 flex 撑开再右对齐」支持得不稳，
    实测出现过整个内容块右移、左半屏空掉。
    这里改成从上到下四段文字，结构跟「文本 API」一致，最不容易出问题。
    """
    lines = [
        _span(r.label, "text-18-chillduansans font-bold"),
        _span(r.headline, "text-32-chillduansans font-bold"),
    ]
    if r.detail_override:
        lines.append(_span(r.detail_override, "text-13-chillduansans", {"color": MUTED}))
    if r.detail2_override:
        lines.append(_span(r.detail2_override, "text-13-chillduansans", {"color": MUTED}))

    return {
        "type": "div",
        "props": {
            "tw": "flex flex-col w-full h-full min-w-0 min-h-0 bg-white text-black gap-[6px] justify-start",
            "children": lines,
        },
    }


def _row_element(r: UsageRow) -> dict:
    if r.kind == "tibo":
        return _tibo_row(r)
    if r.kind == "platform":
        return _platform_row(r)
    if r.kind == "balance":
        return _balance_row(r)
    if r.kind == "quota":
        return _quota_row(r)
    return _consumption_row(r)


def build_canvas(
    rows: list[UsageRow],
    all_rows: list[UsageRow] | None = None,
    device_note: str = "",
    title: str = "AI 用量",
    page_index: int = 0,
    page_total: int = 0,
    max_rows: int | None = None,
) -> dict:
    """生成可直接 POST 的 Canvas payload。"""
    # 保持传入顺序（分页逻辑已经排好序了，别在这里二次排序）
    picked = list(rows)[: (max_rows or MAX_ROWS)]
    total = len(all_rows if all_rows is not None else rows)
    hidden = max(0, total - len(picked))

    stamp = iso(now_ts())
    clock = stamp[11:16]

    right_bits = []
    if page_total > 1:
        right_bits.append(f"{page_index + 1}/{page_total}")
    right_bits.append(clock)

    header = {
        "type": "div",
        "props": {
            "tw": "flex flex-row items-baseline justify-between gap-[8px] shrink-0 w-full",
            "children": [
                _span(title, "text-16-chillduansans font-bold"),
                _span(" · ".join(right_bits), "text-12-chillduansans", {"color": MUTED}),
            ],
        },
    }

    body_children: list[dict] = []
    for i, r in enumerate(picked):
        if i:
            body_children.append(_divider())
        body_children.append(_row_element(r))

    body = {
        "type": "div",
        "props": {
            "tw": "flex flex-col flex-1 min-h-0 justify-start gap-[6px] w-full",
            "children": body_children,
        },
    }

    root = {
        "type": "div",
        "props": {
            "tw": "flex flex-col w-full h-full min-w-0 min-h-0 bg-white text-black items-stretch",
            "children": [header, _divider(), body],
        },
    }

    return {
        "data": {
            "title": title,
            "updated": stamp,
            "rows": [r.to_dict() for r in picked],
        },
        "windowData": {"default": [root]},
        "layoutFull": {"tw": f"p-[{PAD}px]", "style": {"padding": f"{PAD}px", "backgroundColor": "#FFFFFF"}},
        "border": 0,
    }


def summarize(rows: list[UsageRow]) -> str:
    """给命令行 / 飞书用的纯文本摘要。"""
    lines = []
    for r in select_rows(rows):
        lines.append(f"{r.label:<24} {r.headline:>9}   {r.detail}")
    return "\n".join(lines)
