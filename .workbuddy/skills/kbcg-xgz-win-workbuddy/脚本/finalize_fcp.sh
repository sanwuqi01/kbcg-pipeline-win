#!/bin/zsh
# 口播出稿 · 冻结后终结器
# 用法: finalize_fcp.sh <工作目录>
#
# 本脚本的边界是硬契约：
#   1. 验证 decision_lock.json；
#   2. 只执行 p3 / p1b / p4f / p4f 终检与交付。
# 不得重跑 p1a / p1a2 / p1 / p2 / p1q；粗剪已冻结，终结器只做包装投影与验收。
set -e
set -o pipefail

WORK="${1:?用法: finalize_fcp.sh <工作目录>}"
WORK="${WORK%/}"
PIPE="$(cd "$(dirname "$0")" && pwd)"
PYMLX="$HOME/.local/share/uv/tools/mlx-whisper/bin/python"
[[ -x "$PYMLX" ]] || PYMLX="$(command -v python3)"
MANIFEST="$WORK/run_manifest.json"
LOCK="$WORK/decision_lock.json"
AUDIO="$WORK/_vad16k.wav"
DELIVERY_PATH=""

typeset -a REQUIRED
REQUIRED=(
  "$WORK/config.json"
  "$WORK/word_track.json"
  "$WORK/content_plan.json"
  "$WORK/keep.json"
  "$WORK/expression_review.json"
  "$WORK/structure.json"
  "$WORK/segments.json"
  "$WORK/rough_segments.json"
  "$WORK/cut_review.json"
  "$WORK/cards.json"
  "$LOCK"
  "$AUDIO"
)
for required_path in "${REQUIRED[@]}"; do
  [[ -f "$required_path" ]] || { echo "⛔ 缺少冻结后必需输入: $required_path" >&2; exit 1; }
done

# 返工轮若已声明问题所属工序，交付前必须证明没有串改上游。
if [[ -f "$WORK/stage_scope_guard.json" ]]; then
  python3 "$PIPE/stage_scope_guard.py" verify "$WORK"
fi

CFG_LINE=$(CFG="$WORK/config.json" python3 - <<'PY'
import json, os, re, sys
c = json.load(open(os.environ['CFG'], encoding='utf-8'))
vals = [c.get('name'), c.get('original_path') or c.get('proxy'),
        c.get('core_expression'), c.get('material_type')]
if any(not isinstance(x, str) or not x for x in vals):
    sys.exit('⛔ config 缺少 name/original_path/core_expression/material_type')
if any('\t' in x or '\n' in x for x in vals):
    sys.exit('⛔ config 路径/名称不得包含制表符或换行')
core = vals[2].strip()
if not re.search(r'[\u3400-\u9fff]', core):
    sys.exit('⛔ 中文核心表达必须包含中文，不能仅用 C2861/C2637 等技术编号')
if re.fullmatch(r'[Cc]\d+', core, re.I):
    sys.exit('⛔ 中文核心表达不能仅为技术编号')
if re.search(r'[\\/:\0\r\n\t]', core):
    sys.exit('⛔ 中文核心表达含路径分隔符或控制字符')
vals[2] = core
print('\t'.join(vals))
PY
)
IFS=$'\t' read -r NAME SOURCE CORE_EXPRESSION MATERIAL_TYPE <<< "$CFG_LINE"
[[ "$MATERIAL_TYPE" == "single_speaker" || "$MATERIAL_TYPE" == "single_subject_interview" ]] || {
  echo "⛔ material_type=$MATERIAL_TYPE；当前链只允许 single_speaker / single_subject_interview。multi_speaker/unknown 必须停止。" >&2
  exit 1
}
typeset -a SPEAKER_INPUTS
if [[ "$MATERIAL_TYPE" == "single_subject_interview" ]]; then
  [[ -f "$WORK/speaker_turns.json" ]] || {
    echo "⛔ single_subject_interview 缺少 speaker_turns.json" >&2
    exit 1
  }
  SPEAKER_INPUTS=("$WORK/speaker_turns.json")
fi
[[ -f "$SOURCE" ]] || { echo "⛔ config 指向的真实素材不存在: $SOURCE" >&2; exit 1; }

XMLOUT="$WORK/${NAME}_v7.fcpxml"
CAPTIONS="$WORK/captions_plan.json"
typeset -a PACKAGING_INPUTS
[[ -f "$WORK/final_cut_packaging.json" ]] && PACKAGING_INPUTS=("$WORK/final_cut_packaging.json")
DELIVER_DIR="${SOURCE:h}"
DELIVERY_DATE="$(date +%F)"
DELIVERY_STEM="${DELIVERY_DATE}_${CORE_EXPRESSION}"
DELIVERY_PATH="$DELIVER_DIR/${DELIVERY_STEM}.fcpxml"
version=2
while [[ -e "$DELIVERY_PATH" ]]; do
  DELIVERY_STEM="${DELIVERY_DATE}_${CORE_EXPRESSION}_第${version}版"
  DELIVERY_PATH="$DELIVER_DIR/${DELIVERY_STEM}.fcpxml"
  version=$((version + 1))
done
typeset -a EXPECTED
EXPECTED=(decision_lock p3 p1b p4f_generate p4f_verify delivery)

