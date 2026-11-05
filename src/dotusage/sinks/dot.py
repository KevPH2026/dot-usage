"""DOT 墨水屏设备推送。

这条链路是官方 Open API：
    GET  /api/authV2/open/device/{id}/loop/list   → 找到 Canvas API 内容项的 key
    POST /api/authV2/open/device/{id}/canvas        → 写入卡片并刷新屏幕

注意：Canvas API 要求设备上已经存在一个「Canvas API」内容项（在 Dot App 的
Content Studio 里加到 loop/fixed 列表）。没有它服务端会回 noCanvasApiContent，
这时候脚本只报错、不乱写别的内容项。

**这个文件只管墨水屏**。别的输出目标各自一个 sink 文件，
加新目标不用改这里，也不用改核心代码。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from dotusage.util import config as cfgmod
from dotusage.core.schema import UsageRow, now_ts

from .base import PushError, Sink, SinkSkip, register

BASE_URL = "https://dot.mindreset.tech"

__all__ = [
    "PushError",
    "DotDeviceSink",
    "dot_devices",
    "find_content_keys",
    "push_dot",
    "push_dot_image",
    "push_dot_pages",
    "push_dot_smart",
    "push_dot_text",
    "switch_next",
]


# --------------------------------------------------------------------------
# DOT
# --------------------------------------------------------------------------
def _dot_request(method: str, path: str, key: str, body: dict | None = None,
                 timeout: int = 25, retries: int = 4) -> dict:
    """调 DOT Open API。

    **429 要退避重试**：一次推送要打 40 个图像内容项，
    很容易撞上 key 的速率限制（实测约 40 次/分）。
    直接失败会让整轮推送白跑，所以这里自动退避重试。
    """
    url = f"https://dot.mindreset.tech{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    last_err = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
                try:
                    return json.loads(raw)
                except Exception:  # noqa: BLE001
                    return {"raw": raw}
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", errors="ignore")[:200]
            if e.code == 429 and attempt < retries - 1:
                wait = 8 * (attempt + 1)
                cfgmod.log_line(f"429 rate limited, retry in {wait}s: {path}")
                time.sleep(wait)
                last_err = msg
                continue
            raise PushError(f"{method} {path} → HTTP {e.code}: {msg}") from e
        except Exception as e:  # noqa: BLE001
            raise PushError(f"{method} {path} → {str(e)[:160]}") from e
    raise PushError(f"{method} {path} → 重试 {retries} 次仍失败: {last_err}")


def _iter_dicts(obj):
    """递归遍历 JSON 里的所有 dict（不假设服务端返回的确切层级）。"""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v)


def _type_of(node: dict) -> str:
    for k in ("type", "taskType", "contentType", "usageGroup"):
        v = node.get(k)
        if isinstance(v, str) and v:
            return v.upper()
    return ""


def _key_of(node: dict) -> str | None:
    """取内容项的 key。

    实测服务端对没有 key 的项会返回字符串 'None'（不是 null），
    这个值必须当无效处理，否则会拿它当 taskKey 去写，写不进去。
    """
    for k in ("key", "taskKey", "id"):
        v = node.get(k)
        if isinstance(v, str) and v and v != "None":
            return v
    return None


def _find_canvas_keys(api_key: str, device_id: str, task_type: str) -> list[str]:
    """按列表顺序找出**所有** Canvas API 内容项的 key。

    服务端返回结构（result / result.list / result.tasks …）随版本变过，
    所以不按固定路径取：先找「最像任务列表的 dict 数组」，在里面按顺序找
    类型含 CANVAS 的条目；找不到再退化成全量递归扫描。
    """
    lst = _dot_request("GET", f"/api/authV2/open/device/{device_id}/{task_type}/list", api_key)
    # 实测这个端点直接返回数组（不是 {result: [...]}），两种都要认
    if isinstance(lst, dict):
        code = lst.get("code")
        if code not in (0, 200, None):
            raise PushError(f"读取内容列表失败：{json.dumps(lst, ensure_ascii=False)[:200]}")
    elif not isinstance(lst, list):
        return []

    arrays: list[list] = []  # noqa: E501

    def collect(o):
        if isinstance(o, list):
            if o and all(isinstance(x, dict) for x in o):
                arrays.append(o)
            for x in o:
                collect(x)
        elif isinstance(o, dict):
            for v in o.values():
                collect(v)

    collect(lst)

    best: list[str] = []
    for arr in arrays:
        keys = []
        for it in arr:
            if not isinstance(it, dict):
                continue
            k = _key_of(it)
            if not k:
                continue
            if "CANVAS" in _type_of(it):
                keys.append(k)
        if len(keys) > len(best):
            best = keys
    if best:
        return best

    for node in _iter_dicts(lst):
        k = _key_of(node)
        if k and "CANVAS" in _type_of(node):
            return [k]
    return []


def _canvas_body(canvas: dict, cfg: dict, task_type: str, task_key: str | None, alias: str) -> dict:
    body = {
        "taskType": task_type,
        "refreshNow": True,
        "taskAlias": alias,
        "data": canvas.get("data", {}),
        "windowData": canvas.get("windowData", {}),
        "layoutFull": canvas.get("layoutFull", {}),
        "border": canvas.get("border", 0),
    }
    # 不传 taskKey 时，服务端会更新列表里的第一个 Canvas API 项
    if task_key:
        body["taskKey"] = task_key
    if canvas.get("link"):
        body["link"] = canvas["link"]
    return body


def find_content_keys(
    api_key: str,
    device_id: str,
    task_type: str = "loop",
    want: str = "CANVAS",
) -> list[str]:
    """找出列表里 type 含 want（如 CANVAS / IMAGE）的内容项 key，按列表顺序。"""
    lst = _dot_request("GET", f"/api/authV2/open/device/{device_id}/{task_type}/list", api_key)
    if isinstance(lst, dict):
        code = lst.get("code")
        if code not in (0, 200, None):
            raise PushError(f"读取内容列表失败：{json.dumps(lst, ensure_ascii=False)[:200]}")
    elif not isinstance(lst, list):
        return []

    keys: list[str] = []
    for node in _iter_dicts(lst):
        k = _key_of(node)
        if not k:
            continue
        if want in _type_of(node):
            keys.append(k)
    # 去重保序
    seen: set[str] = set()
    uniq = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def push_dot_image(image_b64: str, cfg: dict, api_key: str | None = None,
                   task_key: str | None = None, alias: str = "AI 用量总览") -> str:
    """把 PNG（base64）推到设备的图片 API 内容项。"""
    key = api_key or cfgmod.dot_api_key()
    if not key:
        raise PushError("没有 DOT_API_KEY，无法推送图片")
    device_id = cfg["device_id"]
    task_type = cfg.get("task_type", "loop")

    if not task_key:
        keys = find_content_keys(key, device_id, task_type, "IMAGE")
        if not keys:
            raise PushError(
                f"设备 {device_id} 的 {task_type} 列表里没有图片 API 内容项。"
                "请在 Dot. App → 内容工坊里加一个「图片 API」内容项。"
            )
        task_key = keys[0]

    body = {
        "taskType": task_type,
        "taskKey": task_key,
        "refreshNow": True,
        "taskAlias": alias,
        "image": image_b64,
        "border": 0,
        "ditherType": "DIFFUSION",
    }
    res = _dot_request("POST", f"/api/authV2/open/device/{device_id}/image", key, body)
    code = res.get("code") if isinstance(res, dict) else None
    if code not in (0, 200, None):
        raise PushError(f"图片推送被拒: {json.dumps(res, ensure_ascii=False)[:300]}")
    return "图片已推送"


def push_dot_pages(pages: list[dict], cfg: dict, api_key: str | None = None) -> str:
    """把多页卡片写进设备。

    三种情况：
    1) 设备上的 Canvas 内容项 ≥ 页数 → 一页一项，设备按 loop 间隔自己轮播，点一下也能翻
    2) Canvas 内容项不够 → 每次跑轮换写一页，轮流显示
    3) 列表里认不出 Canvas 项 → 不带 taskKey 写，服务端会自动更新第一个 Canvas 项
    """
    key = api_key or cfgmod.dot_api_key()
    if not key:
        raise PushError("没有 DOT_API_KEY，无法推送墨水屏")

    device_id = cfg["device_id"]
    task_type = cfg.get("task_type", "loop")
    alias_base = cfg.get("task_alias", "AI 用量")

    keys = _find_canvas_keys(key, device_id, task_type)

    def post(canvas: dict, task_key: str | None, alias: str) -> None:
        body = _canvas_body(canvas, cfg, task_type, task_key, alias)
        res = _dot_request("POST", f"/api/authV2/open/device/{device_id}/canvas", key, body)
        code = res.get("code") if isinstance(res, dict) else None
        if code not in (0, 200, None):
            raise PushError(f"Canvas 推送被拒: {json.dumps(res, ensure_ascii=False)[:300]}")

    # 1) 够用：每页写一项
    if keys and len(keys) >= len(pages):
        for i, page in enumerate(pages):
            post(page, keys[i], f"{alias_base} {i + 1}/{len(pages)}")
        return f"墨水屏已更新 {len(pages)} 屏（每页一个 Canvas 内容项，屏幕会自己轮播）"

    # 2) 不够：轮换写一页
    if keys:
        state = cfgmod.read_dot_state()
        cursor = int(state.get("page_cursor") or 0)
        idx = cursor % len(pages)
        post(pages[idx], keys[0], f"{alias_base} {idx + 1}/{len(pages)}")
        cfgmod.write_dot_state({"page_cursor": cursor + 1, "updated_at": now_ts()})
        return (
            f"墨水屏已更新第 {idx + 1}/{len(pages)} 屏"
            f"（设备只有 {len(keys)} 个 Canvas 内容项，改为轮换显示；"
            "在 Content Studio 再加 Canvas API 内容项就能同时显示多屏）"
        )

    # 3) 认不出来：交给服务端匹配第一个 Canvas 项
    try:
        post(pages[0], None, alias_base)
        return "墨水屏已更新 1 屏（未识别到画板内容项，由服务端自动匹配）"
    except PushError as e:
        hint = (
            "设备上还没有「画板 API」内容项。"
            "请在 Dot. App → 内容工坊（Content Studio）里，给这台 Quote/0 "
            "加一个「画板 API」内容项，放进循环列表；"
            "想多屏就多建几个（一个内容项 = 一屏）。"
        )
        raise PushError(f"{e}｜{hint}") from e


def push_dot(canvas: dict, cfg: dict, api_key: str | None = None) -> str:
    """单页推送（兼容旧调用）。"""
    return push_dot_pages([canvas], cfg, api_key)


def push_dot_text(
    cfg: dict,
    api_key: str | None = None,
    task_key: str | None = None,
    alias: str = "Tibo",
    title: str = "",
    message: str = "",
    signature: str = "",
    icon_name: str = "",
) -> str:
    """走文本 API 推一条内容（标题 / 消息 / 签名 / 图标）。

    文本 API 的结构最简单——没有 flex 布局，设备原生渲染文字，
    适合二元事件这种「一个超大结论 + 一行说明」的屏。
    """
    key = api_key or cfgmod.dot_api_key()
    if not key:
        raise PushError("没有 DOT_API_KEY，无法推送文本")
    device_id = cfg["device_id"]
    task_type = cfg.get("task_type", "loop")

    if not task_key:
        keys = find_content_keys(key, device_id, task_type, "TEXT")
        if not keys:
            raise PushError(
                f"设备 {device_id} 的 {task_type} 列表里没有文本 API 内容项。"
                "请在 Dot. App → 内容工坊里加一个「文本 API」内容项。"
            )
        task_key = keys[0]

    body = {
        "taskType": task_type,
        "taskKey": task_key,
        "refreshNow": True,
        "taskAlias": alias,
        "title": title,
        "message": message,
        "signature": signature,
        "styles": {
            "title": {"fontSize": 16, "fontWeight": 600, "fontFamily": "ChillDuanSans"},
            "message": {"fontSize": 40, "fontWeight": 700, "fontFamily": "ChillDuanSans", "lineHeight": 1.1},
            "signature": {"fontSize": 11, "fontWeight": 400, "fontFamily": "ChillDuanSans"},
        },
        "border": 0,
    }
    if icon_name:
        from dotusage.util.icons import data_uri

        uri = data_uri(icon_name)
        if uri:
            body["icon"] = uri

    res = _dot_request("POST", f"/api/authV2/open/device/{device_id}/text", key, body)
    code = res.get("code") if isinstance(res, dict) else None
    if code not in (0, 200, None):
        raise PushError(f"文本推送被拒: {json.dumps(res, ensure_ascii=False)[:300]}")
    return "文本已推送"


def _push_images(device_id: str, key: str, task_type: str,
                 pairs: list[tuple[str, str, str]], dither: str) -> int:
    """推多张图到多个图像内容项（限速串行 + **只有第一张立即刷新**）。

    pairs = [(taskKey, base64 png, 别名)]

    两个坑：
    1. Open API 的 key 有速率限制，8 路并发直接 429；
       实测**间隔 1 秒串行**能跑到 50 次/分不触发。
    2. **refreshNow: true 会让设备立刻重刷屏幕**。
       设备上有几十个图像项（出厂内容删不掉，只能靠「把卡片铺满」避免空屏），
       40 个都 refreshNow 就是每 1 秒重刷一次 —— 屏幕闪得没法看。
       所以只有第一张 refreshNow，其余等设备自己轮到时再显示。
    """
    if not pairs:
        return 0
    ok = 0
    for idx, (k, b64, alias) in enumerate(pairs):
        body = {
            "taskType": task_type, "taskKey": k,
            # 只有第一张立刻刷新，其余静默更新，等设备轮到这个内容项时自然显示
            "refreshNow": idx == 0,
            "taskAlias": alias, "image": b64, "border": 0, "ditherType": dither,
        }
        for attempt in range(3):
            try:
                _dot_request("POST", f"/api/authV2/open/device/{device_id}/image", key, body)
                ok += 1
                break
            except PushError as e:
                if "429" in str(e) and attempt < 2:
                    time.sleep(12 * (attempt + 1))
                    continue
                cfgmod.log_line(f"image push failed key={k}: {str(e)[:120]}")
                break
        time.sleep(1.0)
    return ok


def _today_digest(snap: dict | None = None) -> tuple[str, str] | None:
    """窗口内的 token 总量 + 最费的两个模型（给文本卡「速览」用）。

    数据来自状态文件里的 models 段（按模型维度算的，比平台行更细）。

    顺带算出这个合计是「哪个窗口」的数：窗口现在每个源都能单独配，
    混着加没有意义，所以要么统一成一个窗口，要么标题里说清楚。
    """
    from dotusage.core.schema import compact

    snap = snap if snap is not None else (cfgmod.read_state() or {})
    models = [m for m in (snap.get("models") or []) if (m.get("today_tokens") or 0) > 0]
    if not models:
        return None
    total = sum(int(m["today_tokens"]) for m in models)
    models.sort(key=lambda m: -int(m["today_tokens"]))
    sig = " · ".join(f"{m['model']} {compact(m['today_tokens'])}" for m in models[:2])
    return compact(total), sig


def _digest_window(snap: dict | None = None) -> str:
    """消耗行用的窗口标签。全源同一个窗口就返回它，混着就返回空串。"""
    snap = snap if snap is not None else (cfgmod.read_state() or {})
    wins = {
        str(r.get("window") or "近24h")
        for r in (snap.get("rows") or [])
        if r.get("kind") == "consumption" and (r.get("total_tokens") or 0) > 0
    }
    if len(wins) == 1:
        return next(iter(wins))
    return ""


def push_dot_smart(rows, cfg: dict, api_key: str | None = None, state: dict | None = None) -> str:
    """按设备上**已有的内容项类型**自动分配通道。

    三张卡，三个通道，固定分工（用户要求「两张卡必须分开」）：

    | 卡 | 内容 | 通道 | 备选 |
    |---|---|---|---|
    | **Tibo 重置** | 超大 YES/NO + 上次重置 + 概率窗口 | 画板 API | 文本 API |
    | **用量消耗** | 各平台配额 + 余额 + 工具额度 | 图像 API（一屏总览） | 第 2 个画板 API |
    | **速览** | 窗口内 token 总量 + 最费的两个模型 | 文本 API | — |

    通道分配要看**设备上已经有哪几类内容项**，没有的跳过。
    画板只有 1 个时给 Tibo（画板版文字最锐利），消耗卡就走图像 API。
    """
    from dotusage.core.aggregate import aggregate
    from dotusage.cards.canvas import build_pages

    key = api_key or cfgmod.dot_api_key()
    if not key:
        raise PushError("没有 DOT_API_KEY，无法推送墨水屏")
    device_id = cfg["device_id"]
    task_type = cfg.get("task_type", "loop")
    alias_base = cfg.get("task_alias", "AI 用量")
    # 本轮采集的状态（脉搏 / 项目账都在里面）。没传就读磁盘上的那份。
    snap = state or cfgmod.read_state() or {}

    msgs: list[str] = []

    text_keys = find_content_keys(key, device_id, task_type, "TEXT")
    canvas_keys = find_content_keys(key, device_id, task_type, "CANVAS")
    image_keys = find_content_keys(key, device_id, task_type, "IMAGE")

    # Open API 只能往已有内容项里写，不能新建。
    # 缺内容项时用 App 自己的接口补建（POST /task/loop/add/{studioId}）。
    missing = []
    if not canvas_keys:
        missing.append("canvas")
    if not image_keys:
        missing.append("image")
    if not text_keys:
        missing.append("text")
    if missing:
        try:
            from dotusage.sinks.dot_app import ensure_content_items

            created = ensure_content_items(device_id, missing)
            if created:
                text_keys = find_content_keys(key, device_id, task_type, "TEXT")
                canvas_keys = find_content_keys(key, device_id, task_type, "CANVAS")
                image_keys = find_content_keys(key, device_id, task_type, "IMAGE")
                msgs.append(f"自动补建内容项：{'、'.join(created)}")
        except Exception as e:  # noqa: BLE001
            cfgmod.log_line(f"auto create content item failed: {e}")

    rows = list(rows)
    tibo_rows = [r for r in rows if r.kind == "tibo"]
    canvas_idx = 0

    def push_canvas(pages, target_key: str, label: str):
        body = _canvas_body(pages[0], cfg, task_type, target_key, label)
        res = _dot_request("POST", f"/api/authV2/open/device/{device_id}/canvas", key, body)
        if (res.get("code") if isinstance(res, dict) else None) not in (0, 200, None):
            raise PushError(f"画板推送被拒: {json.dumps(res, ensure_ascii=False)[:200]}")

    # ---- 卡 1：Tibo 重置（优先画板：超大 NO + 概率窗口排版最漂亮）----
    if tibo_rows:
        t = tibo_rows[0]
        sig = t.detail_override
        if t.detail2_override:
            sig = f"{sig}\n{t.detail2_override}"
        if canvas_keys:
            pages = build_pages(rows, max_rows=cfg.get("max_rows_per_page"),
                                device_note=device_id[-4:], only_tibo=True)
            if pages:
                push_canvas(pages, canvas_keys[0], "Tibo 重置")
                canvas_idx = 1
                msgs.append("Tibo 卡 → 画板 API")
        elif text_keys:
            push_dot_text(
                cfg, key, task_key=text_keys[0], alias="Tibo 重置",
                title="Tibo 今天重置了吗", message=t.headline, signature=sig, icon_name=t.icon,
            )
            msgs.append("Tibo 卡 → 文本 API")

    # ---- 卡 2：用量消耗（优先图像 API：一屏放下所有平台）----
    quota_pages = build_pages(rows, max_rows=cfg.get("max_rows_per_page"),
                              device_note=device_id[-4:], only_tibo=False)
    quota_pages = [p for p in quota_pages if p.get("data", {}).get("title") != "Tibo 重置监测"]

    if image_keys:
        from dotusage.cards.usage import overview_rows, render_image_b64

        # 总览图只放消耗，不含 Tibo；一屏最多 5 行
        b64 = render_image_b64(overview_rows(aggregate(rows), limit=5))
        dither = cfg.get("image_dither_type", "ORDERED")

        # 脉搏卡（24 小时节奏）——有快照里的小时数据就一起推
        pulse_png = b""
        try:
            from dotusage.core.pulse import from_snapshot, render_png

            hourly = from_snapshot(snap)
            if hourly:
                import base64

                pulse_png = base64.b64encode(render_png(hourly)).decode("ascii")
        except Exception as e:  # noqa: BLE001
            cfgmod.log_line(f"pulse render failed: {e}")

        # 项目烧钱榜（按工作目录拆的账）
        proj_png = b""
        try:
            from dotusage.core.projects import collect_projects, render_png as render_proj

            pdata = snap.get("projects")
            if pdata and pdata.get("rows"):
                import base64

                proj_png = base64.b64encode(render_proj(pdata)).decode("ascii")
        except Exception as e:  # noqa: BLE001
            cfgmod.log_line(f"projects render failed: {e}")

        # 设备上的图像项可能有几十个（出厂内容删不掉），
        # 全部铺上卡片，避免轮播时出现空白屏；三种卡轮着来。
        # 官方重置时间（和「烧完预测」是两回事：那个是推算，这个是官方下发的时刻）
        reset_png = b""
        try:
            from dotusage.cards.reset_list import render_png as render_reset, reset_rows

            items = reset_rows(rows)
            if items:
                import base64

                reset_png = base64.b64encode(render_reset(items)).decode("ascii")
        except Exception as e:  # noqa: BLE001
            cfgmod.log_line(f"reset card render failed: {e}")

        # 额度窗口：两个窗口并排（最近的 5 小时 + 最近的周）
        win_png = b""
        try:
            from dotusage.cards.window import pick_windows, render_png as render_win

            wdata = pick_windows(rows)
            if wdata.get("table"):
                import base64

                win_png = base64.b64encode(render_win(wdata)).decode("ascii")
        except Exception as e:  # noqa: BLE001
            cfgmod.log_line(f"window card render failed: {e}")

        variants: list[tuple[str, str]] = [(b64, f"{alias_base} 消耗")]
        if pulse_png:
            variants.append((pulse_png, "用量脉搏"))
        if proj_png:
            variants.append((proj_png, "项目烧钱榜"))
        if reset_png:
            variants.append((reset_png, "官方重置时间"))
        if win_png:
            variants.append((win_png, "额度窗口"))

        pairs: list[tuple[str, str, str]] = []
        for i, k in enumerate(image_keys):
            png, alias = variants[i % len(variants)]
            if len(image_keys) > len(variants):
                # 槽位比卡片多 → 同名项加序号，免得 App 里两行标题一样分不清
                alias = f"{alias} {i // len(variants) + 1}"
            pairs.append((k, png, alias))
        if len(variants) < len(image_keys):
            cfgmod.log_line(
                f"image variants {len(variants)} < slots {len(image_keys)}: "
                + ", ".join(a for _p, a in variants)
            )
        ok = _push_images(device_id, key, task_type, pairs, dither)
        if ok:
            msgs.append(f"消耗 / 脉搏 / 项目榜 / 重置列表 / 额度窗口 → 图像 API ×{ok}（{len(pairs)} 个图像项）")
    elif canvas_idx < len(canvas_keys) and quota_pages:
        push_canvas(quota_pages, canvas_keys[canvas_idx], f"{alias_base} 消耗")
        msgs.append("消耗卡 → 画板 API")

    # ---- 卡 3：速览（文本 API：一行大字 + 一行签名）----
    if text_keys:
        try:
            sig3 = _today_digest(snap)
            if sig3:
                # 标题跟着实际窗口走：默认近24h 就写「近24h token 消耗」，
                # 各源窗口不一致时不硬写「今日」，免得对不上账。
                win = _digest_window(snap)
                title = f"{win} token 消耗" if win else "token 消耗"
                push_dot_text(
                    cfg, key, task_key=text_keys[0], alias="速览",
                    title=title, message=sig3[0], signature=sig3[1],
                    icon_name="summary",
                )
                msgs.append("速览 → 文本 API")
        except PushError as e:
            msgs.append(f"速览失败：{e}")

    if not msgs:
        raise PushError(
            f"设备 {device_id} 的 {task_type} 列表里没有可写的内容项。"
            "请在 Dot. App → 内容工坊里加「文本 API」「画板 API」或「图像 API」内容项。"
        )
    return "；".join(msgs)


def switch_next(cfg: dict, api_key: str | None = None) -> str:
    """让设备立刻切到下一屏内容。"""
    key = api_key or cfgmod.dot_api_key()
    if not key:
        raise PushError("没有 DOT_API_KEY，无法切换屏幕")
    device_id = cfg["device_id"]
    res = _dot_request("POST", f"/api/authV2/open/device/{device_id}/next", key, {})
    code = res.get("code") if isinstance(res, dict) else None
    if code not in (0, 200, None):
        raise PushError(f"切换失败: {json.dumps(res, ensure_ascii=False)[:200]}")
    return "设备已切到下一屏"


def dot_devices(api_key: str | None = None) -> list[dict]:
    key = api_key or cfgmod.dot_api_key()
    if not key:
        raise PushError("没有 DOT_API_KEY")
    res = _dot_request("GET", "/api/authV2/open/devices", key)
    # 实测这个端点直接返回数组，不是 {result: [...]}
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        items = res.get("result") or res.get("data") or []
        if isinstance(items, dict):
            items = items.get("list") or items.get("devices") or []
        return items if isinstance(items, list) else []
    return []


class DotDeviceSink(Sink):
    """把卡片推到墨水屏设备。"""

    name = "dot"
    label = "墨水屏设备"
    needs_secret = True

    def push(self, rows, cfg: dict, state: dict, force: bool = False) -> str:
        if not cfgmod.dot_api_key():
            # 没配密钥不是故障，是还没配；跳过即可，别让定时任务每次都报红
            raise SinkSkip("还没配 DOT_API_KEY，跳过墨水屏推送")
        return push_dot_smart(rows, cfg, state=state)


register(DotDeviceSink())
