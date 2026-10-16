"""五类关系场景。与 frontend/src/api/sample.ts 中的实现逐一对应，保证两种模式口径一致。"""
from __future__ import annotations

import time
from typing import Any

from .extraction import norm_org
from .evaluation import _package_key
from .queries import Store, Unit, co_groups, store

SCOPE_LABEL = {'project': '项目级', 'package': '同包级'}


class Acc:
    __slots__ = ('count', 'known', 'unknown', 'units', 'anns', 'wins')

    def __init__(self):
        self.count, self.known, self.unknown, self.wins = 0, None, 0, 0
        self.units: set = set()
        self.anns: set = set()

    def bump(self, u: Unit, amount: float | None) -> None:
        if u.key in self.units:
            return
        self.units.add(u.key)
        self.count += 1
        self.anns.update(u.ann_ids)
        if amount is None:
            self.unknown += 1
        else:
            self.known = (self.known or 0) + amount


def units_for(s: Store, req: dict) -> list[Unit]:
    all_units = (s.packages if req.get('scope') == 'package' else s.projects).values()
    dr = req.get('date_range') or [None, None]
    frm, to = (dr + [None, None])[:2]
    out = []
    for u in all_units:
        d = (u.date or '')[:10]
        if frm and (not d or d < frm):
            continue
        if to and (not d or d > to):
            continue
        out.append(u)
    return out


def attributable(u: Unit) -> float | None:
    return u.amount if len(u.winners) == 1 else None


def summarize(s: Store, units: list[Unit]) -> dict:
    pkeys = {u.project_key for u in units}
    pkg_count = sum(max(1, len(u.package_nos)) for u in units)
    by_project: dict[str, float | None] = {}
    for u in units:
        if u.project_key not in by_project:
            p = s.projects.get(u.project_key)
            by_project[u.project_key] = p.amount if p else u.amount
    known, unknown = None, 0
    for v in by_project.values():
        if v is None:
            unknown += 1
        else:
            known = (known or 0) + v
    return {'project_count': len(pkeys), 'package_count': pkg_count, 'known_amount': known, 'unknown_count': unknown}


def pair_key(a: str, b: str) -> str:
    return f'{a}\u0001{b}' if a < b else f'{b}\u0001{a}'


def name(s: Store, k: str) -> str:
    return s.display.get(k, k)


def col(key: str, label: str, kind: str | None = None) -> dict:
    return {'key': key, 'label': label, **({'kind': kind} if kind else {})}


