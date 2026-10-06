#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
写 keep.json 的辅助工具（2026-07-31 新增）
用法：
  查词.py <工作目录> <要找的文本>       在词轴里找这段话，给出可直接抄进 keep.json 的下标
  查词.py <工作目录> --dump [起] [止]   按下标区间打印词轴（核对用）
  查词.py <工作目录> --check            体检现有 keep.json：每条 drop/fix 实际删/改了什么

═══ 为什么需要它 ═══
keep.json 是 AI 唯一动脑的地方，而它的语法是【词下标区间】。
下标只能靠数——本轮我至少数错 5 次：
  · [101,103] 想删「怎么讲呢」，漏了「呢」
  · [109,110] 想删「对不对」，剩下半个「对」
  · [507,511] 想删「只要有动作」，漏了「作」
  · [567,568] 以为是「它就开始」，实际只有「它就」
  · [878] 想指「好」，实际指到了「现在」
每错一次就毁掉一句话，而且要跑完整条流水线才可能被发现。
p1 的下标自校验能兜住一部分（理由里写了引号文本时），但那是【事后】。
这个工具是【事前】：先查准下标，再写 keep.json。
"""
import sys, os, json, re

if len(sys.argv) < 3:
    sys.exit(__doc__)
WORK = sys.argv[1]
tk = json.load(open(f'{WORK}/word_track.json'))
W = tk['words']
FULL = ''.join(w['t'] for w in W)
# 字符位置 → 词下标
c2i, pos = [], 0
for i, w in enumerate(W):
    for _ in range(len(w['t'])):
        c2i.append(i)
    pos += len(w['t'])

def ctx(a, b, pad=6):
    lo, hi = max(0, a - pad), min(len(W), b + pad + 1)
    pre = ''.join(W[i]['t'] for i in range(lo, a))
    mid = ''.join(W[i]['t'] for i in range(a, b + 1))
    suf = ''.join(W[i]['t'] for i in range(b + 1, hi))
    return f'{pre}【{mid}】{suf}'

arg = sys.argv[2]

if arg == '--dump':
    a = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    b = int(sys.argv[4]) if len(sys.argv) > 4 else min(a + 40, len(W) - 1)
    for i in range(max(0, a), min(len(W), b + 1)):
        w = W[i]
        print(f'  {i:>4} 「{w["t"]}」 {w["vs"]:7.2f}-{w["ve"]:7.2f}')
    sys.exit()

if arg == '--check':
    kp = f'{WORK}/keep.json'
    if not os.path.exists(kp):
        sys.exit('本片没有 keep.json（走 edl 粗区间路径）')
    kd = json.load(open(kp))
    print(f'=== drop {len(kd.get("drop", []))} 处 ===')
    for it in kd.get('drop', []):
        a, b = it[0], it[1]
        why = it[2] if len(it) > 2 else ''
        got = ''.join(W[i]['t'] for i in range(a, min(b + 1, len(W))))
        dur = W[min(b, len(W)-1)]['ve'] - W[a]['vs']
        print(f'  [{a}-{b}] {dur:5.2f}s 实删「{got}」')
        print(f'          上下文 {ctx(a, b)}')
        if why: print(f'          理由 {why}')
    if kd.get('fix'):
        print(f'=== fix {len(kd["fix"])} 处 ===')
        for it in kd['fix']:
            a, b, new = it[0], it[1], it[2]
            old = ''.join(W[i]['t'] for i in range(a, min(b + 1, len(W))))
            flag = '' if len(new) == len(old) else '  ⚠ 不等长→整并到首词'
            print(f'  [{a}-{b}] 「{old}」→「{new}」{flag}')
            print(f'          上下文 {ctx(a, b)}')
    sys.exit()

# ---- 正查：文本 → 下标 ----
q = re.sub(r'\s', '', arg)
hits, start = [], 0
while True:
    k = FULL.find(q, start)
    if k < 0: break
    hits.append(k); start = k + 1
if not hits:
    print(f'✗ 词轴里找不到「{q}」')
    # 给个近似提示：逐字找最长公共前缀
    for n in range(len(q) - 1, 1, -1):
        sub = q[:n]
        if FULL.find(sub) >= 0:
            print(f'  最长能匹配的前缀是「{sub}」——后面的字可能被 whisper 漏转或听成别的字')
            break
    sys.exit(1)

print(f'找到 {len(hits)} 处「{q}」：')
for k in hits:
    a, b = c2i[k], c2i[k + len(q) - 1]
    exact = ''.join(W[i]['t'] for i in range(a, b + 1))
    warn = ''
    if exact != q:
        warn = (f'\n        ⚠ 词边界不齐：该区间实际是「{exact}」，比你要找的多出 '
                f'「{exact.replace(q, "", 1)}」——按词下标删只能整词删')
    print(f'  [{a}, {b}]  {W[a]["vs"]:.2f}-{W[b]["ve"]:.2f}s ({W[b]["ve"]-W[a]["vs"]:.2f}s){warn}')
    print(f'        {ctx(a, b)}')
    print(f'        可抄: [{a}, {b}, "删：\'{exact}\'"]')
