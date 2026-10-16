"""方案二：A（V16r2）与 B（盘点-转写）两路独立抽取的对齐与比对。

程序在这里只做配对和比较：按包号分组、按名称相似度配对标的行、逐字段判断是否一致。
不一致的地方交给定点重抽（recheck）由模型对照原文裁决；程序不生成任何字段值。
"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from .evaluation import _package_key
from .source_check import NUMERIC, norm_text

ITEM_FIELDS = ('item_name', 'category', 'brand', 'model', 'unit_price', 'quantity', 'total_price')
PROJECT_FIELDS = ('buyer', 'total_amount')


def _num(v: Any) -> float | None:
    from .extraction import to_number
    return to_number(v)


def same(field: str, a: Any, b: Any) -> bool:
    if field in NUMERIC or field == 'total_amount':
        x, y = _num(a), _num(b)
        if x is None or y is None:
            return x is None and y is None
        return abs(x - y) <= max(0.01, abs(y) * 0.001)
    return norm_text(a) == norm_text(b)


def _groups(items: list[dict]) -> dict[str | None, list[int]]:
    out: dict[str | None, list[int]] = {}
    for i, it in enumerate(items):
        out.setdefault(_package_key(it.get('package_no')), []).append(i)
    return out


def _row_score(a: dict, b: dict) -> float:
    x, y = norm_text(a.get('item_name')), norm_text(b.get('item_name'))
    s = SequenceMatcher(None, x, y).ratio() if x and y else 0.0
    if norm_text(a.get('model')) and norm_text(a.get('model')) == norm_text(b.get('model')):
        s += 0.3
    if norm_text(a.get('brand')) and norm_text(a.get('brand')) == norm_text(b.get('brand')):
        s += 0.2
    return s


def _pair(a_idx: list[int], b_idx: list[int], a_items: list[dict], b_items: list[dict]) -> list[tuple[int, int]]:
    cand = sorted(((_row_score(a_items[i], b_items[j]),
                    sum(a_items[i].get(f) is not None and b_items[j].get(f) is not None
                        and same(f, a_items[i].get(f), b_items[j].get(f)) for f in NUMERIC), i, j)
                   for i in a_idx for j in b_idx), reverse=True)
    pairs, used_a, used_b = [], set(), set()
    for s, _, i, j in cand:
        if s < 0.6:
            break
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        pairs.append((i, j))
    return pairs


def _align_groups(ga: dict, gb: dict, a_items: list[dict], b_items: list[dict]) -> dict:
    """两路包号写法不同（如“1-1”与“1”、“豫政采…-1”与“1”）时，按行内容把 B 的包对齐到 A 的包。

    只在包号集合不一致时调用；只有当 B 的某个包与 A 的某个包按名称一一高度对应时才重命名，
    否则保留原包号（照旧视为分歧交给重抽），所以不会把真正不同的包硬凑在一起。
    """
    out: dict = {k: v for k, v in gb.items() if k in ga}
    free_a = [k for k in ga if k not in gb]
    for kb, bi in gb.items():
        if kb in ga:
            continue
        best, best_score = None, 0.0
        for ka in free_a:
            ai = ga[ka]
            if len(ai) != len(bi):
                continue
            pairs = _pair(ai, bi, a_items, b_items)
            score = len(pairs) / len(ai) if ai else 0.0
            if score > best_score:
                best, best_score = ka, score
        if best is not None and best_score == 1.0:
            out[best] = bi
            free_a.remove(best)
            continue
        # A 把一个包拆成多个子包（1-1、1-2…）而 B 合成一个包（1）：B 的行按名称分配到这些 A 子包
        if free_a:
            assign: dict = {}
            remaining = list(bi)
            for ka in list(free_a):
                pairs = _pair(ga[ka], remaining, a_items, b_items)
                if len(pairs) == len(ga[ka]):
                    assign[ka] = [j for _, j in pairs]
                    remaining = [j for j in remaining if j not in assign[ka]]
            if assign and not remaining:
                for ka, js in assign.items():
                    out[ka] = js
                    free_a.remove(ka)
                continue
        out[kb] = out.get(kb, []) + bi
    return out


def compare(a: dict, b: dict) -> dict:
    """返回 {fields: 字段分歧, packages: 行集合分歧, project: 项目级分歧, agree: 一致字段数}。

    行号均指 A 结果中的行号；A 是合并的底稿。
    """
    a_items, b_items = a.get('items') or [], b.get('items') or []
    ga, gb = _groups(a_items), _groups(b_items)
    # 两边各自只有一个包时视为同一包（常见于单包项目：一边写“1”，另一边写 null）
    if len(ga) == 1 and len(gb) == 1:
        gb = {next(iter(ga)): next(iter(gb.values()))}
    elif set(ga) != set(gb):
        gb = _align_groups(ga, gb, a_items, b_items)
    fields, packages, agree = [], [], 0
    for key in list(ga) + [k for k in gb if k not in ga]:
        ai, bi = ga.get(key, []), gb.get(key, [])
        pairs = _pair(ai, bi, a_items, b_items)
        if not ai and not bi:
            continue
        if len(ai) != len(bi) or len(pairs) != len(ai):
            packages.append({'key': key, 'a_rows': ai, 'b_rows': [b_items[j] for j in bi]})
            # 清单问题和已配对行的字段问题同时保留，避免多一行掩盖其他字段差异。
        for i, j in pairs:
            for f in ITEM_FIELDS:
                va, vb = a_items[i].get(f), b_items[j].get(f)
                if same(f, va, vb):
                    agree += 1
                else:
                    fields.append({'row': i, 'field': f, 'a': va, 'b': vb})
    project = []
    for f in PROJECT_FIELDS:
        if same(f, a.get(f), b.get(f)):
            agree += 1
        else:
            project.append({'field': f, 'a': a.get(f), 'b': b.get(f)})
    return {'fields': fields, 'packages': packages, 'project': project, 'agree': agree}