def s1(s: Store, req: dict, units: list[Unit]) -> dict:
    buyer = norm_org(req.get('buyer_id') or '')
    mine = [u for u in units if u.buyer == buyer]
    acc: dict[str, Acc] = {}
    for u in mine:
        for w in u.winners:
            acc.setdefault(w, Acc()).bump(u, attributable(u))
    srt = sorted(acc.items(), key=lambda kv: (-kv[1].count, -(kv[1].known if kv[1].known is not None else -1)))
    rows = [{'_key': k, 'rank': i + 1, 'supplier': name(s, k), 'count': v.count, 'known_amount': v.known, 'unknown': v.unknown}
            for i, (k, v) in enumerate(srt)]
    ann_ids = {a for u in mine for a in u.ann_ids}
    ann_units: dict[str, list[Unit]] = {}
    for u in mine:
        for aid in u.ann_ids:
            ann_units.setdefault(aid, []).append(u)
    brands: dict[str, dict] = {}
    for it in s.items:
        if it['announcement_id'] not in ann_ids or not it.get('brand'):
            continue
        matched = ann_units[it['announcement_id']]
        if req['scope'] == 'package':
            pkey = _package_key(it.get('package_no'))
            matched = [u for u in matched if pkey in {_package_key(p) for p in u.package_nos}
                       or (pkey is None and len((s.ann_by_id.get(it['announcement_id']) or {}).get('packages') or []) <= 1)]
        if not matched:
            continue
        b = brands.setdefault(it['brand'], {'n': 0, 'anns': set(), 'sup': set(),
                                            'units': set(), 'amount': None, 'unknown': 0})
        b['n'] += 1
        b['anns'].add(it['announcement_id'])
        b['units'].update(u.key for u in matched)
        amount = it.get('total_price')
        if amount is None:
            b['unknown'] += 1
        else:
            b['amount'] = (b['amount'] or 0) + amount
        if it.get('supplier'):
            b['sup'].add(it['supplier'])
    brand_sorted = sorted(brands.items(), key=lambda kv: (-len(kv[1]['units']),
                          -(kv[1]['amount'] if kv[1]['amount'] is not None else -1), kv[0]))
    brand_rows = [{'_key': 'brand:' + k, 'brand': k, 'lines': v['n'], 'projects': len(v['anns']),
                   'count': len(v['units']), 'known_amount': v['amount'], 'unknown': v['unknown'],
                   'suppliers': '、'.join(sorted(v['sup'])) or None} for k, v in brand_sorted]
    top = rows[:req['top_k']]
    nodes = [{'id': buyer, 'label': name(s, buyer), 'kind': 'buyer'}] + \
            [{'id': r['_key'], 'label': r['supplier'], 'kind': 'supplier', 'weight': r['count']} for r in top]
    edges = [{'source': buyer, 'target': r['_key'], 'label': f"{r['count']} 次", 'weight': r['count']} for r in top]
    refs = {k: sorted(v.anns) for k, v in srt}
    refs.update({'brand:' + k: sorted(v['anns']) for k, v in brands.items()})
    return {**summarize(s, mine),
            'definition': f"「{name(s, buyer)}」的中标供应商，按{SCOPE_LABEL[req['scope']]}合作次数排序。",
            'columns': [col('rank', '排名', 'count'), col('supplier', '中标供应商'), col('count', '合作次数', 'count'),
                        col('known_amount', '已归属交易金额', 'money'), col('unknown', '金额未归属次数', 'count')],
            'rows': rows,
            'secondary': {'title': '产品供应商合作统计',
                          'columns': [col('brand', '产品供应商（品牌）'), col('count', '合作次数', 'count'),
                                      col('known_amount', '已知交易总金额', 'money'),
                                      col('unknown', '金额未披露明细', 'count'), col('lines', '标的条数', 'count'),
                                      col('projects', '涉及公告数', 'count'), col('suppliers', '对应中标供应商')],
                          'rows': brand_rows},
            'nodes': nodes, 'edges': edges, 'evidence_refs': refs}


def s2(s: Store, req: dict, units: list[Unit]) -> dict:
    buyer = norm_org(req.get('buyer_id') or '')
    mine = [u for u in units if u.buyer == buyer]
    only = bool(req.get('participant_only'))
    acc: dict[str, Acc] = {}
    pairs: dict[str, Acc] = {}
    for u in mine:
        lst = sorted(b for b in u.bidders if not only or b not in u.winners)
        for b in lst:
            a = acc.setdefault(b, Acc())
            if u.key not in a.units and b in u.winners:
                a.wins += 1
            a.bump(u, None)
        # 答疑 A2：协同投标组合严格按“同一项目同一包”计；不同包是独立子项目，各计一次
        for g in co_groups(s, u):
            glst = sorted(b for b in g.bidders if not only or b not in g.winners)
            for i in range(len(glst)):
                for j in range(i + 1, len(glst)):
                    pairs.setdefault(pair_key(glst[i], glst[j]), Acc()).bump(g, None)
    srt = sorted(acc.items(), key=lambda kv: -kv[1].count)
    rows = [{'_key': k, 'rank': i + 1, 'bidder': name(s, k), 'count': v.count, 'wins': v.wins, 'losses': v.count - v.wins}
            for i, (k, v) in enumerate(srt)]
    psrt = sorted(((k, v) for k, v in pairs.items() if v.count >= 2), key=lambda kv: -kv[1].count)
    pair_rows = []
    for k, v in psrt[:50]:
        a, b = k.split('\u0001')
        pair_rows.append({'_key': 'pair:' + k, 'a': name(s, a), 'b': name(s, b), 'count': v.count})
    top = rows[:req['top_k']]
    top_ids = {r['_key'] for r in top}
    nodes = [{'id': buyer, 'label': name(s, buyer), 'kind': 'buyer'}] + \
            [{'id': r['_key'], 'label': r['bidder'], 'kind': 'supplier', 'weight': r['count']} for r in top]
    edges = [{'source': buyer, 'target': r['_key'], 'label': str(r['count']), 'weight': r['count']} for r in top]
    for k, v in psrt:
        a, b = k.split('\u0001')
        if a in top_ids and b in top_ids:
            edges.append({'source': a, 'target': b, 'label': f'共现 {v.count}', 'weight': v.count})
    refs = {k: sorted(v.anns) for k, v in srt}
    refs.update({'pair:' + k: sorted(v.anns) for k, v in psrt[:50]})
    return {**summarize(s, mine),
            'definition': f"「{name(s, buyer)}」的{'未中标参与方' if only else '投标主体'}频次；共同参与按同项目、同包统计。",
            'columns': [col('rank', '排名', 'count'), col('bidder', '投标主体'), col('count', '参与次数', 'count'),
                        col('wins', '其中中标', 'count'), col('losses', '其中未中标', 'count')],
            'rows': rows,
            'secondary': {'title': '高频共同参与组合（同一项目同一包内两两共现 ≥ 2 次）',
                          'columns': [col('a', '主体 A'), col('b', '主体 B'), col('count', '共同参与次数', 'count')],
                          'rows': pair_rows},
            'nodes': nodes, 'edges': edges, 'evidence_refs': refs}


