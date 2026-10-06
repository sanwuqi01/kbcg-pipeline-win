#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D1 · Windows 转写适配器（faster-whisper 替代 mlx-whisper）。

═══ 为什么有这个脚本 ═══
原 Skill 的转写命令写死在 SKILL.md:113-117（Mac / MLX）：

    "$ASR_BIN" <视频路径> \
      --model mlx-community/whisper-large-v3-turbo \
      --language zh --condition-on-previous-text False \
      --word-timestamps True --output-format json --output-dir <工作目录>

Windows 上 MLX 不可用，改用 CTranslate2 版 faster-whisper 跑**同款权重**
（large-v3-turbo）。但「同款权重」不等于「同款输出」——下列参数必须逐个对齐，
否则 `p1a_建词轴.py` 的下游断言会以难查的方式失败：

| Mac 侧（mlx_whisper / openai-whisper 默认） | faster-whisper 默认 | 本脚本 |
|---|---|---|
| `condition_on_previous_text=False`（命令行显式传） | `True` | **False** ← 必须显式对齐 |
| `beam_size=None`（贪心） | `beam_size=5`（束搜索） | **1**（贪心）← 必须显式对齐 |
| 无 VAD 过滤 | `vad_filter=False` | **False**，一次不用 |
| `word_timestamps=True` | 同 | True |
| `language='zh'` | 自动检测 | **'zh'** |

**为什么不加 VAD 过滤**：整个 Skill 的架构靠 `p1a` 自己的 silero-vad 把真实停顿
暴露成 gap（见 `p1a:120-131` 的血泪注释）。若转写阶段先过滤掉静音，词时间轴会被
重排，`p1a` 就再也看不到那些停顿——这是架构级冲突，不是参数偏好。

**为什么不直接让 faster-whisper 自己下模型**：本机 HF 缓存已被证实存在
「`snapshots/<rev>/` 下文件退化成 0 字节空文件而 `blobs/` 完好」的 Windows 静默损坏
（见 `03_Windows迁移分析/02_实测记录.md` D2 坑 A）。因此本脚本用
`snapshot_download(local_dir=...)` 把模型**实体复制**到项目内目录，完全绕开
symlink 机制，并自带尺寸校验。

**CUDA 上的 Windows 坑**：CTranslate2 需要 `cublas64_12.dll`，但它不自带、本机也没装
CUDA Toolkit，而该 DLL 其实在 `torch/lib/` 里。故 CUDA 设备下必须先
`prepare_cuda_dll_path()` 预置搜索路径，否则报
`RuntimeError: Library cublas64_12.dll is not found or cannot be loaded`。

═══ 输出契约 ═══
`p1a_建词轴.py:156-164` 只读这两层：

    json['segments'][*]['words'][*] → {'word': str, 'start': float, 'end': float,
                                       'probability': float|可选}

故输出为 whisper-verbose-JSON 兼容结构，多带一个 `windows_port` 来源块。
文件名固定 `<媒体主名>_words.json`，**不含 'aligned'**（`p1a:67-70` 靠这个排除
p1a2 产出的对齐版；工作目录内必须且只能有一份）。
注意 `seek`/`tokens` 两字段 CTranslate2 不产出，故省略而非填假值。

用法：
    转写_faster_whisper.py <媒体路径> --outdir <工作目录>
    转写_faster_whisper.py 视频.mp4 --outdir w --hotwords "张三丰,某某医院,试管,卵泡"

hotwords 说明：空格或逗号分隔。IP 专名（人名/机构名）与领域术语的同音错字
（如「张三」被听成「章三」、「某某医院」被听成「某某一院」）优先靠它预防；
下游 核专名.py 只做事后候选，不替代热词前置。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# allow_patterns：只取 CT2 推理真正需要的文件（faster-whisper 同款清单）
_ALLOW = [
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
    "preprocessor_config.json",
]


