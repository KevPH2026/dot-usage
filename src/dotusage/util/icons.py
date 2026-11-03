"""平台图标：把 icons/png/*.png 读成 data URI，供 Canvas 的 img 元素用。

PNG 是用 SF Symbols 渲染的单色小图（icons/render_icons.swift），
几百字节一个，内嵌进 payload 不会把体积撑爆。
"""

from __future__ import annotations

import base64
from pathlib import Path

ICON_DIR = Path.home() / "Tools" / "dot-usage" / "icons" / "png"
_cache: dict[str, str] = {}


def data_uri(name: str) -> str:
    """返回 PNG 的 data URI；没有图标就返回空串。"""
    if not name:
        return ""
    if name in _cache:
        return _cache[name]
    path = ICON_DIR / f"{name}.png"
    if not path.exists():
        _cache[name] = ""
        return ""
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    uri = f"data:image/png;base64,{b64}"
    _cache[name] = uri
    return uri


def available() -> list[str]:
    if not ICON_DIR.exists():
        return []
    return sorted(p.stem for p in ICON_DIR.glob("*.png"))
