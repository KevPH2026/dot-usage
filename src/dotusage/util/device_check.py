"""自查：推一次卡片，下载设备真实渲染图，存成能直接看的 PNG。

用法：
    python3 -m dot_usage.cli check

原理：
    DOT 服务端会把设备的当前渲染图挂在 CDN 上（status 接口的
    renderInfo.current.image），那张图是 **180° 旋转**的（服务端坐标系约定），
    旋转回来就是设备屏幕上真实的画面。

    所以这条命令 = 推送 → 等设备刷新 → 抓渲染图 → 转正 → 存盘。
    改完排版跑一次，就能自己看到真机效果，不用靠 App 预览猜。
"""

from __future__ import annotations

import time
import urllib.request
from pathlib import Path

from dotusage.util.config import dot_api_key
from dotusage.sinks.dot import _dot_request

OUT = Path.home() / "Tools" / "dot-usage" / "preview" / "device-actual.png"


def capture_device_render(wait: float = 6.0, out: Path = OUT) -> Path:
    """抓设备当前渲染图并转正保存。返回本地路径。"""
    key = dot_api_key()
    if not key:
        raise RuntimeError("没有 DOT_API_KEY")
    cfg_did = ""

    time.sleep(wait)
    st = _dot_request("GET", f"/api/authV2/open/device/{cfg_did}/status", key)
    imgs = (st.get("renderInfo") or {}).get("current", {}).get("image") or []
    if not imgs:
        raise RuntimeError("设备没有返回渲染图")
    raw = Path("/tmp/_dot_device_render_raw.png")
    urllib.request.urlretrieve(imgs[0], raw)

    try:
        from PIL import Image

        im = Image.open(raw)
        im.rotate(180, expand=True).convert("RGB").save(out)
    except Exception:
        out.write_bytes(raw.read_bytes())
    return out
