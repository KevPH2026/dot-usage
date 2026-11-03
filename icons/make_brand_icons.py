#!/usr/bin/env python3
"""从本机已安装的各家 App 里抠出**真正的品牌 Logo**，转成墨水屏能用的黑白单色图。

为什么不用 SF Symbols：那些是"近似图形"（六边形、闪电），
用户要的是各模型家自己的 logo。本机恰好装着 ChatGPT / ZCode /
DeepSeek Harness / Grok Bot / MiniMax Code，logo 就在它们的 bundle 里。

管道：
    App 资源(.png/.icns) → RGBA → 取形状 mask → 二值化 → 白底黑形 → 256×256

反色 logo 的处理（比如智谱是「黑底白 Z」）：
    mask = alpha ∧ (亮度 > 阈值)   —— 只留图形本身，丢掉底板
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops

OUT_DIR = Path(__file__).resolve().parent / "png"
# 64px 足够设备上 16~22px 显示；再大 base64 会把画板 payload 撑爆
# （官方限制：单个字符串 ≤ 4000 字符）
SIZE = 64

# key → (源文件, 说明, 取图模式)
#   "dark" = 取比底色暗的部分（白底黑标）
#   "light" = 取比底色亮的部分（黑底白标）
# 各家 App 图标底色深浅不一，逐个指定比自动猜可靠。
SOURCES = {
    # 第四项是底色灰度覆盖（None = 自动算众数）
    "chatgpt": ("/Applications/ChatGPT.app/Contents/Resources/icon-chatgpt.png", "OpenAI 六边形结", "dark", None),
    "zai": ("/Applications/ZCode.app/Contents/Resources/icon.png", "智谱 GLM 的 Z", "light", 0),
    "minimax": ("/Applications/MiniMax Code.app/Contents/Resources/icon.icns", "MiniMax", "dark", None),
    "deepseek": ("/Applications/DeepSeek Harness.app/Contents/Resources/icon.png", "DeepSeek 鲸鱼", "dark", None),
}

# Grok 图标特殊：icns 是「黑底 + 白色字形 + 黑色斜杠」三层，
# 底色众数法抠不出字形，改成「不透明 且 比黑底亮」= 字形本体，
# 黑色斜杠自然被排除 → 出来就是白底黑字、斜杠镂空。
GROK_SRC = "/Applications/Grok Bot.app/Contents/Resources/icon.icns"


def to_mono_grok(src: Path, size: int = SIZE) -> Image.Image:
    im = _load(src)
    alpha = im.getchannel("A")
    lum = im.convert("L")
    ap, lp = alpha.load(), lum.load()
    w, h = im.size
    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    for y in range(h):
        for x in range(w):
            if ap[x, y] >= 128 and lp[x, y] > 100:
                mp[x, y] = 255
    bbox = mask.getbbox()
    if not bbox:
        raise RuntimeError("没抠出图形")
    m = mask.crop(bbox)
    ratio = (size - 8) / max(m.size)
    new = (max(1, int(m.width * ratio)), max(1, int(m.height * ratio)))
    m = m.resize(new, Image.LANCZOS)
    canvas = Image.new("L", (size, size), 0)
    canvas.paste(m, ((size - new[0]) // 2, (size - new[1]) // 2))
    return _transparent(canvas, size)


def _load(path: Path) -> Image.Image:
    if path.suffix.lower() == ".icns":
        tmp = Path("/tmp/_logo_conv.png")
        subprocess.run(
            ["sips", "-s", "format", "png", str(path), "--out", str(tmp)],
            capture_output=True,
            check=False,
        )
        path = tmp
    return Image.open(path).convert("RGBA")


def to_mono(src: Path, size: int = SIZE, mode: str = "dark", bg_override: int | None = None) -> Image.Image:
    """把品牌图标转成「白底黑形」的方形 PNG。

    不猜底色是深是浅，而是**先算底色**：
    1. 取不透明区域的灰度众数 = 底色灰度
    2. 与底色差异大的像素 = logo 本身（白底黑标 / 黑底白标 / 白底蓝标都适用）
    3. 抠出形状 → 裁切 → 等比缩放居中 → 白底黑形
    """
    im = _load(src)
    alpha = im.getchannel("A")
    gray = im.convert("L")
    gp, ap = gray.load(), alpha.load()
    w, h = im.size

    # 底色 = 不透明像素的灰度众数
    hist: list[int] = [0] * 256
    for y in range(0, h, 3):
        for x in range(0, w, 3):
            if ap[x, y] > 200:
                hist[gp[x, y]] += 1
    bg = bg_override if bg_override is not None else max(range(256), key=lambda v: hist[v])

    # 图形 = 与底色差异大的不透明像素
    mask = Image.new("L", (w, h), 0)
    mp = mask.load()
    for y in range(h):
        for x in range(w):
            if ap[x, y] < 128:
                continue
            if mode == "light":
                if gp[x, y] > bg + 60:
                    mp[x, y] = 255
            else:
                if gp[x, y] < bg - 60:
                    mp[x, y] = 255

    bbox = mask.getbbox()
    if not bbox:
        raise RuntimeError("没抠出图形")
    mask = mask.crop(bbox)
    ratio = (size - 8) / max(mask.size)
    new = (max(1, int(mask.width * ratio)), max(1, int(mask.height * ratio)))
    mask = mask.resize(new, Image.LANCZOS)

    canvas = Image.new("L", (size, size), 0)
    canvas.paste(mask, ((size - new[0]) // 2, (size - new[1]) // 2))
    return _transparent(canvas, size)


def _transparent(mask: Image.Image, size: int) -> Image.Image:
    """mask → 透明底黑形 RGBA。

    不用 1bit 转换（那会把 alpha 压掉），
    64px 的 RGBA 也就 300~600 字节，base64 后依然远低于画板 4000 字符上限。
    """
    out = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    out.paste((0, 0, 0, 255), (0, 0), mask)
    # alpha 二值化：源图标边缘有大量半透明像素，会把 PNG 撑大 3~5 倍。
    # 墨水屏和菜单栏都是小尺寸显示，硬边完全够看。
    hard = out.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
    out.putalpha(hard)
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for key, (src, desc, mode, bg) in SOURCES.items():
        p = Path(src)
        if not p.exists():
            print(f"跳过 {key}: 找不到 {src}")
            continue
        try:
            im = to_mono(p, mode=mode, bg_override=bg)
        except Exception as e:  # noqa: BLE001
            print(f"跳过 {key}: {e}")
            continue
        out = OUT_DIR / f"{key}.png"
        im.save(out, optimize=True)
        print(f"{key}.png  ← {desc}  ({out.stat().st_size} bytes)")

    # Grok 单独处理
    gp = Path(GROK_SRC)
    if not gp.exists():
        print(f"跳过 grok: 找不到 {GROK_SRC}")
    else:
        try:
            im = to_mono_grok(gp)
            out = OUT_DIR / "grok.png"
            im.save(out, optimize=True)
            print(f"grok.png  ← xAI Grok 标志  ({out.stat().st_size} bytes)")
        except Exception as e:  # noqa: BLE001
            print(f"跳过 grok: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
