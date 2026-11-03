#!/bin/bash
# 安装 dot-usage：构建菜单栏 App + 注册 15 分钟定时采集。
# 用法： bash ~/Tools/dot-usage/install.sh
set -euo pipefail

TOOL_DIR="$HOME/Tools/dot-usage"
APP_DIR="$HOME/Applications"
APP_BUNDLE="$APP_DIR/DotUsageBar.app"
AGENTS="$HOME/Library/LaunchAgents"
STATE="$HOME/.local/state/dot-usage"

echo "== 1/4 准备目录 =="
mkdir -p "$APP_DIR" "$AGENTS" "$STATE" "$TOOL_DIR/preview"
chmod 700 "$STATE" 2>/dev/null || true

echo "== 2/5 安装 Python 包 =="
cd "$TOOL_DIR"
if python3 -m pip install -e . --quiet 2>/dev/null; then
  echo "  ✓ 已 pip install -e .（命令：dot-usage）"
else
  echo "  ! pip 安装失败，改用 PYTHONPATH 方式"
fi

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
echo "  ✓ $APP_BUNDLE"

echo "== 4/5 注册 LaunchAgent =="
# plist 里用 __HOME__ 占位，拷的时候换成真实家目录
for f in "$TOOL_DIR"/launchd/*.plist; do
  sed "s|__HOME__|$HOME|g" "$f" > "$AGENTS/$(basename "$f")"
done
launchctl bootout "gui/$(id -u)/com.mavis.dotusage.collect" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.mavis.dotusage.menubar" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$AGENTS/com.mavis.dotusage.collect.plist"
launchctl bootstrap "gui/$(id -u)" "$AGENTS/com.mavis.dotusage.menubar.plist"
launchctl enable "gui/$(id -u)/com.mavis.dotusage.collect" 2>/dev/null || true
launchctl enable "gui/$(id -u)/com.mavis.dotusage.menubar" 2>/dev/null || true
echo "  ✓ 定时采集：每 15 分钟"
echo "  ✓ 菜单栏 App：开机自启"

echo "== 5/5 首次采集 =="
cd "$TOOL_DIR"
PYTHONPATH="$TOOL_DIR/src" python3 -m dotusage.cli collect | tail -8

cat <<'MSG'

装好了。剩下两件事需要你自己动手：

1) 拿 DOT 的 API Key
   在 Dot. App → 更多 → API Key 里创建一个，然后放进钥匙串（不进对话、不进代码）：
       security add-generic-password -a "$USER" -s dot-usage -w 'dot_app_你的密钥'
   或者临时用环境变量： export DOT_API_KEY=dot_app_xxx

2) 在 Dot. App 的 Content Studio 里，给这台 Quote/0 加一个「Canvas API」内容项
   放进 loop 列表。卡片要有地方可写，Canvas API 才能落上去。

做完这两步，跑一次就能同时刷墨水屏和飞书：
       cd ~/Tools/dot-usage && python3 -m dot_usage.cli run

自检：  python3 -m dot_usage.cli doctor
预览：  python3 -m dot_usage.cli preview && open preview/card-preview.html
MSG
