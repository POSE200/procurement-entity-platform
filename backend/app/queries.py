"""检索与五类关系场景（确定性计算，模型不参与统计）。

统计口径与前端示例模式一致（规划 2.1 节）：
- 业务项目 = 采购单位 + 项目编号（缺编号时退化为公告 ID）；项目数去重，包数另列。
- 项目金额只取公告总中标金额，跨公告按公告去重求和；null 不当 0，计入 unknown_count。
- 一个单元有多个中标方时，金额不重复计给每家，计入“金额未归属”。
关系数据来自 SQL 事实库的内存投影，按数据修订号缓存。

答疑口径（2026-10-07）：
- A1：资格性审查或符合性审查未通过的投标人不计为参与方。抽取结果原样保存（评测不受影响），
  只在关系投影时排除：原文明确写出“某主体 + 资格性/符合性审查未通过”等字样，且该条投标记录
  没有得分和排名时才排除；中标记录从不排除。
- A2：不同标段视为独立子项目，共同投标组合严格按“同一项目同一包”计算（见 co_groups）。
"""
from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import storage
from .extraction import norm_org


@dataclass
class Unit:
    key: str
    project_key: str
    buyer: str
    title: str
    date: str | None
    amount: float | None = None
    winners: set = field(default_factory=set)
    bidders: set = field(default_factory=set)
    ann_ids: list = field(default_factory=list)
    package_nos: list = field(default_factory=list)
    # 每个投标主体在本单元的竞标明细：{规范名: {'score','rank','amount'}}
    # 项目级单元中同一主体投多个包时：得分取最高、排名取最好、报价按包求和
    bids: dict = field(default_factory=dict)


def _num(v: Any) -> float | None:
    try:
        return None if v is None or v == '' else float(v)
    except (TypeError, ValueError):
        return None


def _add_bid(u: Unit, n: str, b: dict) -> None:
    score, rank, amount = _num(b.get('total_score')), _num(b.get('rank')), _num(b.get('amount'))
    cur = u.bids.get(n)
    if cur is None:
        u.bids[n] = {'score': score, 'rank': rank, 'amount': amount}
        return
    if score is not None and (cur['score'] is None or score > cur['score']):
        cur['score'] = score
    if rank is not None and (cur['rank'] is None or rank < cur['rank']):
        cur['rank'] = rank
    if amount is not None:
        cur['amount'] = (cur['amount'] or 0) + amount


@dataclass
class Store:
    revision: int
    anns: list[dict]
    ann_by_id: dict[str, dict]
    items: list[dict]
    display: dict[str, str]
    projects: dict[str, Unit]
    packages: dict[str, Unit]
    orgs: list[dict]
    # project_key -> 该项目下的同包单元（共同投标只在同一包内成立）
    package_units_by_project: dict[str, list[Unit]] = field(default_factory=dict)
    # announcement_id -> 因资格性/符合性审查未通过而不计为参与方的主体（规范名）
    disqualified: dict[str, set] = field(default_factory=dict)


def co_groups(s: Store, u: Unit) -> list[Unit]:
    """返回判断“共同投标”时使用的同包单元。

    同包级单元本身就是一个组；项目级单元拆成其下各包。若项目没有可识别的包单元
    （例如只有一个包且无包号），整个项目就是唯一的包。
    """
    if u.key != u.project_key:
        return [u]
    groups = s.package_units_by_project.get(u.project_key) or []
    # 有投标主体无法归入任何包（多包公告但该条记录缺包号）时无法可靠拆分，退回整个项目
    if not groups or u.bidders - {b for g in groups for b in g.bidders}:
        return [u]
    return groups


_FAIL_PHRASE = (r'(?:(?:资格性?|符合性)(?:审查|检查|评审)?(?:未通过|不通过|不合格)'
                r'|(?:未通过|不通过)(?:资格性?|符合性)(?:审查|检查|评审)?'
                r'|无效投标|投标无效|无效响应|响应无效|否决其投标|被否决)')
# 名称与失败原因之间的文字不能跨句，也不能夹着另一个机构名
_ORG_SUFFIX = re.compile(r'公司|中心|事务所|研究院|研究所|合作社|经营部|商行|门市部|工作室')


