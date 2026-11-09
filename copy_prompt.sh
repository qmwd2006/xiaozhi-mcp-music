#!/usr/bin/env bash
# 一键复制小智角色提示词到剪贴板
#
# 用法：
#   ./copy_prompt.sh            # 自动选择方式
#   ./copy_prompt.sh --osc52    # 通过终端 OSC52 复制（SSH 远程推荐）
#   ./copy_prompt.sh --print    # 只打印，不复制
#
# 说明：提示词正文取自 xiaozhi_role_prompt_plain.txt（不含说明文字）。

set -euo pipefail
cd "$(dirname "$0")"

SRC="xiaozhi_role_prompt_plain.txt"
if [[ ! -f "$SRC" ]]; then
  echo "缺少 $SRC，请先运行生成脚本" >&2
  exit 1
fi

MODE="${1:-auto}"

if [[ "$MODE" == "--print" ]]; then
  cat "$SRC"
  exit 0
fi

osc52() {
  # OSC52: ESC ] 52 ; c ; <base64> BEL
  local b64
  b64=$(base64 -w0 < "$SRC")
  printf '\033]52;c;%s\a' "$b64" > /dev/tty
  echo "已通过终端 OSC52 发送复制请求（需终端支持，如 iTerm2 / Windows Terminal / kitty / WezTerm）"
}

if [[ "$MODE" == "--osc52" ]]; then
  osc52
  exit 0
fi

# auto：按环境依次尝试
if [[ -n "${WAYLAND_DISPLAY:-}" ]] && command -v wl-copy >/dev/null 2>&1; then
  wl-copy < "$SRC"
  echo "已复制到 Wayland 剪贴板"
elif [[ -n "${DISPLAY:-}" ]] && command -v xclip >/dev/null 2>&1; then
  xclip -selection clipboard < "$SRC"
  echo "已复制到 X11 剪贴板"
elif command -v pbcopy >/dev/null 2>&1; then
  pbcopy < "$SRC"
  echo "已复制到 macOS 剪贴板"
elif command -v clip.exe >/dev/null 2>&1; then
  clip.exe < "$SRC"
  echo "已复制到 Windows 剪贴板"
else
  echo "未检测到可用的剪贴板工具，改用终端 OSC52..." >&2
  osc52
fi
