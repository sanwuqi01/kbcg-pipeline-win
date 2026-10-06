#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qwen3-ForcedAligner 的 Windows / PyTorch 适配器（D2 产物）。

═══ 为什么有这个文件 ═══
macOS 侧 `脚本/p1a2_对齐词轴.py:312` 直接 `from mlx_qwen3_asr import ForcedAligner`。
`mlx-qwen3-asr` 是 Apple Silicon 专属（Metal GPU required，PyPI 上无 Windows 轮子），
Windows 无法安装。但 Qwen3-ForcedAligner-0.6B 有**官方 PyTorch/Transformers 实现**
（`Qwen/Qwen3-ForcedAligner-0.6B-hf`，随 transformers 5.x 发布），同样的权重、
非自回归单次前向。本文件把官方实现包成与 mlx 版**逐字段同签名**的适配器，
使 p1a2 只需换 import、不动任何对齐逻辑。

═══ 对外契约（与 mlx_qwen3_asr.ForcedAligner.align 一致，p1a2 依赖此签名）═══
    aws = aligner.align(seg, ' '.join(text), 'Chinese')
    for aw in aws:
        aw.text          # str，必须是**单个字符**（p1a2 会断言拒绝多字符单元）
        aw.start_time    # float，秒（相对 seg 起点的偏移）
        aw.end_time      # float，秒

- seg：np.ndarray，float32、16 kHz 单声道，值域 [-1, 1]（与 p1a2 的
  `AUDIO[int(lo*SR):int(hi*SR)]` 同型）。
- text：调用方已按字符加空格（`' '.join(text)`），强制逐字产生真实时间戳。
- 返回的时间是**相对 seg 起点**的偏移；调用方自己加 `lo`。绝不返回绝对时间。

═══ 为什么不能「多字符单元线性均摊」═══
p1a2 对多字符单元是硬断言失败（`禁止线性均摊`）——对齐器内部的字级边界必须真实存在。
Qwen 对 CJK 天然逐字 tokenize；调用方显式加空格后各语种字符都各自产生时间戳。
本适配器只做结构转换，**不做任何插值/均摊**。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DEFAULT_MODEL_ID = "Qwen/Qwen3-ForcedAligner-0.6B-hf"

# 判断"本地快照可用"必须存在的文件。缺任何一个都算不完整，宁可联网重取。
# 2026-09-14 实测：本机 HF 缓存里 snapshot 是**实体文件**（Windows 上
# symlink 缓存修过一次），1.83GB 的 model.safetensors 完整存在。
_REQUIRED_SNAPSHOT_FILES = (
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "processor_config.json",
)