def s3(s: Store, req: dict, units: list[Unit]) -> dict:
    focus = norm_org((req.get('supplier_ids') or [''])[0])
    participated = [u for u in units if focus in u.bidders]
    acc: dict[str, Acc] = {}
    shared_groups: dict[str, list[Unit]] = {}
    no_others = 0
    for u in participated:
        # 答疑 A2：只有与本方在同一项目同一包竞标才算共同竞标，不同包各计一次
        groups = [g for g in co_groups(s, u) if focus in g.bidders]
        if not any(len(g.bidders) > 1 for g in groups):
            no_others += 1
        for g in groups:
            for b in sorted(g.bidders):
                if b == focus:
                    continue
                a = acc.setdefault(b, Acc())
                if g.key in a.units:
                    continue
                if b in g.winners:
                    a.wins += 1
                a.bump(g, None)
                shared_groups.setdefault(b, []).append(g)
    srt = sorted(acc.items(), key=lambda kv: -kv[1].count)
    rows = []
    for i, (k, v) in enumerate(srt):
        shared = shared_groups[k]
        sm = summarize(s, shared)
        mine_sc = [u.bids[focus]['score'] for u in shared if (u.bids.get(focus) or {}).get('score') is not None]
        their_sc = [u.bids[k]['score'] for u in shared if (u.bids.get(k) or {}).get('score') is not None]
        their_amt = [u.bids[k]['amount'] for u in shared if (u.bids.get(k) or {}).get('amount')]
        rows.append({'_key': k, 'rank': i + 1, 'bidder': name(s, k), 'count': v.count, 'wins': v.wins,
                     'total_amount': sm['known_amount'], 'unknown': sm['unknown_count'],
                     'focus_score': round(sum(mine_sc) / len(mine_sc), 2) if mine_sc else None,
                     'rival_score': round(sum(their_sc) / len(their_sc), 2) if their_sc else None,
                     'rival_bid': sum(their_amt) if their_amt else None})
    top = rows[:req['top_k']]
    nodes = [{'id': focus, 'label': name(s, focus), 'kind': 'focus'}] + \
            [{'id': r['_key'], 'label': r['bidder'], 'kind': 'supplier', 'weight': r['count']} for r in top]
    edges = [{'source': focus, 'target': r['_key'], 'label': f"共投 {r['count']}", 'weight': r['count']} for r in top]
    return {**summarize(s, participated),
            'definition': f"「{name(s, focus)}」参与 {len(participated)} 个{SCOPE_LABEL[req['scope']]}单元，按同项目、同包的共同竞标次数排序。",
            'columns': [col('rank', '排名', 'count'), col('bidder', '共同竞标主体'), col('count', '共同竞标次数', 'count'),
                        col('total_amount', '共同项目总金额', 'money'), col('rival_bid', '对方报价合计', 'money'),
                        col('focus_score', '本方平均得分', 'count'), col('rival_score', '对方平均得分', 'count'),
                        col('wins', '其中该主体中标', 'count')],
            'rows': rows, 'nodes': nodes, 'edges': edges, 'evidence_refs': {k: sorted(v.anns) for k, v in srt},
            'notes': [f'另有 {no_others} 个参与单元缺少其他投标主体信息。'] if no_others else []}


