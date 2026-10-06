#!/bin/zsh
# 口播出稿 · 冻结后剪映原生草稿终结器
# 用法: finalize_jianying.sh <工作目录>
set -e
set -o pipefail

WORK="${1:?用法: finalize_jianying.sh <工作目录>}"
WORK="${WORK%/}"
PIPE="$(cd "$(dirname "$0")" && pwd)"
PYMLX="$HOME/.local/share/uv/tools/mlx-whisper/bin/python"
[[ -x "$PYMLX" ]] || PYMLX="$(command -v python3)"
if [[ -z "${CAPCUT_MATE_HOME:-}" ]]; then
  if [[ -x "$HOME/Developer/kbcg-xgz-capcut-mate/.venv/bin/python" ]]; then
    CAPCUT_MATE_HOME="$HOME/Developer/kbcg-xgz-capcut-mate"
  else
    CAPCUT_MATE_HOME="$HOME/Developer/capcut-mate"
  fi
fi
CAPCUT_PY="$CAPCUT_MATE_HOME/.venv/bin/python"
[[ -x "$CAPCUT_PY" ]] || {
  echo "⛔ 缺少 CapCutMate Python 环境: $CAPCUT_PY" >&2
  echo "   请让 Codex 运行本 Skill 安装器后重试。" >&2
  exit 1
}
DRAFT_ROOT=$(python3 "$PIPE/p4j_定位剪映草稿根.py" --save)
[[ -d "$DRAFT_ROOT" ]] || { echo "⛔ 找不到剪映草稿目录: $DRAFT_ROOT" >&2; exit 1; }
echo "✅ 当前剪映草稿根: $DRAFT_ROOT"
MANIFEST="$WORK/run_manifest_jianying.json"
MEDIA_DELIVERY="$WORK/media_delivery.json"
STRUCTURAL_REPORT="$WORK/jianying_structural_verification.json"
LOCK="$WORK/decision_lock.json"
AUDIO="$WORK/_vad16k.wav"
JY_DRAFT_PATH=""
MODE="ai"

typeset -a REQUIRED
if [[ -f "$WORK/human_rough_lock.json" ]]; then
  MODE="human"
  LOCK="$WORK/human_rough_lock.json"
  AUDIO=$(find "$WORK/sources" -type f -name '_vad16k.wav' -print -quit 2>/dev/null)
  [[ -n "$AUDIO" ]] || AUDIO="$WORK/sources/_vad16k.wav"
  REQUIRED=(
    "$WORK/config.json" "$WORK/word_track.json" "$WORK/rough_segments.json"
    "$WORK/cards.json" "$WORK/captions_plan.json" "$WORK/human_baseline.json"
    "$LOCK" "$AUDIO"
  )
else
  REQUIRED=(
    "$WORK/config.json" "$WORK/word_track.json" "$WORK/content_plan.json"
    "$WORK/keep.json" "$WORK/expression_review.json" "$WORK/structure.json" "$WORK/segments.json"
    "$WORK/rough_segments.json" "$WORK/cut_review.json" "$WORK/cards.json"
    "$LOCK" "$AUDIO"
  )
fi
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
source = c.get('original_path') or c.get('proxy')
if not source:
    sources = c.get('sources')
    if not isinstance(sources, list) or not sources:
        sys.exit('⛔ config 既没有 original_path/proxy，也没有 sources')
    source = sources[0].get('path')
vals = [c.get('name'), source, c.get('core_expression'), c.get('material_type')]
if any(not isinstance(x, str) or not x for x in vals):
    sys.exit('⛔ config 缺少 name/original_path/core_expression/material_type')
if any('\t' in x or '\n' in x for x in vals):
    sys.exit('⛔ config 路径/名称不得包含制表符或换行')
core = vals[2].strip()
if not re.search(r'[\u3400-\u9fff]', core) or re.fullmatch(r'[Cc]\d+', core, re.I):
    sys.exit('⛔ 中文核心表达必须包含中文且不能仅为技术编号')
if re.search(r'[\\/:\0\r\n\t]', core):
    sys.exit('⛔ 中文核心表达含路径分隔符或控制字符')