def _flat(text: str) -> str:
    return re.sub(r'\s+', '', (text or '').replace('（', '(').replace('）', ')'))


_PKG_HEADER = re.compile(r'(?:合同包|采购包|标段|标包|包组|包)([0-9A-Za-z一二三四五六七八九十]{1,3})'
                         r'|([0-9A-Za-z一二三四五六七八九十]{1,3})(?:包|标段)')
_CN_NUM = {c: str(i) for i, c in enumerate('一二三四五六七八九十', 1)}


def _header_key(token: str) -> str | None:
    from .evaluation import _package_key
    return _package_key(''.join(_CN_NUM.get(ch, ch) for ch in token))


def _fail_positions(flat: str, name: str) -> list[int]:
    """原文明确写出该主体未通过资格性/符合性审查（或投标无效）的位置。"""
    if not name or name not in flat:
        return []
    esc = re.escape(name)
    out = [m.start() for m in re.finditer(esc + r'(?:通过)?(?:不通过|未通过|不合格)', flat)]  # 评审表“通过 不通过”列
    for m in re.finditer(esc + r'([^。；;]{0,40}?)' + _FAIL_PHRASE, flat):
        if not _ORG_SUFFIX.search(m.group(1)):
            out.append(m.start())
    return out


def _failed_review(flat: str, name: str, package_no: str | None = None, multi_package: bool = False,
                   records_in_ann: int = 1) -> bool:
    """该条投标记录是否属于“未通过资格性/符合性审查”。

    单包公告：原文有明确的未通过表述即可。多包公告：同一主体可能在一个包未通过、在另一个包正常
    参与评审，因此要求未通过表述之前最近的包标题与本条记录的包号一致；找不到包标题时，
    只有该主体在本公告中仅有一条投标记录才认定。
    """
    positions = _fail_positions(flat, name)
    if not positions:
        return False
    if not multi_package:
        return True
    from .evaluation import _package_key
    mine = _package_key(package_no)
    for pos in positions:
        headers = list(_PKG_HEADER.finditer(flat, 0, pos))
        if not headers:
            if records_in_ann == 1:
                return True
            continue
        last = headers[-1]
        if mine is not None and _header_key(last.group(1) or last.group(2)) == mine:
            return True
    return False


_cache: Store | None = None
_scope_cache: dict[str, Store] = {}


def _ann_row(r: dict) -> dict:
    return {'announcement_id': r['announcement_id'], 'title': r['title'], 'publish_time': r['publish_time'],
            'project_no': r['project_no'], 'buyer': r['buyer'], 'project_name': r['project_name'],
            'job_id': r.get('job_id'),
            'total_amount': r['total_amount'], 'packages': storage.loads(r['packages_json'], []),
            'bidders': storage.loads(r['bidders_json'], []), 'item_count': r.get('item_count', 0),
            'has_attachment': bool(r['has_attachment']), 'gold_source': None}


def _add(m: dict[str, Unit], key: str, pk: str, a: dict, amount: float | None) -> Unit:
    u = m.get(key)
    if not u:
        u = m[key] = Unit(key, pk, norm_org(a['buyer'] or '未知采购单位'), a['project_name'] or a['title'], a['publish_time'])
    if a['announcement_id'] not in u.ann_ids:
        u.ann_ids.append(a['announcement_id'])
        if amount is not None:
            u.amount = (u.amount or 0) + amount
    if a['publish_time'] and (not u.date or a['publish_time'] < u.date):
        u.date = a['publish_time']
    return u


