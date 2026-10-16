"""任务二总览：关系模型规模 + 赛题 6 项要求逐条的示例查询与结果摘要。

示例对象从当前库中自动挑选“关系最丰富”的主体，保证评审打开页面就能看到非空结果；
统计全部复用 scenarios.py / profiles.py，口径与各页面完全一致。
"""
from __future__ import annotations

import time
from collections import Counter
from itertools import combinations

from . import profiles, scenarios
from .queries import Store, co_groups, store


def model_stats(s: Store) -> dict:
    units = list(s.projects.values())
    buyers = {u.buyer for u in units}
    winners = {w for u in units for w in u.winners}
    bidders = {b for u in units for b in u.bidders}
    bw = {(u.buyer, w) for u in units for w in u.winners}
    bb = {(u.buyer, b) for u in units for b in u.bidders if b not in u.winners}
    # 答疑 A2：共同竞标关系按同一项目同一包统计
    co = {p for u in units for g in co_groups(s, u) for p in combinations(sorted(g.bidders), 2)}
    bid_records = sum(len(u.bids) for u in units)
    scored = sum(1 for u in units for v in u.bids.values() if v.get('score') is not None)
    priced = sum(1 for u in units for v in u.bids.values() if v.get('amount') is not None)
    brands = {it['brand'] for it in s.items if it.get('brand')}
    buyer_brand = {(it['buyer'], it['brand']) for it in s.items if it.get('buyer') and it.get('brand')}
    return {
        'announcements': len(s.anns), 'projects': len(units), 'packages': len(s.packages),
        'buyers': len(buyers), 'winners': len(winners), 'participants': len(bidders - winners), 'bidders': len(bidders),
        'edges_buyer_winner': len(bw), 'edges_buyer_participant': len(bb), 'edges_co_bid': len(co),
        'bid_records': bid_records, 'bid_with_score': scored, 'bid_with_amount': priced,
        'product_suppliers': len(brands), 'edges_buyer_product_supplier': len(buyer_brand),
    }


def suggestions(s: Store) -> dict:
    units = list(s.projects.values())
    # 采购单位：有竞标信息（≥2 家）的项目最多
    bc = Counter(u.buyer for u in units if len(u.bidders) >= 2)
    buyer = bc.most_common(1)[0][0] if bc else None
    # 中标供应商：中标且有其他竞标方的项目最多
    wc = Counter(w for u in units if len(u.bidders) >= 2 for w in u.winners)
    winner = wc.most_common(1)[0][0] if wc else None
    # 场景 4：共同采购单位最多的两家中标供应商
    by_buyer: dict[str, set] = {}
    for u in units:
        by_buyer.setdefault(u.buyer, set()).update(u.winners)
    pc4: Counter = Counter()
    for ws in by_buyer.values():
        for p in combinations(sorted(ws), 2):
            pc4[p] += 1
    pair4 = list(pc4.most_common(1)[0][0]) if pc4 else []
    # 场景 5：共同投标项目最多的两家中标供应商
    all_winners = {w for u in units for w in u.winners}
    pc5: Counter = Counter()
    for u in units:
        pairs = {p for g in co_groups(s, u) for p in combinations(sorted(b for b in g.bidders if b in all_winners), 2)}
        for p in pairs:
            pc5[p] += 1
    pair5 = list(pc5.most_common(1)[0][0]) if pc5 else []
    name = lambda k: s.display.get(k, k)  # noqa: E731
    return {'buyer': name(buyer) if buyer else None, 'winner': name(winner) if winner else None,
            'pair4': [name(x) for x in pair4], 'pair5': [name(x) for x in pair5]}


def _brief(r: dict, label_key: str) -> dict:
    top = r['rows'][0] if r['rows'] else None
    return {'rows': len(r['rows']), 'project_count': r['project_count'], 'known_amount': r['known_amount'],
            'top': top.get(label_key) if top else None, 'top_count': top.get('count') if top else None,
            'secondary_rows': len((r.get('secondary') or {}).get('rows') or [])}


def overview(job_id: str | None = None) -> dict:
    t0 = time.perf_counter()
    s = store(job_id)
    sug = suggestions(s)
    out: dict = {'model': model_stats(s), 'suggestions': sug, 'results': {}}
    res = out['results']
    if sug['buyer']:
        res['s1'] = _brief(scenarios.run(1, {'buyer_id': sug['buyer'], 'job_id': job_id}), 'supplier')
        res['s2'] = _brief(scenarios.run(2, {'buyer_id': sug['buyer'], 'job_id': job_id}), 'bidder')
        p = profiles.run('buyer', {'org': sug['buyer'], 'job_id': job_id})
        res['buyer_profile'] = {'tags': p['tags'], 'metrics': p['metrics'][:6]}
    if sug['winner']:
        res['s3'] = _brief(scenarios.run(3, {'supplier_ids': [sug['winner']], 'job_id': job_id}), 'bidder')
        p = profiles.run('supplier', {'org': sug['winner'], 'job_id': job_id})
        res['supplier_profile'] = {'tags': p['tags'], 'metrics': p['metrics'][:6]}
    if len(sug['pair4']) == 2:
        res['s4'] = _brief(scenarios.run(4, {'supplier_ids': sug['pair4'], 'job_id': job_id}), 'buyer')
    if len(sug['pair5']) == 2:
        res['s5'] = _brief(scenarios.run(5, {'supplier_ids': sug['pair5'], 'job_id': job_id}), 'project')
    out['job_id'] = job_id
    out['scope_announcements'] = len(s.anns)
    out['source_revision'] = f'sql:rev{s.revision}'
    out['query_ms'] = (time.perf_counter() - t0) * 1000
    return out
