"""把各来源的行按平台聚合成「一行一个平台」。

用户要的是「不同模型只显示一个」——
所以 z.ai 和 ZCode 合成一行，ChatGPT 和 Codex 合成一行，
每个平台只占一行：标题（图标 + 平台名 + 大数字）+ 一行紧凑明细。

明细里只留最要紧的三样：窗口百分比、重置倒计时、消耗量。
次数、型号数、余额这类次要信息另开一页，避免一行塞不下被截断。
"""

from __future__ import annotations

from dotusage.core.schema import UsageRow, compact, countdown_short, now_ts

# key → (展示名, 图标名, 归属的 source)
PLATFORMS = [
    ("chatgpt", "ChatGPT", "chatgpt", {"chatgpt", "codex"}),
    ("zai", "Z.ai", "zai", {"zai", "zcode"}),
    ("minimax", "MiniMax", "minimax", {"minimax"}),
    ("deepseek", "DeepSeek", "deepseek", {"deepseek"}),
    ("grok", "Grok", "grok", {"grok"}),
]

WINDOW_SHORT = {
    "5小时窗口": "5h",
    "7天窗口": "周",
    "周窗口": "周",
    "限流窗口": "限流",
}


def _short_window(w: str) -> str:
    if not w:
        return ""
    for k, v in WINDOW_SHORT.items():
        if k in w:
            return v
    return w.replace("窗口", "")


def _window_days(window: str) -> float:
    """窗口长度（天）：'7天窗口' → 7.0，'5小时窗口' → 0.208。"""
    import re

    m = re.search(r"(\d+)\s*天", window or "")
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+)\s*小时", window or "")
    if m:
        return float(m.group(1)) / 24
    return 0.0


def predict_exhaust(row: UsageRow) -> str:
    """「照现在的速度，配额什么时候烧完」。

    官方给的是**窗口内已用百分比** + **窗口剩余时间**。
    反推：窗口已经过了多久 → 得出日均烧多少 → 剩下的够烧几小时。

        ChatGPT 周窗口已用 90%，5.3 天后重置
        → 窗口才过去 1.7 天 → 53%/天 → 剩下的 10% 只够 4.5 小时

    比「5 天后重置」有用得多：重置是官方的，**烧完是你自己的**。
    够烧到重置就返回空串。
    """
    if row.used_percent is None or not row.resets_at:
        return ""
    wd = _window_days(row.window)
    if wd <= 0:
        return ""
    hours_to_reset = (row.resets_at - now_ts()) / 3600
    if hours_to_reset <= 0:
        return ""
    elapsed_days = wd - hours_to_reset / 24
    if elapsed_days < 0.15:          # 窗口刚开始，速率不可靠
        return ""
    rate = row.used_percent / elapsed_days
    if rate <= 0:
        return ""
    hours_left = (100 - row.used_percent) / rate * 24
    if hours_left >= hours_to_reset:   # 够烧到重置，不算事
        return ""
    # 字要短 —— 墨水屏右侧那列只有 7 个字宽
    if hours_left < 1:
        return "1小时内烧完"
    if hours_left < 48:
        return f"{hours_left:.0f}小时烧完"
    return f"{hours_left / 24:.1f}天烧完"


def aggregate(rows: list[UsageRow]) -> list[UsageRow]:
    """把多行合并成每平台一行 + 若干余额行。"""
    by_source: dict[str, list[UsageRow]] = {}
    for r in rows:
        by_source.setdefault(r.source, []).append(r)

    out: list[UsageRow] = []
    balances: list[UsageRow] = []
    specials: list[UsageRow] = []  # Tibo 重置监测这种独立指标

    for key, label, icon, sources in PLATFORMS:
        group: list[UsageRow] = []
        for s in sources:
            group.extend(by_source.get(s, []))
        if not group:
            continue

        quotas_all = [r for r in group if r.kind == "quota"]
        cons = [r for r in group if r.kind == "consumption"]
        bal = [r for r in group if r.kind == "balance"]

        # 纯余额的平台（DeepSeek）不建平台行，只留余额行
        if not quotas_all and not cons:
            for b in bal:
                b.icon = icon
                out.append(b)
            continue

        # 同一平台可能有多个模型池（MiniMax 的 general / video），
        # 用户要「一个平台显示一行」→ 只保留最紧张的那一组窗口。
        # 同一模型的多个窗口（5h / 周）要全留。
        by_model: dict[str, list[UsageRow]] = {}
        for q in quotas_all:
            by_model.setdefault(q.label, []).append(q)
        quotas = (
            max(by_model.values(), key=lambda g: max((x.used_percent or 0) for x in g))
            if by_model
            else []
        )

        # ---- 标题右侧的大数字：配额最紧张的窗口优先，其次消耗量 ----
        headline = ""
        used_percent = None
        top = None
        if quotas:
            top = max(quotas, key=lambda r: r.used_percent or 0)
            used_percent = top.used_percent
            # 没有百分比（比如 xAI 只给订阅档位）→ 拿档位短名当大数字
            headline = (
                f"{used_percent:.0f}%" if used_percent is not None else (top.headline_override or "")
            )
        elif cons:
            headline = compact(sum(r.total_tokens for r in cons))

        # ---- 紧凑文案：只留「窗口 + 倒计时」，百分比用大数字 + 进度条表示 ----
        # 注意：分隔符不用「·」——那套字体里它会渲染成一条竖杠，很脏
        win = ""
        if quotas and top is not None:
            win = _short_window(top.window)
        reset_txt = ""
        resets = [q.resets_at for q in quotas if q.resets_at]
        if resets:
            reset_txt = countdown_short(min(resets))
        # 右侧文案：能算出「什么时候烧完」就只说这个
        #（比官方的「几天后重置」更贴近会不会撞线）；
        # 算不出来就说这个窗口自己的重置倒计时——**别混两个窗口的信息**。
        burn = predict_exhaust(top) if top is not None else ""
        if burn:
            note = burn
        elif top is not None:
            note = " ".join(x for x in (win, countdown_short(top.resets_at) if top.resets_at else "") if x)
        else:
            note = ""
        if not note and top is not None:
            note = top.detail_override or ""
        if not quotas and cons:
            note = compact(sum(r.total_tokens for r in cons))

        out.append(
            UsageRow(
                source=key,
                label=label,
                model="",
                kind="platform",
                used_percent=used_percent,
                total_tokens=sum(r.total_tokens for r in cons),
                calls=sum(r.calls for r in cons),
                updated_at=max((r.updated_at for r in group), default=now_ts()),
                headline_override=headline,
                detail_override=note,
                icon=icon,
            )
        )

        # 余额 / 额度紧跟在平台行后面（Z.ai 的工具额度属于 Z.ai，DeepSeek 余额属于 DeepSeek）
        for b in bal:
            b.icon = icon
            out.append(b)

    # 平台行按配额紧张度排序（最满的排最前），余额行跟着各自的平台走
    out.sort(key=lambda r: (r.kind != "platform", -(r.used_percent or 0), r.source))
    # 独立指标（tibo）放最前面
    for r in rows:
        if r.kind == "tibo":
            specials.append(r)
    return specials + out