def store(job_id: str | None = None) -> Store:
    global _cache
    rev = storage.revision()
    if job_id and not storage.one('SELECT id FROM jobs WHERE id=?', (job_id,)):
        raise ValueError('任务不存在，请重新选择任务范围')
    cached = _scope_cache.get(job_id) if job_id else _cache
    if cached and cached.revision == rev:
        return cached
    sql = """SELECT a.*, (SELECT COUNT(*) FROM items i WHERE i.announcement_id=a.announcement_id) AS item_count
             FROM announcements a"""
    params = ()
    if job_id:
        sql += " WHERE a.announcement_id IN (SELECT announcement_id FROM job_tasks WHERE job_id=? AND status IN ('succeeded','skipped'))"
        params = (job_id,)
    raw = storage.rows(sql+' ORDER BY a.publish_time DESC', params)
    anns = [_ann_row(r) for r in raw]
    ann_by_id = {a['announcement_id']: a for a in anns}
    items = storage.rows("SELECT * FROM items WHERE announcement_id IN (SELECT announcement_id FROM job_tasks WHERE job_id=? AND status IN ('succeeded','skipped'))", (job_id,)) if job_id else storage.rows('SELECT * FROM items')
    for it in items:
        a = ann_by_id.get(it['announcement_id'], {})
        it.update(buyer=a.get('buyer'), project_name=a.get('project_name'), publish_time=a.get('publish_time'),
                  job_id=a.get('job_id'))
    display: dict[str, str] = {}
    org: dict[str, dict] = {}

    def stat(name: str) -> dict:
        n = norm_org(name)
        display.setdefault(n, name)
        return org.setdefault(n, {'org_id': n, 'name': name, 'as_buyer': 0, 'as_winner': 0, 'as_participant': 0})

    projects: dict[str, Unit] = {}
    packages: dict[str, Unit] = {}
    disqualified: dict[str, set] = {}
    texts = {r['announcement_id']: _flat((r.get('summary_text') or '') + '\n' + (r.get('body_text') or ''))
             for r in raw}
    for a in anns:
        pk = f"{norm_org(a['buyer'] or '未知采购单位')}|{a['project_no'] or a['announcement_id']}"
        if a['buyer']:
            stat(a['buyer'])['as_buyer'] += 1
        proj = _add(projects, pk, pk, a, a['total_amount'])
        for p in a['packages']:
            if p.get('package_no') and p['package_no'] not in proj.package_nos:
                proj.package_nos.append(p['package_no'])
        single = len(a['packages']) <= 1
        flat = texts.get(a['announcement_id'], '')
        multi = len(a['packages']) > 1 or len({b.get('package_no') for b in a['bidders']}) > 1
        records = Counter(norm_org(b['name']) for b in a['bidders'])
        for b in a['bidders']:
            n = norm_org(b['name'])
            # 答疑 A1：未通过资格性/符合性审查的投标人不计为参与方（有得分/排名说明进入了评审，保留）
            if (b['role'] != 'winner' and _num(b.get('total_score')) is None and _num(b.get('rank')) is None
                    and _failed_review(flat, n, b.get('package_no'), multi, records[n])):
                disqualified.setdefault(a['announcement_id'], set()).add(n)
                continue
            stat(b['name'])['as_winner' if b['role'] == 'winner' else 'as_participant'] += 1
            proj.bidders.add(n)
            if b['role'] == 'winner':
                proj.winners.add(n)
            _add_bid(proj, n, b)
            pno = b.get('package_no') or ((a['packages'][0].get('package_no') if a['packages'] else None) or '1' if single else None)
            if pno is None:
                continue
            pu = _add(packages, f'{pk}|{pno}', pk, a, a['total_amount'] if single else None)
            if pno not in pu.package_nos:
                pu.package_nos.append(pno)
            pu.bidders.add(n)
            if b['role'] == 'winner':
                pu.winners.add(n)
            _add_bid(pu, n, b)
    orgs = sorted(org.values(), key=lambda o: -(o['as_buyer'] + o['as_winner']))
    by_project: dict[str, list[Unit]] = {}
    for u in packages.values():
        by_project.setdefault(u.project_key, []).append(u)
    result = Store(rev, anns, ann_by_id, items, display, projects, packages, orgs, by_project, disqualified)
    if job_id:
        for key in list(_scope_cache):
            if _scope_cache[key].revision != rev: _scope_cache.pop(key, None)
        if len(_scope_cache) >= 16: _scope_cache.pop(next(iter(_scope_cache)), None)
        _scope_cache[job_id] = result
    else:
        _cache = result
    return result


# ---------------- 标的物 / 公告 / 机构 ----------------
def _has(v: Any, q: str) -> bool:
    return bool(v) and q in str(v).lower()


