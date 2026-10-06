#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核专名（2026-08-04 圆桌新增 · 写 keep.json 前的辅助工具）
用法：核专名.py <工作目录>

═══ 为什么必须有这个 ═══
C2880 血证：whisper 把 FDE 全片听成 FD（5 处），而 FDE 是整条视频的核心概念词。
AI 通读一遍没发现，作者在 FCP 里手工改了 6 处。

按语种拆开命中率，规律非常干净：
    中文同音错字   13 处 → AI 抓到  9 处（69%）
    英文缩写/专名   7 处 → AI 抓到  0 处（ 0%）

**语义通读对英文缩写结构性无效。**
中文错字读起来会绊一下（知识「户」/ 内「讯」/ 公「寓」上面搞流量 / 降本「真相」），
语义通读抓得到；而 `FD` 读起来就是一个完全正常的英文缩写，句子里没有任何异样可绊。

唯一可靠的信号是**跨全片一致性**：同一个概念，全片 FD 出现 5 次、FDE 出现 2 次。
这是机器一眼能看见、人通读时反而看不见的东西——因为那两处相隔 3 分钟。

⚠ **多数派未必是对的**：本片正确的恰恰是少数派 FDE（5 票 vs 2 票）。
所以本脚本**只列候选，不判对错，不自动改任何东西**。

═══ 三条硬约束（防止它变成新的自动化事故源）═══
1. 只列候选，不判对错，不自动写 keep.json。
2. 不进 finalize 阻塞链路，不设 FAIL——它跟 查词.py 一样是手动辅助。
3. 区块① 的全清单必须完整打印，不许截断。它的价值恰恰在于「全都摆出来让人扫一眼」，
   截断就废了（这是 B7 报警被 warns[:10] 挤掉那个坑的同一形状）。