def s4(s: Store, req: dict, units: list[Unit]) -> dict:
    sups = list(dict.fromkeys(norm_org(x) for x in req.get('supplier_ids') or []))
    per: list[dict[str, Acc]] = []
    for sp in sups:
        m: dict[str, Acc] = {}
        for u in units:
            if sp in u.winners:
                m.setdefault(u.buyer, Acc()).bump(u, attributable(u))
        per.append(m)
    common = [b for b in (per[0].keys() if per else []) if all(b in m for m in per)]
    involved: list[Unit] = []
    rows = []
    for b in common:
        us = [u for u in units if u.buyer == b and any(sp in u.winners for sp in sups)]
        involved += us
        sm = summarize(s, us)
        row: dict[str, Any] = {'_key': b, 'buyer': name(s, b), 'projects': sm['project_count'],
                               'total_amount': sm['known_amount'], 'unknown': sm['unknown_count']}
        for i in range(len(sups)):
            row[f'c{i}'] = per[i][b].count
            row[f'a{i}'] = per[i][b].known
        rows.append(row)
    rows.sort(key=lambda r: -r['projects'])
    shown_rows = rows[:req['top_k'] * 2]
    shown = {r['_key'] for r in shown_rows}
    nodes = [{'id': sp, 'label': name(s, sp), 'kind': 'focus'} for sp in sups] + \
            [{'id': r['_key'], 'label': r['buyer'], 'kind': 'buyer', 'weight': r['projects']} for r in shown_rows]
    edges = [{'source': sp, 'target': b, 'label': f'{v.count} 次', 'weight': v.count}
             for i, sp in enumerate(sups) for b, v in per[i].items() if b in shown]
    refs = {r['_key']: sorted({a for u in involved if u.buyer == r['_key'] for a in u.ann_ids}) for r in rows}
    cols = []
    for i, sp in enumerate(sups):
        n = name(s, sp)
        short = n[:10] + '…' if len(n) > 10 else n
        cols += [col(f'c{i}', f'{short} 次数', 'count'), col(f'a{i}', f'{short} 已归属金额', 'money')]
    return {**summarize(s, involved),
            'definition': f"所选 {len(sups)} 家供应商共同合作的采购单位，按{SCOPE_LABEL[req['scope']]}统计。",
            'columns': [col('buyer', '共同采购单位'), *cols, col('projects', '去重项目数', 'count'),
                        col('total_amount', '去重项目总金额', 'money'), col('unknown', '金额未知项目', 'count')],
            'rows': rows, 'nodes': nodes, 'edges': edges, 'evidence_refs': refs}


