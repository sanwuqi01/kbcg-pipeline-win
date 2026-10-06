#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工序①a3 字缝吸附（字级时间精修 · 2026-09-14 接入）

用法：p1a3_字缝吸附.py <工作目录> [--dry] [--force]

═══ 为什么有这个模块 ═══
强制对齐器（Qwen3-ForcedAligner-0.6B；Windows 侧由 脚本_win/aligner_win.py 复刻）
返回的每个字 s/e 是**帧级推断值**，推断的是「这个字大致落在哪一段」，而不是
「声学字缝到底在哪个采样点」。两者在停顿处一致，在**连读处**会差几十到一百多毫秒。

2026-09-14 人工抽听暴露的实例（9-11.mp3 样本）：「第三步」的「三」被标成
24.880-25.200，而 24.880 处**仍在发声**（-19.4dB）—— 刀口切在前一个字「第」的内部，
用户从 24.880-25.200 这段里听到的是「第三」两个字。真实字缝在 24.945-25.010 的
65ms 能量谷里。旁证：「第」只分到 80ms（常速单字 150-250ms），后半段被「三」吃掉。

全片扫描（同一 26.8s 样本，112 条字缝）：47 条「该处有显著能量谷」，
其中 **33 条边界偏 55-190ms**（偏早 18 / 偏晚 15）。人耳可察觉门槛约 70-80ms
（「血」右缝偏 65ms 未察觉，「三」偏 80ms 即被听成两个字）。⇒ 系统性，不是孤例。

═══ 判据（踩过的坑，别退回去）═══
· **不能用「边界处能量高 = 错误」当判据。** 中文连读时字与字之间**本就没有谷**，
  按那个判据会得出 71%(80/112) 的骇人错误率。反证：用户听对的「卵」，两条缝都
  「穿在发声区」（落差仅 0.9dB）。正确判据 = 「该处有没有谷」×「边界有没有对上那个谷」。
· **字不是首尾相接的。** 本对齐器每个字有独立的声学跨度，字间允许留静音空隙
  （实测「胎」e=1.040 而「不」s=1.200，空 160ms）。所以必须**逐字独立吸附 s/e**；
  第一版把字缝当「左字终点 = 右字起点」的共享边界一起拉动，**把用户听对的「胎」改坏了**。
· **吸附点必须自身就安静。** 谷里中点是最好的刀口，但非对称谷的中点可能已经爬回
  上升沿（实测「基」的候选点只有 1.0dB —— 那不是静音，是还在发声）。所以谷宽判定
  之外再加一道：吸附点**自身**要比两侧 60ms 峰值低 ≥10dB，否则不吸。
· **两趟做，不要一趟。** 起点受「前字已吸附终点」约束、终点受「后字已吸附起点」约束，
  挤在一趟里从左到右做，终点只能用后字【原】起点当上界 —— 余量被压死，
  实测终点吸附率不到起点的一成。两趟之后两端修复率才相当。

═══ 契约（下游全链依赖，改前必读）═══
· 字的文本、下标、顺序【绝对不变】——本脚本只改时间字段。`segments.json` 会把
  `char_times` 整份抄走，`p3_分卡.py` 与 `decision_contract.py` 都逐字比对
  `word_track` 的 `char_times`；文本或下标错位 = 全毁。
· 纯后处理，**不重跑对齐模型**：只把已有的 s/e 挪到声学谷上。
· 位移硬约束：`|Δ| ≤ MAX_MOVE(0.20s)`；`|Δ| ≤ MIN_MOVE(0.010s)` 视为抖动，不动。
· 不重叠且单调：任一字的 `s ≥ 前字(已吸附)e + GUARD`，`e ≤ 后字(原值)s - GUARD`。
· 吸附后按 p1a2 同一套规则**重推词级时间**：`ws = 首字 s`、`we = 末字 e`，
  再过一遍 VAD 主块钳制，最后把字钳回 `[ws, we]`。`vs/ve` 必须同步 = `ws/we`
  —— p1 粗定位 / `segments` 的 words s,e / p3 分卡 / p1b A0 读的都是 `vs/ve`。
· `blk` 按新词时间与 `voice_blocks` 最大重叠重判；`orphan/weak` 保持原值。
· 吸附前的词轴留一份 `word_track.吸附前备份.json`（只写一次，不覆盖），可回退。

