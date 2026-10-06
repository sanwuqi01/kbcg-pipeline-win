#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核断点（字幕卡边界质量自检 · 写 cards.json 后、p3 投影前的辅助工具）
用法：核断点.py <工作目录> [--json]

═══ 为什么必须有这个 ═══
实测血泪（2026-09-29 医生口播）：机械按词贪心打包出的卡，字宽合规、
不拆声学词、不拆英文 token——契约全绿——但把「白带」切成了
「…是好白 | 带呢」、「厘米」切成「…10个厘 | 米像…」。

根因：ASR 词轴按**字**切分（"白""带"是两个独立词），现有
caption_boundary_contract 的不可拆单元只认「多字声学词」和「英文
token」，医疗术语由单字拼成，恰好落在它的盲区。

医疗口播里术语被切在卡界，观众读到的第一眼是半个术语——比普通口播
伤得多（「白 | 带」会被读成「白」+「带」，理解成本和信任成本都上升）。

═══ 本脚本的边界（沿核专名.py 模式，不越权）═══
1. **只列候选，不判对错，不自动改 cards.json**。
2. 不进 finalize 阻塞链路（除非 --strict 由调用方显式要求非零退出码）。
3. 术语表只是**发现容易漏审的候选**；每个边界最终由 Agent/人按语义决定。

═══ 检测的三类问题 ═══
① 术语切界：卡边界落在术语词表（医疗词 + 可配置词）的内部
② 虚词悬尾：单卡以「的了是呢啊」等单字虚词收尾且下卡还有实词——
   意群没切干净，读感断在半句
③ 单字孤卡：整卡只有一个字（桥接卡除外，bridge 对象有 left_text/right_text）

