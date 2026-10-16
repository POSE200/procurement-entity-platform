"""按赛题《输出标准样例表.xlsx》导出提取结果（总表 / 标的表 / 投标人表）。

以官方样例表为模板：保留表头（第 1 行）、填写说明（第 2 行）、填写示例（第 3 行）及全部样式，
删除“示例数据”行后从第 4 行起写入数据，
确保列顺序、表头文字、蓝色必填字段与样例完全一致，便于组委会脚本自动评估。

口径（依据样例表的填写说明与示例数据）：
- UUID：未分包公告直接用公告 ID；多包公告按包顺序在公告 ID 后加 _01、_02…（与样例一致）。
- 标段/合同包：优先填包名称，没有名称时填原文包号；单包且原文无包名/包号的填“未分包”。
- 金额统一换算为万元：总表中标金额、投标人报价保留两位小数（四舍五入）；
  标的表单价、总价按万元保留 4 位小数（样例中 0.0055、23.622 等即保留原精度）。
- 数量：照录原文，整数去掉多余的“.0”。
- 标的表“类别”：结果公告中提取的标的记“中标”（与样例示例数据一致）。
- 投标人类别：中标人 / 参与人；资格性或符合性审查未通过的记“其他”，通过审查列写“未通过，原因”。
- URL：公告 HTML 不含源地址，按页面导航（中央/地方 × 中标/成交）与公告 ID 中的日期复原中国政府采购网地址。
"""
from __future__ import annotations

import io
import re
from copy import copy
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from . import storage
from .evaluation import _package_key

TEMPLATE = Path(__file__).resolve().parent / 'templates' / '输出标准样例表.xlsx'
SHEETS = ('总表', '标的表', '投标人表')
DATA_START = 4  # 第 1 行表头，第 2 行填写说明，第 3 行填写示例（与样例表一致）
# 样例表之外、组委会后续通知改为灰色（非标准、不计入提取率与精度）的字段：工作表 → 列号
GREY_FIELDS = {'总表': (2,)}  # B 列 URL

# 页面导航 → URL 栏目段
_CHANNEL = {('中央公告', '中标公告'): 'zygg/zbgg', ('中央公告', '成交公告'): 'zygg/cjgg',
            ('地方公告', '中标公告'): 'dfgg/zbgg', ('地方公告', '成交公告'): 'dfgg/cjgg'}
_CRUMB_RE = re.compile(r'当前位置：(.*?)</p>', re.S)
_CRUMB_TITLE_RE = re.compile(r'title="([^"]+)" class="CurrChnlCls"')
_ID_RE = re.compile(r't(\d{6})\d{2}_\d+')
_FAIL_REASON_RE = r'([^。；;\n]{0,40})'


def channel_from_html(raw: bytes | str) -> str | None:
    """从公告 HTML 的“当前位置”导航读出栏目（如 dfgg/zbgg）。"""
    s = raw.decode('utf-8', errors='ignore') if isinstance(raw, bytes) else raw
    m = _CRUMB_RE.search(s)
    if not m:
        return None
    t = _CRUMB_TITLE_RE.findall(m.group(1))
    level = next((x for x in t if x in ('中央公告', '地方公告')), None)
    kind = next((x for x in t if x in ('中标公告', '成交公告')), None)
    return _CHANNEL.get((level, kind))


def source_url(aid: str, channel: str | None) -> str | None:
    m = _ID_RE.fullmatch(aid)
    if not m:
        return None
    return f'https://www.ccgp.gov.cn/cggg/{channel or "dfgg/zbgg"}/{m.group(1)}/{aid}.htm'


def _wan(v: Any, places: int) -> float | None:
    """元 → 万元，四舍五入到指定小数位；空值保持为空。"""
    if v is None or v == '':
        return None
    try:
        d = Decimal(str(v)) / Decimal(10000)
    except Exception:  # noqa: BLE001
        return None
    q = d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return float(q.normalize()) if places > 2 else float(q)


def _qty(v: Any) -> str | None:
    if v is None or v == '':
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else f'{f:g}'