def _hub_cache_root() -> Path:
    """与 huggingface_hub 同口径解析缓存根，不依赖其内部 API。"""
    for name in ("HUGGINGFACE_HUB_CACHE", "HF_HUB_CACHE"):
        override = os.environ.get(name)
        if override:
            return Path(override)
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def local_snapshot(model_id: str) -> str | None:
    """返回 HF 缓存里该模型的**完整**本地快照路径；没有就返回 None。

    为什么需要这个：`from_pretrained(model_id)` 即使权重已在本地，也会先向
    Hub 发请求做元数据/模板检查。本机走代理时该请求会 502，整条对齐直接崩，
    而模型其实完好——把"能不能跑"绑在了"当前网络通不通"上。这里改成
    离线优先：本地有完整快照就用本地路径，彻底不发网络请求。
    """
    repo = _hub_cache_root() / ("models--" + model_id.replace("/", "--"))
    snapshots = repo / "snapshots"
    if not snapshots.is_dir():
        return None
    for snap in sorted(
        (p for p in snapshots.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        if all(
            (snap / name).is_file() and (snap / name).stat().st_size > 0
            for name in _REQUIRED_SNAPSHOT_FILES
        ):
            return str(snap)
    return None


@dataclass
class AlignedUnit:
    """单个对齐单元。字段名与 mlx_qwen3_asr 返回对象保持一致。"""

    text: str
    start_time: float
    end_time: float

    def __repr__(self) -> str:  # 便于 spike 打印
        return f"AlignedUnit({self.text!r}, {self.start_time:.3f}, {self.end_time:.3f})"


def _field(item, name):
    """兼容 decode_forced_alignment 返回 dict 或 dataclass 两种形态。"""
    if isinstance(item, dict):
        return item[name]
    return getattr(item, name)


class Qwen3ForcedAligner:
    """官方 PyTorch Qwen3-ForcedAligner 的 mlx 同签名封装。

    模型按需惰性加载（首次 align 时），避免仅 import 就吃掉几 GB 内存。
    """

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        device: str | None = None,
        dtype=None,
    ) -> None:
        self.model_id = model_id
        self._device = device
        self._dtype = dtype
        self._processor = None
        self._model = None

    # ---- 惰性加载 ----
    def _ensure_loaded(self):
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForTokenClassification, AutoProcessor

        if self._device is None:
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
        if self._dtype is None:
            # CPU 上 float32 数值最忠实且最快；CUDA 上按**算力**选半精度：
            #   bfloat16 需 Ampere（SM 8.0）及以上才有原生支持。
            #   Turing/Volta（本机 GTX 1650 = SM 7.5）跑 bf16 会退化成模拟路径，
            #   又慢又可能失真 → 这些卡必须用 float16。
            if self._device == "cpu":
                self._dtype = torch.float32
            else:
                major, _minor = torch.cuda.get_device_capability(0)
                self._dtype = torch.bfloat16 if major >= 8 else torch.float16

        # 离线优先：本地有完整快照就不再向 Hub 发任何请求。
        source = local_snapshot(self.model_id)
        if source is None:
            if os.environ.get("HF_HUB_OFFLINE") == "1":
                raise RuntimeError(
                    f"HF_HUB_OFFLINE=1，但本地缓存里没有 {self.model_id} 的完整快照；"
                    "请先联网下载一次权重"
                )
            print(f"⚠ 本地没有 {self.model_id} 的完整快照，将从 HuggingFace 下载")
            source = self.model_id
        else:
            print(f"对齐器权重（本地快照，不联网）: {source}")

        self._processor = AutoProcessor.from_pretrained(source)
        self._model = AutoModelForTokenClassification.from_pretrained(
            source, dtype=self._dtype
        ).to(self._device)
        self._model.eval()

        sr = getattr(
            getattr(self._processor, "feature_extractor", None), "sampling_rate", None
        )
        self.sampling_rate = int(sr) if sr else 16000

    @property
    def device(self) -> str:
        self._ensure_loaded()
        return self._device

    # ---- 主入口：与 mlx 版同签名 ----
    def align(self, audio, text: str, language: str = "Chinese") -> list[AlignedUnit]:
        """对齐一段音频与文本，返回逐单元时间。

        audio：np.ndarray(float32, 采样率见 self.sampling_rate)，或文件路径。
        """
        self._ensure_loaded()
        import torch

        if isinstance(audio, np.ndarray):
            audio = np.ascontiguousarray(audio.astype(np.float32))
        inputs, word_lists = self._processor.prepare_forced_aligner_inputs(
            audio=audio, transcript=text, language=language
        )
        inputs = inputs.to(self._device, self._dtype)
        with torch.inference_mode():
            outputs = self._model(**inputs)
        decoded = self._processor.decode_forced_alignment(
            logits=outputs.logits,
            input_ids=inputs["input_ids"],
            word_lists=word_lists,
            timestamp_token_id=self._model.config.timestamp_token_id,
        )
        rows = decoded[0] if isinstance(decoded, (list, tuple)) else decoded
        return [
            AlignedUnit(
                text=str(_field(row, "text")),
                start_time=float(_field(row, "start_time")),
                end_time=float(_field(row, "end_time")),
            )
            for row in rows
        ]


if __name__ == "__main__":
    # 自检：仅验证模型能加载，不做对齐。
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    al = Qwen3ForcedAligner()
    al._ensure_loaded()
    print(f"model   : {al.model_id}")
    print(f"device  : {al.device}")
    print(f"dtype   : {al._dtype}")
    print(f"sr      : {al.sampling_rate}")