═══ 与 caption_boundary_contract 的关系 ═══
contract 是**硬法**（错误即拒绝），管声学词/英文 token；
本脚本是**体检**（警告），管术语/意群这些需要词典与语感的东西。
2026-09-29 起术语词表也进 contract 作硬护栏（本脚本仍保留：
contract 只拦「词表内术语」，这里还多查虚词悬尾与孤卡）。
"""
import argparse
import json
import sys
from pathlib import Path

# ── 医疗口播高频术语（单字拼成、ASR 不会给成多字词的）────────────────────
DEFAULT_TERMS = [
    # 本片实测出现过
    "白带", "排卵", "备孕", "卵泡", "雌激素", "孕激素", "拉丝", "经前期",
    "鸡蛋清", "乳白色", "霉菌", "浑浊", "粘稠", "试孕", "激素", "生殖科",
    "同房", "阴道", "子宫", "宫颈", "炎症", "月经", "怀孕", "胎儿",
    "医生", "妇科", "儿科", "试管", "婴儿", "羊水", "剖腹产",
    # 常见度量与数值读法
    "厘米", "毫米", "公斤", "毫升",
]

# 句末语气词（了/呢/啊…）天然标记句界，收在卡尾是**正确**断点，不查；
# 只查「的/是」——结构助词悬尾才是意群没收干净、下卡还续着实词的信号。
FILLER_TAIL = set("的是")


def load_cards(work: Path) -> dict:
    cards_path = work / "cards.json"
    if not cards_path.is_file():
        print(f"✗ 找不到 {cards_path}——先写 cards.json 再核断点")
        raise SystemExit(1)
    return json.loads(cards_path.read_text(encoding="utf-8"))


def load_segment_texts(work: Path) -> dict[str, str]:
    rough_path = work / "rough_segments.json"
    rows = json.loads(rough_path.read_text(encoding="utf-8"))
    texts = {}
    for row in rows:
        key = (row.get("_rough_structure") or {}).get("playback_key") or f"{row.get('line')}.{row.get('part')}"
        texts[key] = row["text"]
    return texts


def term_spans(text: str, terms: list[str]) -> list[tuple[int, int, str]]:
    spans = []
    for term in terms:
        start = 0
        while True:
            idx = text.find(term, start)
            if idx < 0:
                break
            spans.append((idx, idx + len(term), term))
            start = idx + 1
    return spans


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work", type=Path)
    ap.add_argument("--terms", default=",".join(DEFAULT_TERMS),
                    help="追加术语（逗号分隔），与内置表合并")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--strict", action="store_true",
                    help="发现任何候选即退出码 2（默认 0，仅提示）")
    args = ap.parse_args()
    work = args.work.expanduser().resolve()

    cards = load_cards(work)
    seg_texts = load_segment_texts(work)
    terms = list(DEFAULT_TERMS) + [t.strip() for t in args.terms.split(",") if t.strip()]

    problems = {"term_split": [], "filler_tail": [], "orphan_card": []}

    for key, entries in cards.items():
        seg_text = seg_texts.get(key, "".join(
            e[0] if isinstance(e, list) else (e.get("cards") or [[e.get("left_text", "")]])[0][0]
            for e in entries if isinstance(e, (list, dict))
        ))
        # 展开简写与 bridge 两种形态，取卡文本序列
        card_texts = []
        for e in entries:
            if isinstance(e, list):
                card_texts.append(e[0])
            elif isinstance(e, dict):  # bridge 对象
                card_texts.extend([c[0] for c in e.get("cards", [])])
                # bridge 的 left_text/right_text 是跨段合成显示，不算本段切界
        joined = "".join(card_texts)

        # ① 术语切界
        cursor = 0
        boundaries = []
        for t in card_texts[:-1]:
            cursor += len(t)
            boundaries.append(cursor)
        for b in boundaries:
            for s, e, term in term_spans(joined, terms):
                if s < b < e:
                    left = joined[max(0, b - 6):b]
                    right = joined[b:b + 6]
                    problems["term_split"].append({
                        "key": key, "boundary": b, "term": term,
                        "context": f"「{left}|{right}」"})

        # ② 虚词悬尾：末字是虚词且该卡长 >1（一个字本身就是一张卡的见③）
        for i, t in enumerate(card_texts):
            if len(t) > 1 and t[-1] in FILLER_TAIL:
                nxt = card_texts[i + 1] if i + 1 < len(card_texts) else ""
                if nxt and nxt[0] not in FILLER_TAIL:
                    problems["filler_tail"].append({
                        "key": key, "card": t, "next": nxt[:6]})

        # ③ 单字孤卡（首卡/末卡同样算；两张连续孤卡也各算）
        for i, t in enumerate(card_texts):
            if len(t) == 1:
                prev = card_texts[i - 1] if i > 0 else ""
                problems["orphan_card"].append({
                    "key": key, "card": t, "prev_tail": prev[-4:] if prev else "",
                    "position": i, "total": len(card_texts)})

    n = sum(len(v) for v in problems.values())
    if args.json:
        print(json.dumps({"problems": problems, "count": n},
                         ensure_ascii=False, indent=1))
    else:
        print("=" * 78)
        print(f"核断点：{work.name}   卡数 {sum(len(v) for v in cards.values())}"
              f"   术语表 {len(terms)} 词")
        print("  ⚠ 只列候选，不判对错；意群断点最终由人/Agent 按语义定")
        print("=" * 78)
        for group, title in (("term_split", "① 术语切界（最伤医疗口播）"),
                             ("filler_tail", "② 虚词悬尾（意群没收干净）"),
                             ("orphan_card", "③ 单字孤卡（读感破碎）")):
            print(f"\n【{title}】{len(problems[group])} 处")
            if group == "term_split":
                for p in problems[group]:
                    print(f"  ⚠ {p['key']} {p['context']}  ←「{p['term']}」被切在卡界")
            elif group == "filler_tail":
                for p in problems[group]:
                    print(f"  ⚠ {p['key']} 「{p['card']}」收在虚词，下一卡「{p['next']}…」")
            else:
                for p in problems[group]:
                    print(f"  ⚠ {p['key']} 第{p['position'] + 1}/{p['total']}卡 单字「{p['card']}」"
                          f"（上卡尾「…{p['prev_tail']}」）")
        if n == 0:
            print("\n  （干净：无术语切界、无虚词悬尾、无单字孤卡）")
        else:
            print(f"\n共 {n} 处候选——逐条按意群重切；别为清零把卡硬拉长超 10 字宽")

    if args.strict and n:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
