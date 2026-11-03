"""DOT App 自己的接口（**不是** Open API）。

Open API 只有 9 个端点，只能往**已经存在**的内容项里写，改不了列表。
要「在设备上加一张卡」，得用 App 用的那套接口：

    POST /api/authV2/user/device/{deviceId}/task/loop/add/{studioId}   → 201
    POST /api/authV2/user/device/{deviceId}/task/loop/delete/{studioId} → 400「不支持的操作」
    GET  /api/authV2/user/device/{deviceId}/task/loop/list
    GET  /api/authV2/user/device/{deviceId}/combine?project=staticData

studioId 就是内容工坊里的内容类型 id：
    mindreset.canvas_api / mindreset.image_api / mindreset.text_api / mindreset.custom_rss …

**认证**：靠 App 自己的 session cookie
（`__Secure-dot-v2.session_token`）。
cookie 不在钥匙串里，而是在 App 的 HTTP 缓存里
（`~/Library/Containers/E440C1C6-.../Data/Library/Caches/tech.mindreset.dot.alpha/Cache.db`
的 `cfurl_cache_blob_data.request_object`，是二进制 plist）。

请求还带一个 `__hhaa__` 头（base64 的 plist，内容就是同一批请求头），
照抄 App 的头就行，别自己造 UA——服务端会按 UA 拒。

**删除这条路走不通**：试过 id / _id / taskKey 各种形式，
自定义内容项和出厂内容都只回「不支持的操作」。
所以出厂那 8 个小部件（天气/日历/新闻/金句…）删不掉，
只能靠调小轮播间隔让它们闪过去。
"""

from __future__ import annotations

import gzip
import json
import plistlib
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://dot.mindreset.tech"
APP_CACHE = (
    Path.home()
    / "Library/Containers/E440C1C6-CF24-42B4-A8AE-0556BE76D5A9/Data/Library/Caches"
    / "tech.mindreset.dot.alpha/Cache.db"
)

# studio id → 展示名
MAX_PER_TYPE = 6   # 一种内容类型最多建几个（安全阀，正常只需要 5）

STUDIO_IDS = {
    "canvas": "mindreset.canvas_api",
    "image": "mindreset.image_api",
    "text": "mindreset.text_api",
}


def _session_headers() -> dict:
    """从 App 的 HTTP 缓存里抄一份它的请求头（含 session cookie）。"""
    if not APP_CACHE.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{APP_CACHE}?mode=ro", uri=True)
    except Exception:  # noqa: BLE001
        return {}
    try:
        ids = [
            r[0]
            for r in con.execute(
                "select entry_ID from cfurl_cache_response "
                "where request_key like '%/studio%' or request_key like '%/user/device%'"
            ).fetchall()
        ]
        for eid in ids:
            row = con.execute(
                "select request_object from cfurl_cache_blob_data where entry_ID=?", (eid,)
            ).fetchone()
            if not row:
                continue
            try:
                obj = plistlib.loads(row[0])
            except Exception:  # noqa: BLE001
                continue
            for x in obj.get("Array", []):
                if isinstance(x, dict) and "Cookie" in x:
                    return {
                        k: (v.lstrip() if isinstance(v, str) else v) for k, v in x.items()
                    }
        return {}
    finally:
        con.close()


def call(path: str, method: str = "GET", payload=None, timeout: int = 25):
    """调 App 接口。返回 (status, body bytes)。"""
    url = path if path.startswith("http") else BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    headers = dict(_session_headers())
    if not headers:
        return 0, b""
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        if e.headers.get("Content-Encoding") == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:  # noqa: BLE001
                pass
        return e.code, raw
    except Exception:  # noqa: BLE001
        return 0, b""


def task_list(device_id: str) -> list[dict]:
    """设备 loop 列表（带 studio id 和 taskKey）。

    **必须用 combine 端点**：`/task/loop/list` 返回的是 studio 目录
    （id / _id / name），**没有 key**；taskKey 只在 combine 里。
    踩过：用错端点导致「以为没有内容项」→ 循环连建 39 个空图像项。
    """
    st, raw = call(f"/api/authV2/user/device/{device_id}/combine?project=staticData")
    if st != 200:
        return []
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    tl = ((data.get("taskData") or {}).get("loop") or {}).get("taskList") or []
    return [{"id": it.get("id"), "key": it.get("key")} for it in tl]


def add_content(device_id: str, studio_id: str) -> tuple[bool, str]:
    """给设备加一个内容项。返回 (成功, 说明)。"""
    st, raw = call(
        f"/api/authV2/user/device/{device_id}/task/loop/add/{studio_id}",
        "POST",
        {"deviceId": device_id, "taskType": "loop"},
    )
    if st in (200, 201):
        return True, f"已添加 {studio_id}"
    msg = raw[:120].decode("utf-8", "ignore") if raw else ""
    return False, f"{studio_id} 添加失败（{st} {msg}）"


def ensure_content_items(device_id: str, want: list[str], min_count: dict[str, int] | None = None) -> dict[str, list[str]]:
    """确保设备上有指定类型的内容项，缺的就建。

    want 里是 'canvas' / 'image' / 'text' 这样的短名。
    min_count 可以要求某种类型至少有几个（比如图像要 2 个：一张总览 + 一张脉搏）。

    返回 {短名: [taskKey, ...]}，按列表顺序。
    """
    out: dict[str, list[str]] = {}
    items = task_list(device_id)
    for short in want:
        studio = STUDIO_IDS.get(short, short)
        need = (min_count or {}).get(short, 1)
        have = [str(it["key"]) for it in items if it.get("id") == studio and it.get("key")]
        # 硬上限：一种类型最多 4 个内容项。
        # 踩过的坑：task_list 之前用错端点（拿不到 taskKey），循环 add 接口
        # 一次没失败、就建了 39 个空图像项，而 delete 接口又删不掉。
        # 这个上限是防再次发生的安全阀。
        while len(have) < need and len(have) < MAX_PER_TYPE:
            ok, msg = add_content(device_id, studio)
            if not ok:
                print(f"⚠️ 新建 {studio} 失败：{msg}")
                break
            items = task_list(device_id)
            new_have = [str(it["key"]) for it in items if it.get("id") == studio and it.get("key")]
            if len(new_have) == len(have):
                break          # 没新增说明接口行为和预期不符，停下来别再刷
            have = new_have
        if have:
            out[short] = have
    return out