python3 "$PIPE/pipeline_manifest.py" init "$MANIFEST" \
  --work "$WORK" --name "$NAME" --source "$SOURCE" --audio "$AUDIO" \
  --lock "$LOCK" --code-dir "$PIPE" --expected "${EXPECTED[@]}"

_cleanup() {
  local rc=$?
  if (( rc != 0 )); then
    if [[ -n "$DELIVERY_PATH" && -f "$DELIVERY_PATH" ]]; then
      rm -f -- "$DELIVERY_PATH"
      echo "⚠ 本轮失败，已撤回本轮新建的交付副本: $DELIVERY_PATH" >&2
    fi
    python3 "$PIPE/pipeline_manifest.py" abort "$MANIFEST" "$rc" \
      --note "finalize 非零退出，未成功交付" >/dev/null 2>&1 || true
  fi
  return $rc
}
trap _cleanup EXIT

_run() {
  local step="$1"
  shift
  local -a inputs outputs command
  while (( $# > 0 )) && [[ "$1" != "--outputs" ]]; do inputs+=("$1"); shift; done
  [[ "$1" == "--outputs" ]] || { echo "⛔ _run 缺 --outputs: $step" >&2; return 2; }
  shift
  while (( $# > 0 )) && [[ "$1" != "--" ]]; do outputs+=("$1"); shift; done
  [[ "$1" == "--" ]] || { echo "⛔ _run 缺命令分隔符: $step" >&2; return 2; }
  shift
  command=("$@")

  # begin 在命令执行前对输入取 sha256；end 才对输出取哈希。
  python3 "$PIPE/pipeline_manifest.py" begin "$MANIFEST" "$step" "${inputs[@]}"
  if "${command[@]}"; then
    python3 "$PIPE/pipeline_manifest.py" end "$MANIFEST" "$step" "${outputs[@]}"
  else
    local rc=$?
    python3 "$PIPE/pipeline_manifest.py" fail "$MANIFEST" "$step" "$rc" \
      --note "步骤命令退出 $rc" || true
    echo "⛔ [$step] 失败（退出码 $rc），停止且不交付" >&2
    return $rc
  fi
}

echo "=== [$NAME] 验证决策锁 ==="
_run decision_lock \
  "$WORK/config.json" "$LOCK" "$WORK/word_track.json" "$WORK/keep.json" \
  "$WORK/expression_review.json" \
  "${SPEAKER_INPUTS[@]}" \
  "$WORK/segments.json" "$WORK/structure.json" "$WORK/rough_segments.json" "$WORK/cards.json" \
  --outputs -- python3 "$PIPE/decision_contract.py" verify "$WORK"

echo "=== [$NAME] p3 将已冻结分卡投影为字幕时间线 ==="
_run p3 \
  "$LOCK" "$WORK/structure.json" "$WORK/rough_segments.json" "$WORK/word_track.json" "$WORK/cards.json" \
  --outputs "$CAPTIONS" -- "$PYMLX" "$PIPE/p3_分卡.py" "$WORK"

echo "=== [$NAME] p1b 帧级验收 ==="
_run p1b \
  "$LOCK" "$WORK/rough_segments.json" "$CAPTIONS" "$WORK/word_track.json" "$WORK/keep.json" "$AUDIO" \
  --outputs -- "$PYMLX" "$PIPE/p1b_帧级验收.py" "$WORK" --captions

echo "=== [$NAME] p4f 生成 FCPXML ==="
_run p4f_generate \
  "$LOCK" "$WORK/config.json" "$WORK/rough_segments.json" "$CAPTIONS" \
  "${PACKAGING_INPUTS[@]}" "$SOURCE" \
  --outputs "$XMLOUT" -- python3 "$PIPE/p4f_出FCPXML.py" "$WORK" "$XMLOUT" \
  --project-name "$DELIVERY_STEM"

echo "=== [$NAME] p4f 终检 ==="
_run p4f_verify "$XMLOUT" "$SOURCE" \
  --outputs -- python3 "$PIPE/p4f_验收.py" "$XMLOUT" --work "$WORK"

echo "=== [$NAME] 交付副本 ==="
_run delivery "$XMLOUT" --outputs "$DELIVERY_PATH" -- cp -- "$XMLOUT" "$DELIVERY_PATH"

python3 "$PIPE/pipeline_manifest.py" finish "$MANIFEST" \
  --xml "$XMLOUT" --delivery "$DELIVERY_PATH"

# 字幕校对面板属于交付后的可选复核层；缺少面板时不影响已经通过验收的 FCPXML。
CAPTION_PANEL_HANDOFF="$HOME/Developer/xugongzi-caption-panel/Scripts/send-to-panel.sh"
if [[ -x "$CAPTION_PANEL_HANDOFF" ]]; then
  "$CAPTION_PANEL_HANDOFF" "$DELIVERY_PATH"
else
  echo "    提示: 未安装字幕校对面板，跳过面板交接"
fi

echo "=== [$NAME] 完成 ==="
echo "    ★ 交付: $DELIVERY_PATH"
echo "      中间产物: $XMLOUT"
echo "      清单: $MANIFEST（complete=true）"