vals[2] = core
print('\t'.join(vals))
PY
)
IFS=$'\t' read -r NAME SOURCE CORE_EXPRESSION MATERIAL_TYPE <<< "$CFG_LINE"
[[ "$MATERIAL_TYPE" == "single_speaker" || "$MATERIAL_TYPE" == "single_subject_interview" ]] || {
  echo "⛔ material_type=$MATERIAL_TYPE；当前链不支持。" >&2; exit 1
}
typeset -a SPEAKER_INPUTS
if [[ "$MATERIAL_TYPE" == "single_subject_interview" ]]; then
  [[ -f "$WORK/speaker_turns.json" ]] || { echo "⛔ 缺 speaker_turns.json" >&2; exit 1; }
  SPEAKER_INPUTS=("$WORK/speaker_turns.json")
fi
[[ -f "$SOURCE" ]] || { echo "⛔ 真实素材不存在: $SOURCE" >&2; exit 1; }

CAPTIONS="$WORK/captions_plan.json"
typeset -a PACKAGING_INPUTS
[[ -f "$WORK/jianying_packaging.json" ]] && PACKAGING_INPUTS=("$WORK/jianying_packaging.json")
DELIVERY_DATE="$(date +%F)"
JY_DRAFT_STEM="${DELIVERY_DATE}_${CORE_EXPRESSION}_剪映草稿"
JY_DRAFT_PATH="$DRAFT_ROOT/$JY_DRAFT_STEM"
version=1
while [[ -e "$JY_DRAFT_PATH" ]]; do
  version=$((version + 1))
  JY_DRAFT_STEM="${DELIVERY_DATE}_${CORE_EXPRESSION}_剪映草稿_第${version}版"
  JY_DRAFT_PATH="$DRAFT_ROOT/$JY_DRAFT_STEM"
done
SOURCE_LINES=$(CFG="$WORK/config.json" python3 - <<'PY'
import json, os, sys
c = json.load(open(os.environ['CFG'], encoding='utf-8'))
sources = c.get('sources')
if isinstance(sources, list) and sources:
    paths = [row.get('path') for row in sources]
else:
    paths = [c.get('original_path') or c.get('proxy')]
if any(not isinstance(path, str) or not path or '\n' in path for path in paths):
    sys.exit('⛔ config 素材路径非法')
print('\n'.join(paths))
PY
)
typeset -a SOURCE_INPUTS
SOURCE_INPUTS=("${(@f)SOURCE_LINES}")
for source_input in "${SOURCE_INPUTS[@]}"; do
  [[ -f "$source_input" ]] || { echo "⛔ 真实素材不存在: $source_input" >&2; exit 1; }
done
typeset -a EXPECTED
if [[ "$MODE" == "human" ]]; then
  EXPECTED=(p4j_media_prepare p4j_draft_generate p4j_draft_verify p4j_draft_register)
else
  EXPECTED=(decision_lock p3 p1b p4j_media_prepare p4j_draft_generate p4j_draft_verify p4j_draft_register)
fi

python3 "$PIPE/pipeline_manifest.py" init "$MANIFEST" \
  --pipeline finalize_jianying.sh --work "$WORK" --name "$NAME" \
  --source "$SOURCE" --audio "$AUDIO" --lock "$LOCK" --code-dir "$PIPE" \
  --expected "${EXPECTED[@]}"

_cleanup() {
  local rc=$?
  if (( rc != 0 )); then
    python3 "$PIPE/pipeline_manifest.py" abort "$MANIFEST" "$rc" \
      --note "剪映 finalize 非零退出，未成功交付" >/dev/null 2>&1 || true
  fi
  return $rc
}
trap _cleanup EXIT