═══ 断言（任一违反即报错退出，不写文件）═══
· 逐字文本与吸附前逐字相同（下标、顺序、字符全等）
· 全局字流单调不减且不重叠（容差 5ms）
· 词时间仍含住自己的字；词间不重叠（容差 5ms）——与 p1a2 同口径
· 任何位移都不超过 MAX_MOVE

═══ 幂等 ═══
带 `char_snap` 标记的词轴再跑会直接跳过（吸附后的时间已经坐在谷上，再吸附不会动），
要重跑加 `--force`。`--dry` 只算不写，用来先看会动多少。
"""
import hashlib
import json
import os
import shutil
import sys
import time
import wave

import numpy as np

# 这个脚本可能被人工直调（「补跑字缝吸附」），不能指望父进程已经设好 PYTHONUTF8。
# GBK 控制台会把上面那些中文/符号输出崩在半路。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

SR = 16000
REACH = 0.20          # 吸附搜索半径（前后各看这么多秒）
MAX_MOVE = 0.20       # 单侧最大允许位移
MIN_MOVE = 0.010      # 小于此值视为抖动，不动
CONTIG_TOL = 0.010    # `e_i` 与 `s_{i+1}` 相差小于此值 ⇒ 视为同一个字缝，一起吸附
GUARD = 0.005         # 与邻字之间保底的时间间隙（与 p1a2 重叠容差同口径）
MIN_CHAR = 0.020      # 吸附后任一个字不得短于此值（防把字压成退化时长）
EWIN = 0.010          # 能量窗
EHOP = 0.005          # 能量步
MIN_DROP = 12.0       # 谷深门槛（dB）
MIN_DUR = 0.030       # 谷宽门槛（s）
FLANK = 0.060         # 判定谷时两侧取 max 的窗宽
SNAP_QUIET_DB = 10.0  # 吸附点【自身】必须比两侧低这么多 dB（非对称谷的中点可能已爬回上升沿）
OFF_TOL = 0.050       # 统计「有谷却对不上」的门槛
SCHEMA = "char-snap@1"
BACKUP = "word_track.吸附前备份.json"
REPORT = "char_snap_report.json"

argv = list(sys.argv[1:])
DRY = "--dry" in argv
FORCE = "--force" in argv
_pos = [a for a in argv if not a.startswith("--")]
if len(_pos) != 1:
    sys.exit("用法: p1a3_字缝吸附.py <工作目录> [--dry] [--force]")
WORK = _pos[0].rstrip("/\\")

track_path = f"{WORK}/word_track.json"
apath = f"{WORK}/_vad16k.wav"
for need in (track_path, apath):
    if not os.path.isfile(need):
        sys.exit(f"⛔ p1a3: 缺 {need}；必须先跑 p1a 与 p1a2")

track = json.load(open(track_path, encoding="utf-8"))
if track.get("char_snap") and not FORCE:
    print(f"已吸附（{track['char_snap']}），跳过。要重跑加 --force。")
    sys.exit(0)
if not track.get("aligner") or track.get("char_timing") is None:
    sys.exit("⛔ p1a3: 词轴还没有对齐时间（缺 aligner/char_timing）；先跑 p1a2")

words = track["words"]
VB = track.get("voice_blocks") or []


# ── 音频：只认 p1a 与素材指纹绑定的 _vad16k.wav（与 p1a2 同一套校验）───────────
def _sha256(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        left = limit
        while True:
            size = 1 << 20 if left is None else min(1 << 20, left)
            if size <= 0:
                break
            b = f.read(size)
            if not b:
                break
            h.update(b)
            if left is not None:
                left -= len(b)
    return h.hexdigest()


meta_path = f"{WORK}/_vad16k.meta.json"
if not os.path.isfile(meta_path):
    sys.exit("⛔ p1a3: 缺 _vad16k.meta.json 指纹侧车；必须先重跑 p1a")
meta = json.load(open(meta_path, encoding="utf-8"))
cache = track.get("audio_cache") or {}
wav_hash = _sha256(apath)
if (meta.get("wav_sha256") not in (None, wav_hash)
        or cache.get("wav_sha256") not in (None, wav_hash)):
    sys.exit("⛔ p1a3: _vad16k.wav 内容与词轴/侧车指纹不一致；拒绝用陈旧音频吸附")

with wave.open(apath, "rb") as wv:
    assert wv.getframerate() == SR and wv.getnchannels() == 1, \
        f"{apath} 不是 16k 单声道（{wv.getframerate()}Hz/{wv.getnchannels()}ch）"
    PCM = np.frombuffer(wv.readframes(wv.getnframes()), dtype=np.int16).astype(np.float32)
PCM_F = PCM / 32768.0


# ── 能量包络（10ms 窗 / 5ms 步）───────────────────────────────────────────────
_win, _hop = int(EWIN * SR), int(EHOP * SR)
if len(PCM_F) <= _win:
    sys.exit("⛔ p1a3: 音频太短，无法做能量分析")
_n = (len(PCM_F) - _win) // _hop + 1
_frames = np.lib.stride_tricks.as_strided(
    PCM_F, shape=(_n, _win), strides=(PCM_F.strides[0] * _hop, PCM_F.strides[0])
)
DB = 20 * np.log10(np.maximum(np.sqrt((_frames ** 2).mean(axis=1)), 1e-6))
T = np.arange(_n) * _hop / SR


def _fi(sec):
    """秒 → 能量帧下标（钳进有效范围）"""
    return min(max(0, int(round(sec * SR / _hop))), len(DB) - 1)


def valleys(a, b):
    """[a, b) 帧区间内的能量谷（返回**谷区中心**的时间，秒）。

    一个点算谷，要同时满足：局部极小、两侧 FLANK 窗内的最大值都比谷底高 MIN_DROP
    以上、且「明显低于两侧」的连续段不短于 MIN_DUR。中文连读区找不到这样的点，
    就自然没有候选 —— 这是「该处有没有谷」的判据本体。

    谷区必须**双向**扩：只从谷底往后走，遇到「谷底在谷区末端」的非对称谷
    （实测 69.215-69.265s 就是这样，谷底在尾巴上）会算出 5ms 宽而误判为不是谷。
    取谷区中心是「离两边都最远」的刀口，比取谷底稳（谷底可能紧贴某一侧起音）。
    """
    out, k = [], a + 1
    edge = int(FLANK * SR / _hop)
    while k < b - 1:
        if DB[k] <= DB[k - 1] and DB[k] <= DB[k + 1]:
            L = DB[max(a, k - edge):k + 1].max()
            R = DB[k:min(b, k + edge) + 1].max()
            top = min(L, R)
            if top - DB[k] >= MIN_DROP:
                thr = top - MIN_DROP * 0.6
                i0 = k
                while i0 > a and DB[i0 - 1] < thr:
                    i0 -= 1
                j = k
                while j < b - 1 and DB[j + 1] < thr:
                    j += 1
                if (j - i0) * _hop / SR >= MIN_DUR:
                    out.append(float((T[i0] + T[j]) / 2))
                    k = j + 1
                    continue
        k += 1
    return out


def off_of(tb):
    """边界 tb 附近有谷的话，返回 (tb - 最近的谷) 的毫秒偏移；没谷返回 None。"""
    v = valleys(_fi(tb - REACH - 0.02), _fi(tb + REACH + 0.02))
    if not v:
        return None
    return (tb - min(v, key=lambda x: abs(x - tb))) * 1000.0


def drop_here(sec):
    """**这个点自己**比两侧 FLANK 窗内峰值低多少 dB。

    这是吸附位置的合格判据：谷是中点没错，但非对称谷的中点可能已经爬回上升沿
    （实测「基」s+117ms 的候选点只有 1.0dB —— 那不是静音，是还在发声）。
    只按「谷宽 ≥30ms」选点会放进这种候选，所以必须逐点复核。
    """
    k = _fi(sec)
    edge = int(FLANK * SR / _hop)
    left = DB[max(0, k - edge):k + 1].max()
    right = DB[k:min(len(DB), k + edge + 1)].max()
    return round(float(min(left, right) - DB[k]), 1)


def pick(cur, lo, hi):
    """在 [lo, hi] 内挑离 cur 最近的、且**自身确实安静**的能量谷；没有就返回 None。"""
    if hi <= lo:
        return None
    cands = [v for v in valleys(_fi(cur - REACH), _fi(cur + REACH))
             if lo <= v <= hi and drop_here(v) >= SNAP_QUIET_DB]
    if not cands:
        return None
    cand = min(cands, key=lambda x: abs(x - cur))
    if MIN_MOVE < abs(cand - cur) <= MAX_MOVE:
        return cand
    return None


# ── 展平成全局字流 ────────────────────────────────────────────────────────────
flat = []
for wi, w in enumerate(words):
    for ci, row in enumerate(w.get("char_times") or []):
        flat.append({"wi": wi, "ci": ci, "c": row["c"],
                     "s": float(row["s"]), "e": float(row["e"])})
if not flat:
    sys.exit("⛔ p1a3: 词轴里没有任何字级时间（char_times 全空）")
orig = [dict(x) for x in flat]

# 吸附前：多少条缝「有谷却对不上」
before_s = sum(1 for k in range(1, len(flat))
               if (o := off_of(flat[k]["s"])) is not None and abs(o) > OFF_TOL * 1000)
before_e = sum(1 for k in range(0, len(flat) - 1)
               if (o := off_of(flat[k]["e"])) is not None and abs(o) > OFF_TOL * 1000)


# ── 逐字独立吸附 ──────────────────────────────────────────────────────────────
# 结构（踩过两轮坑才定下来，别简化）：
#   ① 相邻字是【同一个字缝】时（`e_i` 与 `s_{i+1}` 同点），两边必须**一起落**到
#      同一个谷上。先只做起点、把终点留给下一轮，会出现「后字起点已挪到谷上、
#      前字终点还留在原位」——去重之后终点吸附率不到起点的一成（实测 31 vs 1）。
#   ② 字间真有静音空隙时（对齐器的字不是首尾相接），两边**各自独立**吸附。
# 所以：先扫一遍共享边界（成对落点），再对「没被共享边界定过」的孤立起点/终点
# 各补一趟。
moves = []
n_s = n_e = 0
s_done: set[int] = set()
e_done: set[int] = set()


def _record(i, field, old, new, drop):
    moves.append({"i": i, "word": flat[i]["wi"], "c": flat[i]["c"], "field": field,
                  "from": round(old, 3), "to": round(new, 3),
                  "delta_ms": round((new - old) * 1000), "drop_db": drop})


# —— ① 共享边界：一个字缝，两边一起落 ——
for i in range(len(flat) - 1):
    a, b = flat[i], flat[i + 1]
    if abs(a["e"] - b["s"]) > CONTIG_TOL:
        continue                                   # 有真实空隙，交给下面的独立趟
    cand = pick(a["e"], max(a["s"] + MIN_CHAR, a["e"] - MAX_MOVE),
                min(b["e"] - MIN_CHAR, a["e"] + MAX_MOVE))
    if cand is None:
        s_done.add(i + 1); e_done.add(i)
        continue
    d = drop_here(cand)
    if abs(cand - a["e"]) > MIN_MOVE:
        _record(i, "e", a["e"], cand, d); a["e"] = cand; n_e += 1
    if abs(cand - b["s"]) > MIN_MOVE:
        _record(i + 1, "s", b["s"], cand, d); b["s"] = cand; n_s += 1
    s_done.add(i + 1); e_done.add(i)

# —— ② 孤立起点（含全片首字）：前一对不是共享边界 ——
for i, u in enumerate(flat):
    if i in s_done:
        continue
    lo = flat[i - 1]["e"] + GUARD if i > 0 else 0.0
    cand = pick(u["s"], max(lo, u["s"] - MAX_MOVE), u["e"] - MIN_CHAR)
    if cand is not None:
        _record(i, "s", u["s"], cand, drop_here(cand)); u["s"] = cand; n_s += 1
    s_done.add(i)

# —— ③ 孤立终点（含全片末字）：后一对不是共享边界 ——
for i, u in enumerate(flat):
    if i in e_done:
        continue
    hi = flat[i + 1]["s"] - GUARD if i < len(flat) - 1 else u["e"] + REACH
    cand = pick(u["e"], u["s"] + MIN_CHAR, min(hi, u["e"] + MAX_MOVE))
    if cand is not None:
        _record(i, "e", u["e"], cand, drop_here(cand)); u["e"] = cand; n_e += 1
    if u["e"] < u["s"]:
        u["e"] = u["s"]
    e_done.add(i)

# 吸附后同一统计
after_s = sum(1 for k in range(1, len(flat))
              if (o := off_of(flat[k]["s"])) is not None and abs(o) > OFF_TOL * 1000)
after_e = sum(1 for k in range(0, len(flat) - 1)
              if (o := off_of(flat[k]["e"])) is not None and abs(o) > OFF_TOL * 1000)
max_move = max((abs(m["delta_ms"]) for m in moves), default=0) / 1000.0


# ── 断言①：字一级没被改动（文本 / 下标 / 顺序）───────────────────────────────
for a, b in zip(orig, flat):
    assert a["c"] == b["c"] and a["wi"] == b["wi"] and a["ci"] == b["ci"], \
        "p1a3: 字的文本/下标/顺序被改动——违反契约，不写文件"
assert max_move <= MAX_MOVE + 1e-9, f"p1a3: 位移 {max_move:.3f}s 超过上限 {MAX_MOVE}s"
_degen = [x["c"] for x, o in zip(flat, orig)
          if x["e"] - x["s"] < MIN_CHAR - 1e-9 and o["e"] - o["s"] >= MIN_CHAR - 1e-9]
assert not _degen, f"p1a3: 吸附把字压成退化时长（<{MIN_CHAR*1000:.0f}ms）：{_degen[:8]}"

# ── 重推词级时间：ws=首字s / we=末字e → VAD 主块钳制 → 字钳回 [ws,we] ─────────
by_word = {}
for x in flat:
    by_word.setdefault(x["wi"], []).append(x)

n_ws = n_we = n_blk = 0
for wi, w in enumerate(words):
    rows = by_word.get(wi)
    if not rows:
        continue                       # drop / 空字词：p1a2 的时间原样保留
    ws_new = round(rows[0]["s"], 3)
    we_new = round(rows[-1]["e"], 3)
    # 主块钳制（与 p1a2 同规则、同口径）
    if we_new > ws_new and VB:
        best, bx, by = 0.0, None, None
        for x, y in VB:
            ov = min(y, we_new) - max(x, ws_new)
            if ov > best:
                best, bx, by = ov, x, y
        if bx is not None:
            if ws_new < bx - 1e-6:
                ws_new = round(bx, 3)
            if we_new > by + 1e-6:
                we_new = round(by, 3)
    if we_new < ws_new:
        we_new = ws_new
    if abs(ws_new - w["ws"]) > 1e-9:
        n_ws += 1
    if abs(we_new - w["we"]) > 1e-9:
        n_we += 1
    w["ws"], w["we"] = ws_new, we_new
    w["vs"], w["ve"] = ws_new, we_new
    # 字钳回词边界（与 p1a2 收尾同一套）
    prev = ws_new
    for row in rows:
        row["s"] = round(min(max(row["s"], prev, ws_new), we_new), 3)
        row["e"] = round(min(max(row["e"], row["s"]), we_new), 3)
        prev = row["e"]
    # blk 重判
    if VB:
        def _blk(s_, e_):
            best, bi = 0.0, -1
            for k, (x, y) in enumerate(VB):
                ov = min(y, e_) - max(x, s_)
                if ov > best:
                    best, bi = ov, k
            return bi
        nb = _blk(ws_new, we_new) if we_new > ws_new else _blk(ws_new - 0.02, ws_new + 0.02)
        if nb != w.get("blk", -1):
            w["blk"] = nb
            n_blk += 1

# ── 写回字级时间 ──────────────────────────────────────────────────────────────
for wi, w in enumerate(words):
    rows = by_word.get(wi)
    if not rows:
        continue
    w["char_times"] = [{"c": r["c"], "s": r["s"], "e": r["e"]} for r in rows]

# ── 断言②：字流与词时间的单调 / 不重叠 / 包含关系 ─────────────────────────────
for a, b in zip(flat, flat[1:]):
    assert b["s"] >= a["e"] - GUARD - 1e-6, \
        f"p1a3: 字流重叠 字#{a['wi']}.{a['ci']}「{a['c']}」{a['e']} → " \
        f"字#{b['wi']}.{b['ci']}「{b['c']}」{b['s']}"
for i, w in enumerate(words):
    rows = [r for r in (w.get("char_times") or [])]
    if not rows:
        continue
    assert w["ws"] <= rows[0]["s"] + 1e-9 and rows[-1]["e"] <= w["we"] + 1e-9, \
        f"p1a3: 词{i}「{w['t']}」的时间没有含住自己的字"
    assert w["we"] >= w["ws"], f"p1a3: 词{i}「{w['t']}」we<ws"
_aligned = [i for i, w in enumerate(words)
            if not w.get("alignment_skipped") and (w.get("align_text") or "")]
for a, b in zip(_aligned, _aligned[1:]):
    assert words[b]["ws"] >= words[a]["ws"] - 1e-6, \
        f"p1a3: 词时间非单调 词{a}「{words[a]['t']}」→ 词{b}「{words[b]['t']}」"
    assert words[b]["ws"] >= words[a]["we"] - GUARD - 1e-6, \
        f"p1a3: 相邻词重叠 词{a}「{words[a]['t']}」{words[a]['we']} → " \
        f"词{b}「{words[b]['t']}」{words[b]['ws']}"

# ── 词轴外残声候选按新词时间重算（与 p1a2 同一套判定）────────────────────────
resid = []
for k, (x, y) in enumerate(VB):
    first = next((i for i in range(len(words))
                  if words[i].get("blk") == k and (words[i].get("align_text") or "")), None)
    if first is not None and words[first]["ws"] - x >= 0.12:
        resid.append({
            "residual_id": f"R{k:04d}",
            "block_index": k,
            "start": round(float(x), 6),
            "end": round(float(words[first]["ws"]), 6),
            "duration": round(float(words[first]["ws"] - x), 6),
            "next_word_index": first,
            "next_word_text": words[first]["t"],
        })
track["residual_candidates"] = resid

report = {
    "schema": SCHEMA,
    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "audio": {"path": os.path.basename(apath), "sha256_16": wav_hash[:16]},
    "params": {"reach": REACH, "max_move": MAX_MOVE, "min_move": MIN_MOVE,
               "guard": GUARD, "min_drop_db": MIN_DROP, "min_dur": MIN_DUR},
    "chars_total": len(flat),
    "moved_start": n_s, "moved_end": n_e, "max_move_ms": round(max_move * 1000),
    "valley_misaligned": {
        "start": {"before": before_s, "after": after_s},
        "end": {"before": before_e, "after": after_e},
    },
    "words": {"ws_changed": n_ws, "we_changed": n_we, "blk_reassigned": n_blk},
    "residual_candidates": len(resid),
    "moves": sorted(moves, key=lambda m: -abs(m["delta_ms"])),
}

print("字缝吸附（对齐器粗定位 → 能量谷精修，不重跑模型）")
print(f"  字数 {len(flat)} · 起点吸附 {n_s} 个 · 终点吸附 {n_e} 个 · 最大位移 {max_move*1000:.0f}ms")
print(f"  「有谷却对不上」 起点 {before_s} → {after_s} · 终点 {before_e} → {after_e}"
      f"（判定门槛 ±{OFF_TOL*1000:.0f}ms，谷深 ≥{MIN_DROP:.0f}dB / 谷宽 ≥{MIN_DUR*1000:.0f}ms）")
print(f"  词级：ws 变 {n_ws} · we 变 {n_we} · blk 重判 {n_blk} · 词轴外残声候选 {len(resid)}")

if DRY:
    print()
    print("（--dry：没有写任何文件）位移最大的 12 处：")
    for m in report["moves"][:12]:
        print(f"    字#{m['i']:>4} 词#{m['word']:>4}「{m['c']}」{m['field']} "
              f"{m['from']:.3f} → {m['to']:.3f}  ({m['delta_ms']:+}ms, 谷深 {m['drop_db']}dB)")
    sys.exit(0)

report_path = f"{WORK}/{REPORT}"
bak_path = f"{WORK}/{BACKUP}"
if not os.path.isfile(bak_path):
    shutil.copy(track_path, bak_path)
track["char_snap"] = SCHEMA
track["char_snap_stats"] = {k: report[k] for k in
                            ("chars_total", "moved_start", "moved_end", "max_move_ms",
                             "valley_misaligned", "words")}
json.dump(track, open(track_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
json.dump(report, open(report_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"  已写 {track_path}（吸附前存档 {os.path.basename(bak_path)}）")
print(f"  审计报告 {report_path}")
for m in report["moves"][:6]:
    print(f"     字#{m['i']:>4}「{m['c']}」{m['field']} {m['from']:.3f} → {m['to']:.3f} ({m['delta_ms']:+}ms)")
