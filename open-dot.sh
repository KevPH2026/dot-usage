#!/bin/bash
# 用 deep link 把 DOT App 直接送到内容工坊的某个内容项详情页。
#
# 背景：DOT 是 iOS App 跑在 macOS 上的封装，界面是 RN 的 WebView，
# 没有可被 AppleScript 点的元素树（辅助功能权限也没开），
# 所以「添加到设备」那一下必须你手动点。
# 但**导航**可以用 deep link 直接跳过去（已验证 App 日志会打出
# "Handle Studio detail: /studio/detail/<id>"）。
#
# 用法：
#   ./open-dot.sh image     # → 图像 API（消耗卡用这个）
#   ./open-dot.sh canvas    # → 画板 API
#   ./open-dot.sh both      # → 依次两个都开一遍
#   ./open-dot.sh studio    # → 内容工坊首页

set -u
open_item() {
    case "$1" in
        image)  open "dot:///studio/detail/mindreset.photo" ;;
        canvas) open "dot:///studio/detail/mindreset.canvas_api" ;;
        studio) open "dot:///studio/list" ;;
        *)      open "dot:///" ;;
    esac
}

case "${1:-studio}" in
    both)
        open_item image
        sleep 3
        open_item canvas
        ;;
    *)
        open_item "${1:-studio}"
        ;;
esac

echo "已发送 deep link。请在 DOT App 里点「添加到设备」完成绑定。"
echo "（消耗卡用「图像 API」，Tibo 屏用「文本 API」或「画板 API」）"
