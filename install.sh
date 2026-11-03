#!/bin/bash
# 安装 dot-usage：构建菜单栏 App + 注册 15 分钟定时采集。
# 用法： bash <仓库路径>/install.sh
set -euo pipefail

# 仓库就在哪就装哪，不写死家目录。
TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/Applications"
APP_BUNDLE="$APP_DIR/DotUsageBar.app"
AGENTS="$HOME/Library/LaunchAgents"
STATE="$HOME/.local/state/dot-usage"
APP_CONFIG="$HOME/Library/Application Support/DotUsageBar/config.json"

# ── 候选 Python：必须是 3.11+ ─────────────────────────────────────────
# 数据源配置用标准库 tomllib 读，3.9/3.10 没有它。
# macOS 自带的 /usr/bin/python3 是 3.9，千万别用。
python_candidates() {
  for c in python3.14 python3.13 python3.12 python3.11 python3; do
    p=$(command -v "$c" 2>/dev/null) || continue
    if "$p" -c 'import sys,tomllib; raise SystemExit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then
      echo "$p"
    fi
  done
}

echo "== 1/5 选解释器 + 装包 =="
CANDIDATES="$(python_candidates)"
if [ -z "$CANDIDATES" ]; then
  echo "  ✗ 没找到 Python 3.11+ 的解释器。"
  echo "    dot-usage 读数据源配置要用标准库 tomllib（3.11 才进标准库）。"
  echo "    装一个： brew install python@3.12   然后重跑本脚本。"
  exit 1
fi

# 挨个试：能 pip install -e . 的优先（装上了才有 dot-usage 命令，
# 菜单栏 App 也才能直接 import dotusage）。
PYTHON=""
cd "$TOOL_DIR"
for p in $CANDIDATES; do
  if "$p" -m pip install -e . --quiet 2>/dev/null; then
    PYTHON="$p"; break
  fi
  if "$p" -m pip install -e . --user --quiet 2>/dev/null; then
    PYTHON="$p"; break
  fi
done

if [ -z "$PYTHON" ]; then
  # 装不上也不影响运行：定时任务和菜单栏 App 都走 PYTHONPATH 加载源码
  PYTHON="$(echo "$CANDIDATES" | head -1)"
  echo "  · pip 装不上（系统 Python 受 PEP 668 保护），改用源码方式跑"
  echo "  ✓ 解释器 ${PYTHON} ($("$PYTHON" --version 2>&1))"
else
  echo "  ✓ 已 pip install -e .（${PYTHON}，命令：dot-usage）"
fi

echo "== 2/5 准备目录 =="
mkdir -p "$APP_DIR" "$AGENTS" "$STATE" "$TOOL_DIR/preview"
chmod 700 "$STATE" 2>/dev/null || true

echo "== 3/5 编译菜单栏 App =="
cd "$TOOL_DIR/macos"
mkdir -p build
swiftc -O -parse-as-library -target arm64-apple-macos13.0 DotUsageBar.swift -o build/DotUsageBar

rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"
cp build/DotUsageBar "$APP_BUNDLE/Contents/MacOS/DotUsageBar"
cat > "$APP_BUNDLE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>DotUsageBar</string>
    <key>CFBundleDisplayName</key><string>AI 用量</string>
    <key>CFBundleIdentifier</key><string>com.mavis.dotusage.bar</string>
    <key>CFBundleExecutable</key><string>DotUsageBar</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0.0</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
echo "  ✓ ${APP_BUNDLE}"

echo "== 4/5 写安装配置 + 注册 LaunchAgent =="
# 菜单栏 App 是被 `open -g` 拉起来的，拿不到 LaunchAgent 的环境变量，
# 所以「用哪个 python / 源码在哪」必须落成一个文件给它读。
mkdir -p "$(dirname "$APP_CONFIG")"
cat > "$APP_CONFIG" <<EOF
{
  "python": "$PYTHON",
  "src_dir": "$TOOL_DIR/src"
}
EOF
echo "  ✓ ${APP_CONFIG}"

# plist 里用 __HOME__ / __PYTHON__ / __SRC_DIR__ 占位，拷的时候换成真实值。
# __PYTHON__ 必须替换：写死 /usr/bin/python3 会拿到 3.9，配置读不出来。
for f in "$TOOL_DIR"/launchd/*.plist; do
  sed -e "s|__HOME__|$HOME|g" \
      -e "s|__PYTHON__|$PYTHON|g" \
      -e "s|__SRC_DIR__|$TOOL_DIR/src|g" \
      "$f" > "$AGENTS/$(basename "$f")"
done
launchctl bootout "gui/$(id -u)/com.mavis.dotusage.collect" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.mavis.dotusage.menubar" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENTS/com.mavis.dotusage.collect.plist"
launchctl bootstrap "gui/$(id -u)" "$AGENTS/com.mavis.dotusage.menubar.plist"
launchctl enable "gui/$(id -u)/com.mavis.dotusage.collect" 2>/dev/null || true
launchctl enable "gui/$(id -u)/com.mavis.dotusage.menubar" 2>/dev/null || true
echo "  ✓ 定时采集：每 15 分钟（解释器 ${PYTHON}）"
echo "  ✓ 菜单栏 App：开机自启"

# 让新二进制生效：停掉正在跑的旧进程，launchd 会按新 plist 重新拉起。
# 不这么做的话 `open` 只会激活旧进程，新代码永远不生效。
pkill -f "$APP_BUNDLE/Contents/MacOS/DotUsageBar" 2>/dev/null || true
sleep 2

echo "== 5/5 首次采集 =="
cd "$TOOL_DIR"
PYTHONPATH="$TOOL_DIR/src" "$PYTHON" -m dotusage.cli collect | tail -6

cat <<'MSG'

装好了。剩下两件事需要你自己动手：

1) 拿 DOT 的 API Key
   在 Dot. App → 更多 → API Key 里创建一个，然后放进钥匙串（不进对话、不进代码）：
       security add-generic-password -a "$USER" -s dot-usage -w 'dot_app_你的密钥'
   或者临时用环境变量： export DOT_API_KEY=dot_app_xxx

2) 在 Dot. App 的 Content Studio 里，给这台 Quote/0 加一个「Canvas API」内容项
   放进 loop 列表。卡片要有地方可写，Canvas API 才能落上去。

做完这两步，跑一次就能同时刷墨水屏和本地状态：
       python3.12 -m dotusage.cli run

自检：  python3.12 -m dotusage.cli doctor
预览：  python3.12 -m dotusage.cli preview && open preview/card-preview.html
MSG
