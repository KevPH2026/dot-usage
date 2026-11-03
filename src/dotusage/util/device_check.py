"""自查：推一次卡片，下载设备真实渲染图，存成能直接看的 PNG。

用法：
    python3 -m dotusage.cli check

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

from dotusage.util.config import dot_api_key, load_config
from dotusage.sinks.dot import _dot_request, dot_devices

OUT = Path.home() / "Tools" / "dot-usage" / "preview" / "device-actual.png"


def resolve_device_id(key: str, configured: str | None = None) -> str:
    """设备号：先看配置，再退到「列出设备」接口拿第一台。"""
    did = (configured or load_config().get("device_id") or "").strip()
    if did:
        return did
    try:
        devs = dot_devices(key) or []
    except Exception:  # noqa: BLE001
        devs = []
    for d in devs:
        cand = str(d.get("id") or d.get("deviceId") or d.get("serialNumber") or "").strip()
        if cand:
            return cand
    return ""


def capture_device_render(
    wait: float = 6.0,
    out: Path = OUT,
    device_id: str | None = None,
) -> Path:
    """抓设备当前渲染图并转正保存。返回本地路径。"""
    key = dot_api_key()
    if not key:
        raise RuntimeError("没有 DOT_API_KEY")
    cfg_did = resolve_device_id(key, device_id)
    if not cfg_did:
        raise RuntimeError("找不到设备号：配置里 device_id 是空的，设备列表也是空的")

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