"""
import sys, os, re, json, difflib

WORK = sys.argv[1] if len(sys.argv) > 1 else ''
if not WORK or not os.path.isdir(WORK):
    print(__doc__); sys.exit(1)

wt_path = os.path.join(WORK, 'word_track.json')
if not os.path.exists(wt_path):
    print(f'✗ 找不到 {wt_path}——先跑 p1a_建词轴.py'); sys.exit(1)

WT = json.load(open(wt_path))
words = WT['words'] if isinstance(WT, dict) and 'words' in WT else WT
TXT = ''.join(w.get('w', w.get('t', '')) for w in words)

def ctx(i0, i1, pad=12):
    a = max(0, i0 - pad); b = min(len(words), i1 + pad)
    s = ''.join(words[k].get('w', words[k].get('t', '')) for k in range(a, b))
    return ('...' if a > 0 else '') + s + ('...' if b < len(words) else '')

def tstamp(i):
    w = words[i]
    return w.get('vs', w.get('ws', w.get('s', 0.0)))

# ---------- 区块①：英文/数字 token 全清单 ----------
# 词轴是按字/词切的，英文缩写常被切成多个词，所以先按「连续的拉丁/数字词」聚成 token
TOKS = []          # (tok, i0, i1, t)
i = 0
while i < len(words):
    w = words[i].get('w', words[i].get('t', ''))
    if re.search(r'[A-Za-z0-9%]', w):
        j = i; buf = ''
        while j < len(words):
            wj = words[j].get('w', words[j].get('t', ''))
            if not re.search(r'[A-Za-z0-9%]', wj): break
            buf += wj; j += 1
        tok = buf.strip()
        if tok: TOKS.append((tok, i, j - 1, tstamp(i)))
        i = j
    else:
        i += 1

print('=' * 78)
print(f'核专名：{os.path.basename(WORK.rstrip("/"))}   词轴 {len(words)} 词')
print('  ⚠ 只列候选，不判对错。多数派未必正确（C2880: FD×5 vs FDE×2，对的是 FDE）')
print('=' * 78)

print(f'\n【①英文/数字 token 全清单】{len(TOKS)} 处 —— 必读，逐行扫一眼')
for tok, i0, i1, t in TOKS:
    print(f"  {tok!r:<14} 词[{i0}-{i1}]{'':<3} @{t:7.1f}s  {ctx(i0, i1)}")

# ---------- 区块②：变体聚类 ----------
def norm(s): return re.sub(r'[\s\-_.]', '', s).lower()

print('\n【②变体聚类】同一实体出现两种写法 → 其中一种多半是转写错')
seen, clusters = {}, []
uniq = sorted({t[0] for t in TOKS}, key=lambda s: -len(s))
for a in uniq:
    for b in uniq:
        if a >= b: continue
        na, nb = norm(a), norm(b)
        if len(na) < 2 or len(nb) < 2: continue
        r = difflib.SequenceMatcher(None, na, nb).ratio()
        # 编辑距离 ≤1 的近似：长度差 ≤1 且相似度高，或一个是另一个的前缀
        if (nb.startswith(na) and len(nb) - len(na) <= 2) or r >= 0.8:
            ca = sum(1 for t in TOKS if t[0] == a); cb = sum(1 for t in TOKS if t[0] == b)
            clusters.append((a, ca, b, cb))
if clusters:
    for a, ca, b, cb in clusters:
        print(f'  ⚠ {a!r}×{ca}  ↔  {b!r}×{cb}   ← 少数派可能才是对的，逐处核')
        for tok, i0, i1, t in TOKS:
            if tok in (a, b):
                print(f'       {tok!r:<12} 词[{i0}-{i1}] @{t:7.1f}s  {ctx(i0, i1, 8)}')
                print(f'       └ 若要纠错，抄这行进 keep.json 的 fix：'
                      f'[{i0}, {i1}, "<正确写法>", "\'<正确写法>\'被听成\'{tok}\'"]')
else:
    print('  （无）')

# ---------- 区块③：热词表模糊匹配 ----------
HOT = ['Claude', 'Claude Code', 'Codex', 'DeepSeek', 'Kimi', 'Vibe Coding',
       'agent', 'skill', 'token', 'FDE', 'AI', 'MCP', 'API', 'GPT', 'LLM', 'RAG']
print('\n【③热词表模糊匹配】与已知热词形近 → 可能是它被听错了')
hit = False
_HOTN = {norm(h) for h in HOT}
for tok, i0, i1, t in TOKS:
    # tok 本身就是热词表里的正确写法 → 整个跳过。
    # 否则 'AI' 会被 'API' 勾出来报 17 次噪音（2026-08-04 实测）。
    if norm(tok) in _HOTN: continue
    for h in HOT:
        na, nb = norm(tok), norm(h)
        if difflib.SequenceMatcher(None, na, nb).ratio() >= 0.7 and abs(len(na) - len(nb)) <= 3:
            print(f'  ⚠ {tok!r} 词[{i0}-{i1}] @{t:7.1f}s ≈ 热词 {h!r}   {ctx(i0, i1, 8)}')
            hit = True; break
if not hit: print('  （无）')

# ---------- 区块④：中文高频实词一致性（默认关闭，需 --zh 才跑）----------
# ⚠ 2026-08-04 实测：这一块信噪比很差。C2880 上报出 30+ 对，绝大多数是
#   「两个↔十个」「工作↔合作」「流程↔过程」这类**本来就不同的正常词**，
#   真信号一条没有。留着会淹没区块①②③ 的精准命中（那三块在 C2880 上
#   一次抓出 FD×5↔FDE×2、ColdCode、A化，正是本轮漏掉的全部英文错字）。
#   所以默认不跑。中文同音错字本来就有 69% 能被语义通读抓到，不缺这一块。
#   需要时加 --zh 手动跑一次。
if '--zh' not in sys.argv:
    print('\n【④中文高频实词形近对】（默认跳过——信噪比差，30+ 对里真信号≈0；需要时加 --zh）')
else:
    print('\n【④中文高频实词形近对】全片出现 ≥3 次、且存在形近邻居 → 可能一处被听错')
    print('  ⚠ 本块噪音极大（C2880 实测 30+ 对全是正常词），只当穷举线索用，别逐条当真')
    try:
        import jieba
        freq = {}
        for tk in jieba.cut(TXT):
            if len(tk) >= 2 and re.fullmatch(r'[一-鿿]+', tk):
                freq[tk] = freq.get(tk, 0) + 1
        cand = {k: v for k, v in freq.items() if v >= 3}
        shown = set()
        for a in cand:
            for b in freq:
                if a == b or (b, a) in shown: continue
                if len(a) != len(b): continue
                if sum(1 for x, y in zip(a, b) if x != y) == 1:
                    print(f'  ⚠ {a}×{cand[a]}  ↔  {b}×{freq[b]}')
                    shown.add((a, b))
        if not shown: print('  （无）')
    except Exception as e:
        print(f'  （jieba 不可用，跳过：{e}）')

# ---------- 落盘 ----------
out = os.path.join(WORK, '专名候选.json')
json.dump({
    'tokens': [{'tok': t, 'i0': a, 'i1': b, 't': round(c, 2)} for t, a, b, c in TOKS],
    'variants': [{'a': a, 'a_n': ca, 'b': b, 'b_n': cb} for a, ca, b, cb in clusters],
    '_note': '只列候选，不判对错，不得自动套用。多数派未必正确。',
}, open(out, 'w'), ensure_ascii=False, indent=1)
print(f'\n📄 已写 {out}')
print('  ⚠ 这是给你写 keep.json 的 fix 用的清单，不是报警，也不阻塞流水线。')
