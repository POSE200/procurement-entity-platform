"""用户画像：采购单位画像与供应商（投标主体）画像。确定性统计，模型不参与。

口径与 scenarios.py 一致：
- 单元 = 项目级（采购单位+项目编号）或同包级；项目数去重。
- 金额只在单元仅一个中标方时归属给该中标方；null 不当 0。
- 得分/排名/报价来自公告公布的竞标结果，原文未公布时为 null，不参与平均。
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Any

from .extraction import norm_org
from .queries import Store, Unit, co_groups, store
from .scenarios import SCOPE_LABEL, attributable, summarize, units_for


def _avg(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _name(s: Store, k: str) -> str:
    return s.display.get(k, k)


def _month(d: str | None) -> str | None:
    return d[:7] if d else None


def _trend(units: list[Unit], amount_of) -> list[dict]:
    m: dict[str, dict] = {}
    for u in units:
        k = _month(u.date)
        if not k:
            continue
        r = m.setdefault(k, {'month': k, 'count': 0, 'amount': None})
        r['count'] += 1
        a = amount_of(u)
        if a is not None:
            r['amount'] = (r['amount'] or 0) + a
    return [m[k] for k in sorted(m)]


def _categories(s: Store, ann_ids: set[str], supplier: str | None = None, limit: int = 10) -> list[dict]:
    cats: Counter = Counter()
    brands: Counter = Counter()
    for it in s.items:
        if it['announcement_id'] not in ann_ids:
            continue
        if supplier and norm_org(it.get('supplier') or '') != supplier:
            continue
        if it.get('category'):
            cats[str(it['category']).split('/')[-1].split('\\')[-1]] += 1
        if it.get('brand'):
            brands[it['brand']] += 1
    return ([{'name': k, 'count': v} for k, v in cats.most_common(limit)],
            [{'name': k, 'count': v} for k, v in brands.most_common(limit)])


def buyer_profile(s: Store, req: dict, units: list[Unit]) -> dict:
    buyer = norm_org(req.get('org') or '')
    mine = [u for u in units if u.buyer == buyer]
    if not mine:
        raise LookupError(f'没有找到采购单位「{req.get("org")}」的公告')
    sm = summarize(s, mine)
    sup: dict[str, dict] = {}
    bidder_cnt: Counter = Counter()
    n_bidders = []
    winner_scores, win_discount = [], []
    for u in mine:
        if len(u.bidders) > 0:
            n_bidders.append(len(u.bidders))
        for b in u.bidders:
            bidder_cnt[b] += 1
        for w in u.winners:
            r = sup.setdefault(w, {'count': 0, 'amount': None})
            r['count'] += 1
            a = attributable(u)
            if a is not None:
                r['amount'] = (r['amount'] or 0) + a
            bid = u.bids.get(w) or {}
            if bid.get('score') is not None:
                winner_scores.append(bid['score'])
        # 中标报价相对其他有效报价均值的比例（反映价格竞争程度）
        others = [v['amount'] for k, v in u.bids.items() if k not in u.winners and v.get('amount')]
        wins = [u.bids[w]['amount'] for w in u.winners if (u.bids.get(w) or {}).get('amount')]
        if others and len(wins) == 1:
            win_discount.append(wins[0] / (sum(others) / len(others)))
    total_wins = sum(r['count'] for r in sup.values()) or 1
    top_sup = sorted(sup.items(), key=lambda kv: (-kv[1]['count'], -(kv[1]['amount'] or 0)))
    shares = [r['count'] / total_wins for _, r in top_sup]
    ann_ids = {a for u in mine for a in u.ann_ids}
    cats, brands = _categories(s, ann_ids)
    dates = sorted(u.date[:10] for u in mine if u.date)
    competitive = sum(1 for n in n_bidders if n >= 3)
    return {
        'kind': 'buyer', 'org': _name(s, buyer), 'org_id': buyer, **sm,
        'definition': f'采购单位「{_name(s, buyer)}」的{SCOPE_LABEL[req["scope"]]}采购画像。',
        'metrics': [
            {'label': '采购项目数', 'value': sm['project_count'], 'kind': 'count'},
            {'label': '已知成交总额', 'value': sm['known_amount'], 'kind': 'money'},
            {'label': '合作中标供应商', 'value': len(sup), 'kind': 'count'},
            {'label': '出现过的投标主体', 'value': len(bidder_cnt), 'kind': 'count'},
            {'label': '平均每单元投标家数', 'value': _avg(n_bidders), 'kind': 'num'},
            {'label': '充分竞争单元占比（≥3 家）', 'value': competitive / len(n_bidders) if n_bidders else None, 'kind': 'pct'},
            {'label': '第一大供应商份额', 'value': shares[0] if shares else None, 'kind': 'pct'},
            {'label': '供应商集中度 HHI', 'value': round(sum(x * x for x in shares), 4) if shares else None, 'kind': 'num'},
            {'label': '中标方平均得分', 'value': _avg(winner_scores), 'kind': 'num'},
            {'label': '中标价 / 其他报价均值', 'value': _avg(win_discount), 'kind': 'pct'},
            {'label': '首次公告', 'value': dates[0] if dates else None, 'kind': 'text'},
            {'label': '最近公告', 'value': dates[-1] if dates else None, 'kind': 'text'},
        ],
        'tags': _buyer_tags(sm, shares, n_bidders, competitive),
        'top_partners': [{'_key': k, 'name': _name(s, k), 'count': v['count'], 'amount': v['amount'],
                          'share': v['count'] / total_wins} for k, v in top_sup[:10]],
        'top_bidders': [{'_key': k, 'name': _name(s, k), 'count': v} for k, v in bidder_cnt.most_common(10)],
        'categories': cats, 'brands': brands,
        'trend': _trend(mine, lambda u: (s.projects.get(u.project_key) or u).amount),
        'evidence_refs': {k: sorted({a for u in mine if k in u.bidders for a in u.ann_ids}) for k in bidder_cnt},
    }


def _buyer_tags(sm: dict, shares: list[float], n_bidders: list[int], competitive: int) -> list[str]:
    tags = []
    if sm['project_count'] >= 5:
        tags.append('高频采购')
    if shares and shares[0] >= 0.5 and len(shares) >= 2:
        tags.append('供应商集中')
    elif len(shares) >= 4:
        tags.append('供应商分散')
    if n_bidders and competitive / len(n_bidders) >= 0.7:
        tags.append('竞争充分')
    elif n_bidders and sum(n_bidders) / len(n_bidders) < 2:
        tags.append('竞争信息少')
    if (sm['known_amount'] or 0) >= 1e7:
        tags.append('千万级采购')
    return tags


def supplier_profile(s: Store, req: dict, units: list[Unit]) -> dict:
    sp = norm_org(req.get('org') or '')
    joined = [u for u in units if sp in u.bidders]
    if not joined:
        raise LookupError(f'没有找到投标主体「{req.get("org")}」的投标记录')
    won = [u for u in joined if sp in u.winners]
    scores, ranks, ratio = [], [], []
    won_amount, won_unknown = None, 0
    for u in joined:
        bid = u.bids.get(sp) or {}
        if bid.get('score') is not None:
            scores.append(bid['score'])
        if bid.get('rank') is not None:
            ranks.append(bid['rank'])
        # 报价 / 该单元中标价：<1 说明报价低于中标价，>1 说明偏高
        wins = [u.bids[w]['amount'] for w in u.winners if (u.bids.get(w) or {}).get('amount')]
        if bid.get('amount') and len(wins) == 1 and sp not in u.winners:
            ratio.append(bid['amount'] / wins[0])
    for u in won:
        a = attributable(u)
        if a is None:
            won_unknown += 1
        else:
            won_amount = (won_amount or 0) + a
    buyers: dict[str, dict] = {}
    for u in joined:
        r = buyers.setdefault(u.buyer, {'bids': 0, 'wins': 0, 'amount': None})
        r['bids'] += 1
        if sp in u.winners:
            r['wins'] += 1
            a = attributable(u)
            if a is not None:
                r['amount'] = (r['amount'] or 0) + a
    rivals: dict[str, dict] = defaultdict(lambda: {'count': 0, 'beat': 0, 'lost': 0, 'anns': set()})
    for u in joined:
        # 答疑 A2：竞争对手按同一项目同一包统计，不同包各计一次
        for g in co_groups(s, u):
            if sp not in g.bidders:
                continue
            for b in g.bidders:
                if b == sp:
                    continue
                r = rivals[b]
                r['count'] += 1
                r['anns'].update(g.ann_ids)
                if sp in g.winners and b not in g.winners:
                    r['beat'] += 1
                elif b in g.winners and sp not in g.winners:
                    r['lost'] += 1
    ann_won = {a for u in won for a in u.ann_ids}
    cats, brands = _categories(s, ann_won, sp)
    win_rate = len(won) / len(joined)
    top_buyers = sorted(buyers.items(), key=lambda kv: (-kv[1]['wins'], -kv[1]['bids']))
    top_rivals = sorted(rivals.items(), key=lambda kv: -kv[1]['count'])
    dates = sorted(u.date[:10] for u in joined if u.date)
    return {
        'kind': 'supplier', 'org': _name(s, sp), 'org_id': sp, **summarize(s, won),
        'definition': f'「{_name(s, sp)}」的{SCOPE_LABEL[req["scope"]]}投标画像。',
        'metrics': [
            {'label': '参与投标', 'value': len(joined), 'kind': 'count'},
            {'label': '中标', 'value': len(won), 'kind': 'count'},
            {'label': '中标率', 'value': win_rate, 'kind': 'pct'},
            {'label': '已归属中标金额', 'value': won_amount, 'kind': 'money'},
            {'label': '金额未归属中标', 'value': won_unknown, 'kind': 'count'},
            {'label': '服务采购单位', 'value': sum(1 for v in buyers.values() if v['wins']), 'kind': 'count'},
            {'label': '平均综合得分', 'value': _avg(scores), 'kind': 'num'},
            {'label': '平均排名', 'value': _avg(ranks), 'kind': 'num'},
            {'label': '未中标时报价 / 中标价', 'value': _avg(ratio), 'kind': 'pct'},
            {'label': '竞争对手数', 'value': len(rivals), 'kind': 'count'},
            {'label': '首次出现', 'value': dates[0] if dates else None, 'kind': 'text'},
            {'label': '最近出现', 'value': dates[-1] if dates else None, 'kind': 'text'},
        ],
        'tags': _supplier_tags(len(joined), win_rate, buyers, won_amount),
        'top_partners': [{'_key': k, 'name': _name(s, k), 'count': v['wins'], 'bids': v['bids'], 'amount': v['amount']}
                         for k, v in top_buyers[:10]],
        'top_bidders': [{'_key': k, 'name': _name(s, k), 'count': v['count'], 'beat': v['beat'], 'lost': v['lost']}
                        for k, v in top_rivals[:10]],
        'categories': cats, 'brands': brands,
        'trend': _trend(won, attributable),
        'evidence_refs': {**{k: sorted(v['anns']) for k, v in rivals.items()},
                          **{k: sorted({a for u in joined if u.buyer == k for a in u.ann_ids}) for k in buyers}},
    }


def _supplier_tags(bids: int, win_rate: float, buyers: dict, won_amount: float | None) -> list[str]:
    tags = []
    if bids >= 5:
        tags.append('活跃投标')
    if bids >= 3 and win_rate >= 0.6:
        tags.append('高中标率')
    elif bids >= 3 and win_rate <= 0.2:
        tags.append('陪标/低中标率')
    wins_by = [v['wins'] for v in buyers.values() if v['wins']]
    if wins_by and max(wins_by) >= 2 and max(wins_by) / sum(wins_by) >= 0.6:
        tags.append('依赖单一客户')
    elif len(wins_by) >= 3:
        tags.append('客户多元')
    if (won_amount or 0) >= 1e7:
        tags.append('千万级中标')
    return tags


def run(kind: str, req: dict) -> dict:
    if kind not in ('buyer', 'supplier'):
        raise ValueError('kind 只能是 buyer 或 supplier')
    s = store(req.get('job_id'))
    t0 = time.perf_counter()
    req = {'scope': 'project', **{k: v for k, v in req.items() if v is not None}}
    if req['scope'] not in SCOPE_LABEL:
        raise ValueError('scope 只能是 project 或 package')
    fn = buyer_profile if kind == 'buyer' else supplier_profile
    out: dict[str, Any] = fn(s, req, units_for(s, req))
    return {**out, 'job_id': req.get('job_id'), 'scope_announcements': len(s.anns),
            'source_revision': f'sql:rev{s.revision}', 'query_ms': (time.perf_counter() - t0) * 1000}