def _round2(v: Any) -> float | None:
    if v is None or v == '':
        return None
    try:
        return float(Decimal(str(v)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    except Exception:  # noqa: BLE001
        return None


class _Packages:
    """一篇公告的包清单：给出 UUID、标段名称，并把明细/投标记录的包号映射到对应包。"""

    def __init__(self, aid: str, packages: list[dict], items: list[dict], bidders: list[dict]):
        pk = [p for p in packages if p.get('package_no') or p.get('package_name') or p.get('winner')]
        # 明细/投标里出现、但 packages 中没有的包号也补上，保证每条记录都能落到一个包
        keys = [_package_key(p.get('package_no')) for p in pk]
        for row in [*items, *bidders]:
            k = _package_key(row.get('package_no'))
            if k is not None and k not in keys:
                pk.append({'package_no': row.get('package_no'), 'package_name': None, 'winner': None, 'amount': None})
                keys.append(k)
        if not pk:
            pk = [{'package_no': None, 'package_name': None, 'winner': None, 'amount': None}]
            keys = [None]
        self.aid, self.packages, self.keys = aid, pk, keys
        self.multi = len(pk) > 1

    def uuid(self, i: int) -> str:
        return f'{self.aid}_{i + 1:02d}' if self.multi else self.aid

    def label(self, i: int) -> str:
        p = self.packages[i]
        name, no = (p.get('package_name') or '').strip(), (p.get('package_no') or '').strip()
        if name:
            return name
        if no:
            return no if not no.isdigit() else f'第{no}包'
        return None if self.multi else '未分包'

    def index_of(self, package_no: Any) -> int:
        k = _package_key(package_no)
        if k is not None and k in self.keys:
            return self.keys.index(k)
        if not self.multi:
            return 0
        raise ValueError('多包记录的包号缺失或无法关联，请先复核包归属后再导出')


_REASON_HEAD = re.compile(r'^(?:[，,：:。、\s]|原因是|原因为|原因|理由是|理由为|理由)+')
# 原因文字的结束位置：编号小节（“九、”“1.”）或句末标点
_REASON_STOP = re.compile(r'[一二三四五六七八九十]+、|\d+[.、]|[。；;]')


def _review_status(flat: str, name: str, failed: bool, others: list[str] = ()) -> str:
    """通过审查列：通过 / 未通过，原因。原因照录原文，截到下一个投标主体名称或下一小节之前。"""
    if not failed:
        return '通过'
    from .queries import _FAIL_PHRASE, _flat
    n = re.escape(_flat(name))
    m = re.search(n + _FAIL_REASON_RE + _FAIL_PHRASE, flat)
    reason = ''
    if m:
        tail = _REASON_HEAD.sub('', flat[m.end():m.end() + 80])
        cut = len(tail)
        for o in others:
            j = tail.find(_flat(o)) if o else -1
            if 0 <= j < cut:
                cut = j
        stop = _REASON_STOP.search(tail, 0, cut)
        if stop:
            cut = stop.start()
        reason = tail[:min(cut, 40)].strip('，,：:。 ')
    return f'未通过，{reason}' if reason else '未通过'


def collect(job_id: str | None = None, announcement_ids: list[str] | None = None) -> dict[str, list[list]]:
    """从事实库生成三张表的数据行（不含“序号”列以外的样式）。"""
    from .queries import _failed_review, _flat
    sql = 'SELECT * FROM announcements'
    params: list = []
    if job_id:
        sql += " WHERE announcement_id IN (SELECT announcement_id FROM job_tasks WHERE job_id=? AND status IN ('succeeded','skipped'))"
        params.append(job_id)
    sql += ' ORDER BY announcement_id'
    anns = storage.rows(sql, params)
    if announcement_ids:
        want = set(announcement_ids)
        anns = [a for a in anns if a['announcement_id'] in want]
    datasets = {d['id']: d for d in storage.rows('SELECT id, html_zip FROM datasets')}
    out = {s: [] for s in SHEETS}
    for a in anns:
        aid = a['announcement_id']
        packages = storage.loads(a['packages_json'], [])
        bidders = storage.loads(a['bidders_json'], [])
        items = storage.rows('SELECT * FROM items WHERE announcement_id=? ORDER BY rowid', (aid,))
        P = _Packages(aid, packages, items, bidders)
        def linked_index(package_no):
            try:
                return P.index_of(package_no)
            except ValueError:
                return None

        def row_identity(package_no):
            index = linked_index(package_no)
            # 无法确认包归属的记录保留公告UUID与空包字段，另由复核记录说明。
            return (P.uuid(index), P.label(index)) if index is not None else (aid, None)
        sources = storage.loads(a.get('sources_json'), {}) or {}
        channel = sources.get('channel') or _channel_for(datasets.get(a['dataset_id']), aid)
        url = source_url(aid, channel)
        project = a['project_name'] or a['title']
        # 总表：每个包一行；单包且包金额缺失时用公告总金额
        for i, p in enumerate(P.packages):
            amount = p.get('amount')
            if amount is None and not P.multi:
                amount = a['total_amount']
            winner = p.get('winner') or next((b['name'] for b in bidders if b.get('role') == 'winner'
                                              and linked_index(b.get('package_no')) == i), None)
            out['总表'].append([url, P.uuid(i), P.label(i), project, a['buyer'], winner, _wan(amount, 2)])
        # 标的表
        if sources.get('processing_placeholder') and not items:
            out['标的表'].append([aid, None, None, None, None, None, None, None, None, None])
        for it in items:
            row_uuid, package_label = row_identity(it.get('package_no'))
            out['标的表'].append([row_uuid, package_label, it['item_name'], it['category'], it['brand'], it['model'],
                                 _qty(it['quantity']), _wan(it['unit_price'], 4), _wan(it['total_price'], 4), '中标'])
        # 投标人表：中标人在前，其余按排名/得分
        flat = _flat((a.get('summary_text') or '') + '\n' + (a.get('body_text') or ''))
        multi_pk = P.multi or len({b.get('package_no') for b in bidders}) > 1
        from collections import Counter
        from .extraction import norm_org
        recs = Counter(norm_org(b['name']) for b in bidders)

        def order(b):
            rk = b.get('rank')
            index = linked_index(b.get('package_no'))
            return (index if index is not None else len(P.packages), 0 if b.get('role') == 'winner' else 1,
                    rk if isinstance(rk, (int, float)) else 999, -(b.get('total_score') or 0))
        for b in sorted(bidders, key=order):
            row_uuid, package_label = row_identity(b.get('package_no'))
            n = norm_org(b['name'])
            failed = (b.get('role') != 'winner' and b.get('total_score') is None and b.get('rank') is None
                      and _failed_review(flat, n, b.get('package_no'), multi_pk, recs[n]))
            kind = '中标人' if b.get('role') == 'winner' else ('其他' if failed else '参与人')
            out['投标人表'].append([row_uuid, package_label, b['name'], kind, _review_status(flat, b['name'], failed, [x['name'] for x in bidders if x['name'] != b['name']]),
                                  _wan(b.get('amount'), 2), _round2(b.get('total_score')), None])
    return out


_channel_cache: dict[tuple[str, str], str | None] = {}


def _channel_for(dataset: dict | None, aid: str) -> str | None:
    if not dataset:
        return None
    key = (dataset['html_zip'], aid)
    if key not in _channel_cache:
        from . import ingestion
        try:
            _channel_cache[key] = channel_from_html(ingestion.read_html(dataset, aid))
        except Exception:  # noqa: BLE001 — 原始包不可读时退回默认栏目
            _channel_cache[key] = None
    return _channel_cache[key]


def _clone_style(src, dst) -> None:
    dst.font, dst.border, dst.fill = copy(src.font), copy(src.border), copy(src.fill)
    dst.number_format, dst.alignment, dst.protection = src.number_format, copy(src.alignment), copy(src.protection)


def build_workbook(data: dict[str, list[list]]) -> bytes:
    """套用官方样例表模板写入数据，返回 xlsx 字节。"""
    wb = load_workbook(TEMPLATE)
    for name in SHEETS:
        ws = wb[name]
        width = ws.max_column
        # 数据行样式取样例第 4 行（第一条示例数据，白底宋体），表头与说明行原样保留
        style_row = [ws.cell(4, c) for c in range(1, width + 1)]
        styles = [(copy(c.font), copy(c.border), copy(c.fill), c.number_format, copy(c.alignment)) for c in style_row]
        height = ws.row_dimensions[4].height
        ws.delete_rows(DATA_START, ws.max_row - DATA_START + 1)
        for r, values in enumerate(data[name], start=DATA_START):
            row = [r - DATA_START + 1, *values]  # 序号
            for c, v in enumerate(row, start=1):
                cell = ws.cell(r, c, v)
                f, b, fill, fmt, al = styles[c - 1]
                cell.font, cell.border, cell.fill, cell.alignment = copy(f), copy(b), copy(fill), copy(al)
                cell.number_format = fmt
            if height:
                ws.row_dimensions[r].height = height
        # 组委会补充通知：总表 B 列 URL 改为灰色字段（非标准必填），表头填充改用样例中灰色字段（“序号”列）的样式
        for col in GREY_FIELDS.get(name, ()):
            ws.cell(1, col).fill = copy(ws.cell(1, 1).fill)
        ws.freeze_panes = 'A4'
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export(job_id: str | None = None, announcement_ids: list[str] | None = None) -> tuple[bytes, dict[str, int]]:
    data = collect(job_id, announcement_ids)
    return build_workbook(data), {k: len(v) for k, v in data.items()}