def s5(s: Store, req: dict, units: list[Unit]) -> dict:
    sups = list(dict.fromkeys(norm_org(x) for x in req.get('supplier_ids') or []))
    # 答疑 A2：所选供应商须在同一项目同一包内同时投标，才算共同竞标
    joint = [u for u in units if sups and any(all(sp in g.bidders for sp in sups) for g in co_groups(s, u))]
    rows = []
    for u in joint:
        row: dict[str, Any] = {'_key': u.key, 'project': u.title, 'buyer': name(s, u.buyer), 'date': u.date,
                               'amount': u.amount, 'packages': '、'.join(u.package_nos) or None}
        for i, sp in enumerate(sups):
            row[f'r{i}'] = '中标' if sp in u.winners else '未中标'
            bid = u.bids.get(sp) or {}
            row[f's{i}'] = bid.get('score')
            row[f'k{i}'] = bid.get('rank')
            row[f'b{i}'] = bid.get('amount')
        row['winner'] = '、'.join(name(s, w) for w in sorted(u.winners)) or None
        row['n_bidders'] = len(u.bidders)
        rows.append(row)
    rows.sort(key=lambda r: r['date'] or '', reverse=True)
    # 核心指标：共同项目数、总金额、各家在共同项目中的中标次数
    joint_wins = {sp: sum(1 for u in joint if sp in u.winners) for sp in sups}
    other_win = sum(1 for u in joint if not any(sp in u.winners for sp in sups))
    shown = joint[:req['top_k'] * 3]
    nodes = [{'id': sp, 'label': name(s, sp), 'kind': 'focus'} for sp in sups] + \
            [{'id': 'p:' + u.key, 'label': u.title[:16] + '…' if len(u.title) > 16 else u.title, 'kind': 'project'} for u in shown]
    edges = [{'source': sp, 'target': 'p:' + u.key, 'label': '中标' if sp in u.winners else '参与'} for u in shown for sp in sups]
    per_cols = []
    for i, sp in enumerate(sups):
        n = name(s, sp)
        short = n[:8] + '…' if len(n) > 8 else n
        per_cols += [col(f'r{i}', f'{short} 结果'), col(f's{i}', f'{short} 得分', 'count'),
                     col(f'k{i}', f'{short} 排名', 'count'), col(f'b{i}', f'{short} 报价', 'money')]
    sm = summarize(s, joint)
    summary_rows = [{'_key': 'sum:' + sp, 'supplier': name(s, sp), 'joint': len(joint), 'wins': joint_wins[sp],
                     'rate': f"{joint_wins[sp] / len(joint) * 100:.0f}%" if joint else None} for sp in sups]
    summary_rows.append({'_key': 'sum:other', 'supplier': '（均未中标，由其他主体中标）', 'joint': len(joint),
                         'wins': other_win, 'rate': f"{other_win / len(joint) * 100:.0f}%" if joint else None})
    return {**sm,
            'definition': f"所选 {len(sups)} 家供应商共同参与的项目及竞标结果，按{SCOPE_LABEL[req['scope']]}统计。",
            'columns': [col('project', '项目'), col('buyer', '采购单位'), col('date', '公告日期'),
                        *per_cols, col('winner', '中标方'), col('n_bidders', '投标家数', 'count'),
                        col('packages', '关联包'), col('amount', '项目金额', 'money')],
            'rows': rows,
            'secondary': {'title': f"共同竞标结果汇总（共同项目 {sm['project_count']} 个）",
                          'columns': [col('supplier', '主体'), col('joint', '共同竞标单元', 'count'),
                                      col('wins', '中标次数', 'count'), col('rate', '中标占比')],
                          'rows': summary_rows},
            'nodes': nodes, 'edges': edges, 'evidence_refs': {u.key: u.ann_ids for u in joint}}


HANDLERS = {1: s1, 2: s2, 3: s3, 4: s4, 5: s5}


def run(scenario: int, req: dict) -> dict:
    s = store(req.get('job_id'))
    t0 = time.perf_counter()
    req = {'scope': 'project', 'top_k': 5, **{k: v for k, v in req.items() if v is not None}}
    if req['scope'] not in SCOPE_LABEL:
        raise ValueError('scope 只能是 project 或 package')
    req['top_k'] = max(1, min(int(req['top_k']), 50))
    part = HANDLERS[scenario](s, req, units_for(s, req))
    return {'scenario': scenario, 'definition': '', 'columns': [], 'rows': [], 'nodes': [], 'edges': [],
            'project_count': 0, 'package_count': 0, 'known_amount': None, 'unknown_count': 0,
            'evidence_refs': {}, 'notes': [], 'secondary': None, **part,
            'job_id': req.get('job_id'), 'scope_announcements': len(s.anns),
            'source_revision': f'sql:rev{s.revision}',
            'query_ms': (time.perf_counter() - t0) * 1000}
