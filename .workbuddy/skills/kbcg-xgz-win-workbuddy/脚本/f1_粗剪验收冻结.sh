#!/bin/zsh
# 内部粗剪冻结：表达逐项裁决 → 独立声学验收 → 决策一致性 → 冻结。
set -e
set -o pipefail

WORK="${1:?用法: f1_粗剪验收冻结.sh <工作目录> [--force]}"
WORK="${WORK%/}"
FORCE="${2:-}"
[[ -z "$FORCE" || "$FORCE" == "--force" ]] || {
  echo "⛔ 第二参数只能是 --force" >&2
  exit 2
}
PIPE="$(cd "$(dirname "$0")" && pwd)"
PYMLX="$HOME/.local/share/uv/tools/mlx-whisper/bin/python"
[[ -x "$PYMLX" ]] || PYMLX="$(command -v python3)"

python3 "$PIPE/p1c_生成表达审查.py" "$WORK" --check
"$PYMLX" "$PIPE/p1b_帧级验收.py" "$WORK"
"$PYMLX" "$PIPE/p1q_决策质量闸门.py" "$WORK"

typeset -a FREEZE_ARGS
[[ "$FORCE" == "--force" ]] && FREEZE_ARGS=(--force)
python3 "$PIPE/decision_contract.py" freeze "$WORK" "${FREEZE_ARGS[@]}"
python3 "$PIPE/decision_contract.py" verify "$WORK"
echo "✅ F1 内部粗剪验收与冻结完成；可直接进入字幕和包装"
