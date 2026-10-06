#!/bin/zsh
# 统一交付入口。用户未明确要求 XML 时，默认交付剪映原生草稿。
set -e
WORK="${1:?用法: finalize.sh <工作目录> [final-cut|jianying|both]}"
TARGET="${2:-jianying}"
PIPE="$(cd "$(dirname "$0")" && pwd)"
case "$TARGET" in
  final-cut|fcp) zsh "$PIPE/finalize_fcp.sh" "$WORK" ;;
  jianying|剪映) zsh "$PIPE/finalize_jianying.sh" "$WORK" ;;
  both|两份)
    zsh "$PIPE/finalize_fcp.sh" "$WORK"
    zsh "$PIPE/finalize_jianying.sh" "$WORK"
    ;;
  *) echo "⛔ target 只能是 final-cut、jianying 或 both" >&2; exit 2 ;;
esac