def eprint(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


def resolve_repo(model_id: str) -> str:
    """把 'large-v3-turbo' 这类别名解析成 HF repo id；已是 repo id 则原样返回。

    用 faster-whisper 自己的映射表，避免我们自己维护一份会过期的别名表。
    """
    if "/" in model_id:
        return model_id
    try:
        from faster_whisper.utils import _MODELS  # type: ignore[attr-defined]
        repo = _MODELS.get(model_id)
        if repo:
            return repo
        eprint(f"⚠ faster-whisper 的模型表里没有别名 '{model_id}'，"
               f"可用别名：{sorted(_MODELS)}")
    except Exception as exc:  # noqa: BLE001
        eprint(f"⚠ 读不到 faster-whisper 的模型表（{type(exc).__name__}: {exc}）")
    return model_id


def fetch_model(model_id: str, model_root: Path, offline: bool) -> Path:
    """把模型实体复制到本地目录（不用 symlink），并校验文件尺寸非 0。"""
    from huggingface_hub import snapshot_download

    repo = resolve_repo(model_id)
    safe = repo.replace("/", "__")
    dest = model_root / safe

    if dest.is_dir() and (dest / "model.bin").is_file() and (dest / "model.bin").stat().st_size > 0:
        eprint(f"模型已就位：{dest}")
    else:
        eprint(f"拉取模型 {repo} → {dest}")
        snapshot_download(
            repo_id=repo,
            local_dir=str(dest),          # local_dir 走真实复制，不建 symlink
            allow_patterns=_ALLOW,
            local_files_only=offline,
        )

    # 尺寸校验：本机踩过「缓存存在但文件是 0 字节」的坑，这里必须显式验
    bad = [p.name for p in dest.rglob("*")
           if p.is_file() and p.stat().st_size == 0]
    if bad:
        raise SystemExit(
            f"⛔ 模型目录存在 0 字节文件：{bad}\n"
            f"   {dest}\n"
            "   删除该目录后重跑本脚本（不要相信「缓存已存在」）。"
        )
    missing = [n for n in ("config.json", "model.bin", "tokenizer.json")
               if not (dest / n).is_file()]
    if missing:
        raise SystemExit(f"⛔ 模型目录缺文件 {missing}：{dest}")
    return dest


def pick_device(requested: str) -> tuple[str, str]:
    """返回 (device, compute_type)。GPU 上 int8_float16 兼顾显存与精度。"""
    if requested != "auto":
        return requested, ("int8_float16" if requested == "cuda" else "int8")
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda", "int8_float16"
    except Exception:  # noqa: BLE001
        pass
    return "cpu", "int8"


def prepare_cuda_dll_path(device: str) -> None:
    """把 torch 自带的 CUDA 运行库加进 Windows 的 DLL 搜索路径。

    ═══ 为什么必须这么做（Windows 独有 · 实测踩到）═══
    CTranslate2（faster-whisper 的推理后端）在 CUDA 上需要 `cublas64_12.dll`/
    `cudnn64_9.dll`，但它**只自带 cudnn**（`ctranslate2/cudnn64_9.dll`），cublas 不带。
    而本机没有独立 CUDA Toolkit（`C:\\Program Files\\NVIDIA GPU Computing Toolkit` 不存在），
    `C:\\Windows\\System32` 也没有这些 DLL → 实测报：

        RuntimeError: Library cublas64_12.dll is not found or cannot be loaded

    这些 DLL **其实就在 torch 的 wheel 里**（`site-packages/torch/lib/`，
    实测含 cublas64_12 / cublasLt64_12 / cudnn64_9 / cudart64_12 等 37 个）。
    torch 自己加载时会处理路径，但 CTranslate2 不会 → 必须由调用方显式加进搜索路径。

    注意 Python 3.8+ 在 Windows 上**不再使用 PATH 查找已加载模块的依赖**，
    所以要同时做两件事：改 `PATH`（给 CTranslate2 自己的 LoadLibrary 用）
    与 `os.add_dll_directory`（给 Python 的扩展模块加载器用）。
    """
    if device != "cuda":
        return
    try:
        import importlib.util
        spec = importlib.util.find_spec("torch")
        if spec is None or not spec.origin:
            eprint("⚠ 找不到 torch，无法预置 CUDA DLL 路径")
            return
        torch_lib = Path(spec.origin).resolve().parent / "lib"
        if not torch_lib.is_dir():
            eprint(f"⚠ torch/lib 不存在：{torch_lib}")
            return
        os.environ["PATH"] = str(torch_lib) + os.pathsep + os.environ.get("PATH", "")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(torch_lib))
        eprint(f"CUDA DLL 路径已预置：{torch_lib}")
    except Exception as exc:  # noqa: BLE001
        eprint(f"⚠ 预置 CUDA DLL 路径失败（{type(exc).__name__}: {exc}），继续尝试")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("media", type=Path, help="媒体文件（视频或音频均可）")
    ap.add_argument("--outdir", type=Path, required=True, help="工作目录（输出 *_words.json）")
    ap.add_argument("--model", default="large-v3-turbo",
                    help="faster-whisper 别名或 HF repo id（默认 large-v3-turbo）")
    ap.add_argument("--model-root", type=Path,
                    default=Path(__file__).resolve().parent.parent / "模型" / "asr",
                    help="模型实体存放目录（默认 win-port/模型/asr）")
    ap.add_argument("--language", default="zh", help="默认 zh（对齐 SKILL.md:115）")
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    ap.add_argument("--beam-size", type=int, default=1,
                    help="1 = 贪心，对齐 mlx_whisper 的 beam_size=None 默认；改大即偏离 Mac 基线")
    ap.add_argument("--condition-on-previous-text", default="false",
                    choices=("true", "false"),
                    help="对齐 SKILL.md:115 的 --condition-on-previous-text False")
    ap.add_argument("--hotwords", default=None,
                    help="术语热词（空格或逗号分隔）。用于纠正科室术语（试管/卵泡/促排…）")
    ap.add_argument("--initial-prompt", default=None, help="initial_prompt（与 hotwords 二选一）")
    ap.add_argument("--offline", action="store_true", help="只用已下载的模型，不联网")
    ap.add_argument("--max-words", type=int, default=0,
                    help=">0 时只保留前 N 词（调试用）")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    media: Path = args.media
    if not media.is_file():
        raise SystemExit(f"⛔ 媒体不存在：{media}")
    args.outdir.mkdir(parents=True, exist_ok=True)

    # 文件名不许含 'aligned'，否则会被 p1a:69 当成 p1a2 的对齐版排除掉
    out_name = f"{media.stem}_words.json"
    if "aligned" in out_name:
        raise SystemExit(f"⛔ 输出名不能含 'aligned'：{out_name}")

    model_path = fetch_model(args.model, args.model_root, args.offline)
    device, compute_type = pick_device(args.device)
    prepare_cuda_dll_path(device)      # 必须在 import ctranslate2 之前（见函数文档）

    from faster_whisper import WhisperModel
    t0 = time.perf_counter()
    model = WhisperModel(str(model_path), device=device, compute_type=compute_type)
    t_load = time.perf_counter() - t0
    eprint(f"模型加载 {t_load:.1f}s · device={device} · compute_type={compute_type}")

    t1 = time.perf_counter()
    segments, info = model.transcribe(
        str(media),
        language=args.language,
        task="transcribe",
        beam_size=args.beam_size,
        condition_on_previous_text=(args.condition_on_previous_text == "true"),
        word_timestamps=True,
        vad_filter=False,          # 见文件头：架构级冲突，绝不加
        hotwords=args.hotwords,
        initial_prompt=args.initial_prompt,
    )

    out_segments, n_words = [], 0
    for seg in segments:                      # 生成器，边迭代边转写
        words = []
        for w in (seg.words or []):
            if not w.word.strip():
                continue
            words.append({
                "word": w.word,
                "start": round(float(w.start), 3),
                "end": round(float(w.end), 3),
                "probability": round(float(w.probability), 4),
            })
            if args.max_words and n_words + len(words) >= args.max_words:
                break
        n_words += len(words)
        out_segments.append({
            "id": len(out_segments),
            "start": round(float(seg.start), 3),
            "end": round(float(seg.end), 3),
            "text": seg.text,
            # avg_logprob / no_speech_prob / compression_ratio 供人工复核低置信识别
            "avg_logprob": round(float(seg.avg_logprob), 6) if seg.avg_logprob is not None else None,
            "no_speech_prob": round(float(seg.no_speech_prob), 6) if seg.no_speech_prob is not None else None,
            "compression_ratio": (round(float(seg.compression_ratio), 6)
                                  if getattr(seg, "compression_ratio", None) is not None else None),
            "words": words,
        })
        if args.max_words and n_words >= args.max_words:
            break
    t_asr = time.perf_counter() - t1

    payload = {
        "text": "".join(s["text"] for s in out_segments).strip(),
        "segments": out_segments,
        "language": info.language,
        "windows_port": {
            "backend": "faster-whisper",
            "model": args.model,
            "model_path": str(model_path),
            "device": device,
            "compute_type": compute_type,
            "beam_size": args.beam_size,
            "condition_on_previous_text": args.condition_on_previous_text == "true",
            "vad_filter": False,
            "hotwords": args.hotwords,
            "audio_seconds": round(float(info.duration), 3),
            "asr_seconds": round(t_asr, 2),
            "rtf": round(t_asr / max(1e-6, float(info.duration)), 4),
            "source": str(media.resolve()),
            "mirrors": "SKILL.md:113-117",
        },
    }
    out = args.outdir / out_name
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"音频   : {info.duration:.2f}s")
    print(f"段/词  : {len(out_segments)} 段 / {n_words} 词")
    print(f"耗时   : 加载 {t_load:.1f}s + 转写 {t_asr:.1f}s"
          f"（RTF {payload['windows_port']['rtf']:.3f}，{device}/{compute_type}）")
    print(f"输出   : {out}")
    if out_segments:
        print(f"首段   : [{out_segments[0]['start']:.2f}-{out_segments[0]['end']:.2f}]"
              f" {out_segments[0]['text'].strip()[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