def filter_items(q: dict) -> list[dict]:
    s = store()
    kw, buyer, sup, brand = (str(q.get(k) or '').strip().lower() for k in ('q', 'buyer', 'supplier', 'brand'))
    aid = str(q.get('announcement_id') or '').strip().lower()
    job_id = str(q.get('job_id') or '').strip()
    task_ids = {r['announcement_id'] for r in storage.rows(
        "SELECT announcement_id FROM job_tasks WHERE job_id=? AND status IN ('succeeded','skipped')", (job_id,))} if job_id else None
    out = []
    for it in s.items:
        if kw and not any(_has(it.get(k), kw) for k in ('item_name', 'model', 'category', 'project_name', 'announcement_id')):
            continue
        if aid and not _has(it.get('announcement_id'), aid):
            continue
        if task_ids is not None and it['announcement_id'] not in task_ids:
            continue
        if buyer and not _has(it.get('buyer'), buyer):
            continue
        if sup and not _has(it.get('supplier'), sup):
            continue
        if brand and not _has(it.get('brand'), brand):
            continue
        d = (it.get('publish_time') or '')[:10]
        if q.get('date_from') and (not d or d < q['date_from']):
            continue
        if q.get('date_to') and (not d or d > q['date_to']):
            continue
        out.append(it)
    sort = q.get('sort')
    if sort:
        desc = sort.startswith('-')
        key = sort.lstrip('-')
        present = [r for r in out if r.get(key) is not None]
        missing = [r for r in out if r.get(key) is None]  # 空值始终排在最后
        present.sort(key=lambda r: r[key], reverse=desc)
        out = present + missing
    return out


ITEM_FIELDS = ['item_id', 'announcement_id', 'package_no', 'item_name', 'category', 'brand', 'model', 'unit_price',
               'quantity', 'unit', 'total_price', 'supplier', 'buyer', 'project_name', 'publish_time', 'job_id']


def item_out(it: dict) -> dict:
    return {k: it.get(k) for k in ITEM_FIELDS}


def announcement(aid: str) -> dict | None:
    r = storage.one('SELECT * FROM announcements WHERE announcement_id=?', (aid,))
    if not r:
        return None
    items = storage.rows('SELECT * FROM items WHERE announcement_id=? ORDER BY rowid', (aid,))
    a = _ann_row({**r, 'item_count': len(items)})
    sources = storage.loads(r['sources_json'], {})
    placeholder = sources.get('processing_placeholder', False)
    note_parts = ['处理状态：未解析占位，计0分'] if placeholder else [f"模型：{r['model']}，抽取于 {r['extracted_at']}"]
    if r['review_note']:
        note_parts.append(r['review_note'] if placeholder else '待复核：' + r['review_note'])
    if sources.get('attachments_used'):
        note_parts.append('使用附件：' + '、'.join(d['name'] for d in sources['attachments_used']))
    if sources.get('attachments_skipped'):
        note_parts.append('未解析附件：' + '、'.join(sources['attachments_skipped'][:10]))
    return {**a, 'summary_text': r['summary_text'] or '', 'body_text': r['body_text'] or '',
            'review_note': '\n'.join(note_parts), 'annotated_at': r['extracted_at'],
            'items': [item_out({**it, 'buyer': a['buyer'], 'project_name': a['project_name'],
                                'publish_time': a['publish_time'], 'job_id': a['job_id']}) for it in items]}


def announcements(q: str, limit: int = 50, job_id: str | None = None) -> list[dict]:
    kw = q.strip().lower()
    s = store(job_id)
    lst = [a for a in s.anns if not kw or _has(a['title'], kw) or _has(a['announcement_id'], kw) or _has(a['buyer'], kw)]
    return lst[:limit]


def organizations(q: str, role: str, limit: int = 30, job_id: str | None = None) -> list[dict]:
    kw = norm_org(q).lower()
    if role == 'bidder':
        # 任何投标主体（中标或未中标），供应商画像使用
        val = lambda o: o['as_winner'] + o['as_participant']  # noqa: E731
    else:
        key = 'as_buyer' if role == 'buyer' else 'as_winner'
        val = lambda o: o[key]  # noqa: E731
    lst = [o for o in store(job_id).orgs if val(o) > 0 and (not kw or kw in o['org_id'].lower())]
    return sorted(lst, key=lambda o: -val(o))[:limit]