_run() {
  local step="$1"; shift
  local -a inputs outputs command
  while (( $# > 0 )) && [[ "$1" != "--outputs" ]]; do inputs+=("$1"); shift; done
  [[ "$1" == "--outputs" ]] || { echo "⛔ _run 缺 --outputs: $step" >&2; return 2; }
  shift
  while (( $# > 0 )) && [[ "$1" != "--" ]]; do outputs+=("$1"); shift; done
  [[ "$1" == "--" ]] || { echo "⛔ _run 缺命令分隔符: $step" >&2; return 2; }
  shift; command=("$@")
  python3 "$PIPE/pipeline_manifest.py" begin "$MANIFEST" "$step" "${inputs[@]}"
  if "${command[@]}"; then
    python3 "$PIPE/pipeline_manifest.py" end "$MANIFEST" "$step" "${outputs[@]}"
  else
    local rc=$?
    python3 "$PIPE/pipeline_manifest.py" fail "$MANIFEST" "$step" "$rc" \
      --note "步骤命令退出 $rc" || true
    return $rc
  fi
}

if [[ "$MODE" == "ai" ]]; then
  echo "=== [$NAME] 验证决策锁 ==="
  _run decision_lock "$WORK/config.json" "$LOCK" "$WORK/word_track.json" "$WORK/keep.json" \
    "$WORK/expression_review.json" \
    "${SPEAKER_INPUTS[@]}" "$WORK/segments.json" "$WORK/structure.json" \
    "$WORK/rough_segments.json" "$WORK/cards.json" \
    --outputs -- python3 "$PIPE/decision_contract.py" verify "$WORK"

  echo "=== [$NAME] 投影冻结字幕时间线 ==="
  _run p3 "$LOCK" "$WORK/structure.json" "$WORK/rough_segments.json" \
    "$WORK/word_track.json" "$WORK/cards.json" \
    --outputs "$CAPTIONS" -- "$PYMLX" "$PIPE/p3_分卡.py" "$WORK"

  echo "=== [$NAME] 帧级验收 ==="
  _run p1b "$LOCK" "$WORK/rough_segments.json" "$CAPTIONS" \
    "$WORK/word_track.json" "$WORK/keep.json" "$AUDIO" \
    --outputs -- "$PYMLX" "$PIPE/p1b_帧级验收.py" "$WORK" --captions
else
  echo "=== [$NAME] 人工粗剪锁将由剪映序列化器逐哈希验证 ==="
fi

echo "=== [$NAME] 准备剪映稳定交付素材 ==="
_run p4j_media_prepare "$WORK/config.json" "${SOURCE_INPUTS[@]}" \
  --outputs "$MEDIA_DELIVERY" -- \
  python3 "$PIPE/p4j_准备交付素材.py" "$WORK" --draft-root "$DRAFT_ROOT"

echo "=== [$NAME] 生成剪映真主轨草稿 ==="
_run p4j_draft_generate "$LOCK" "$WORK/config.json" "$WORK/rough_segments.json" \
  "$CAPTIONS" "$MEDIA_DELIVERY" "${PACKAGING_INPUTS[@]}" "${SOURCE_INPUTS[@]}" \
  --outputs "$JY_DRAFT_PATH/draft_info.json" "$JY_DRAFT_PATH/draft_content.json" \
  -- env CAPCUT_MATE_HOME="$CAPCUT_MATE_HOME" "$CAPCUT_PY" \
  "$PIPE/p4j_出剪映草稿.py" "$WORK" --draft-root "$DRAFT_ROOT" \
  --project-name "$JY_DRAFT_STEM"

echo "=== [$NAME] 剪映草稿终检 ==="
_run p4j_draft_verify "$JY_DRAFT_PATH/draft_info.json" "$JY_DRAFT_PATH/draft_content.json" \
  "$JY_DRAFT_PATH/draft_meta_info.json" "$WORK/config.json" "$WORK/rough_segments.json" "$MEDIA_DELIVERY" \
  --outputs "$STRUCTURAL_REPORT" -- python3 "$PIPE/p4j_验收草稿.py" "$JY_DRAFT_PATH" --work "$WORK" --report "$STRUCTURAL_REPORT"

echo "=== [$NAME] 登记剪映草稿箱 ==="
_run p4j_draft_register "$JY_DRAFT_PATH/draft_info.json" \
  "$JY_DRAFT_PATH/draft_content.json" "$JY_DRAFT_PATH/draft_meta_info.json" \
  --outputs "$DRAFT_ROOT/root_meta_info.json" -- \
  python3 "$PIPE/p4j_登记草稿箱.py" "$JY_DRAFT_PATH"

python3 "$PIPE/pipeline_manifest.py" finish "$MANIFEST" \
  --xml "$JY_DRAFT_PATH/draft_content.json" \
  --delivery "$JY_DRAFT_PATH/draft_content.json" \
  --delivery-state complete_structural

echo "=== [$NAME] 剪映草稿结构完成，待实机验收 ==="
echo "    ★ 剪映原生草稿: $JY_DRAFT_PATH"
echo "      清单: $MANIFEST（delivery_state=complete_structural, complete=false）"
echo "      下一步必须打开剪映，确认项目素材可见、时间线无“无访问权限”且播放器可出画面。"
