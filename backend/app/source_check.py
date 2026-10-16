"""首轮抽取结果的原文核验：只拿模型输出回到公告原文/附件文字里查依据，标出可疑字段。

本模块只查错、只定位，不产生任何字段值；不读取标准答案。可疑字段交给模型定点重抽，
最终值仍由模型给出（见 docs：方案一 原文核验 + 定点重抽）。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

ITEM_FIELDS = ('item_name', 'category', 'brand', 'model', 'unit_price', 'quantity', 'total_price')
NUMERIC = ('unit_price', 'quantity', 'total_price')

# 原文中数字前若紧挨着这些词，说明它是预算/限价而不是成交价
BUDGET_WORDS = re.compile(r'(预算|最高限价|控制价|拦标价|限价|采购预算|预算金额|最高投标限价)[^0-9，。；\n]{0,12}$')
NUM_TOKEN = re.compile(r'(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*[（(]?\s*(万)?')
QUOTE_SPLIT = re.compile(r'[\n|｜\t ]+')
WAN_PRICE_HEADER = re.compile(r'(单价|总价|合价)\s*[(（]\s*万\s*元\s*[)）]')
NOT_BRANDS = {'国产', '进口', '国产品牌', '无', '/', '-', '详见附件', '见附件', '自主品牌', '定制', '其他'}
# 名称开头带包号/标段号，如“第1包：”“一标段：”“A包 ”
PKG_PREFIX = re.compile(r'^\s*(第?[一二三四五六七八九十\d]+\s*(包|标段|标包|分包)|[A-Za-z]\s*包)\s*[：:、\-—\s]')
# 名称是“项目名 + 第N包/标段”之类的包标题
PKG_SUFFIX = re.compile(r'[(（]?\s*(第?[一二三四五六七八九十\d]+\s*(包|标段|标包|分包)|[A-Za-z]\s*包)\s*[)）]?\s*$')

# 只定位明示包号及其标签，候选值交给模型核验；程序不据此生成字段值。
PACKAGE_ID_LABEL = re.compile(
    r'(?:项目包号|采购包号|合同包号|标包编号|标段编号|包号|标包|采购包|合同包|包别)\s*[:：]?\s*'
    r'([A-Za-z0-9一二三四五六七八九十][A-Za-z0-9_./-]{0,100})')


def package_id_mentions(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(0)) for m in PACKAGE_ID_LABEL.finditer(text or '')]


def _package_identity_flags(result: dict, src: 'SourceIndex') -> list[dict]:
    from .evaluation import _package_key

    mentions = package_id_mentions(src.text)
    values = {_package_key(value) for value, _ in mentions}
    flags = []
    for i, package in enumerate(result.get('packages') or []):
        old = package.get('package_no')
        role_error = old is not None and not re.search(r'[0-9A-Za-z一二三四五六七八九十]', str(old))
        needs_alias = (old is not None and not package.get('source_aliases') and len(values) > 1
                       and any(len(str(v)) > 16 for v, _ in mentions))
        if role_error or (mentions and _package_key(old) not in values) or needs_alias:
            flags.append({'row': None, 'scope': 'package_identity', 'package_index': i,
                          'field': 'package_no', 'code': 'package_identity_mismatch',
                          'detail': '包号与原文明示的包号标签不一致，需区分项目编号与项目包号',
                          'source_snippets': list(dict.fromkeys(quote for _, quote in mentions))[:18]})
    return flags


def _package_link_flags(result: dict) -> list[dict]:
    """核验明细/投标主体是否连接到已声明的包；关联值仍交给模型决定。"""
    from .evaluation import _package_key
    packages = result.get('packages') or []
    if not packages:
        return []
    known = {_package_key(p.get('package_no')) for p in packages}
    flags = []
    for collection in ('items', 'bidders'):
        for i, row in enumerate(result.get(collection) or []):
            key = _package_key(row.get('package_no'))
            if key in known and (key is not None or len(packages) == 1):
                continue
            code = 'package_link_missing' if key is None else 'package_link_orphan'
            flags.append({'row': i, 'collection': collection, 'scope': 'package_link',
                          'field': 'package_no', 'code': code,
                          'detail': '记录包号为空或未对应到已声明包，需按原文确认归属'})
    return flags


def norm_text(s: Any) -> str:
    """去掉空白与常见标点差异，用于原文包含判断。"""
    s = str(s or '')
    s = s.replace('（', '(').replace('）', ')').replace('：', ':').replace('，', ',')
    s = re.sub(r'[\s"“”\'‘’《》「」【】]', '', s)
    return s.lower()


def wan_price_rows(text: str) -> list[dict]:
    """保留价格列表头与数据行，用于提出单位核对问题。"""
    rows = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if '|' not in line or not WAN_PRICE_HEADER.search(line):
            continue
        headers = line.strip().strip('|').split('|')
        columns = {j: ('unit_price' if '单价' in cell else 'total_price')
                   for j, cell in enumerate(headers) if WAN_PRICE_HEADER.search(cell)}
        for following in lines[i + 1:]:
            if '|' not in following:
                break
            cells = following.strip().strip('|').split('|')
            if len(cells) != len(headers):
                break
            if all(not cell.strip(' :-') for cell in cells):
                continue
            for j, field in columns.items():
                values = [float(m.group(1).replace(',', '')) for m in NUM_TOKEN.finditer(cells[j])]
                if values:
                    rows.append({'field': field, 'values': values, 'header': line, 'row': following})
    return rows


class SourceIndex:
    """把一篇公告的全部可读文字建成便于查找的索引。"""

    def __init__(self, text: str, unreadable_images: int = 0):
        self.text = text or ''
        self.flat = norm_text(self.text)
        self.unreadable_images = unreadable_images
        self.image_names: list[str] = []
        self.inventory_tables: list[dict] = []
        self.candidate_items: list[dict] = []
        self.announcement_text = self.text
        self.wan_price_rows = wan_price_rows(self.text)
        # 每个数字出现的位置及其前文，用于判断是否只以“预算/限价”身份出现
        self.numbers: list[tuple[float, bool]] = []
        for m in NUM_TOKEN.finditer(self.text):
            try:
                v = float(m.group(1).replace(',', ''))
            except ValueError:
                continue
            before = self.text[max(0, m.start() - 20):m.start()]
            is_budget = bool(BUDGET_WORDS.search(before))
            self.numbers.append((v, is_budget))
            if m.group(2):  # “122.2263万元” 同时记录换算成元的值
                self.numbers.append((v * 10000, is_budget))
        for row in self.wan_price_rows:
            self.numbers.extend((v * 10000, False) for v in row['values'])
        for m in re.finditer(r'(?:单价|总价|合价)\s*[(（]\s*万元\s*[)）]\s*[:：]?\s*([\d,]+(?:\.\d+)?)', self.text):
            self.numbers.append((float(m.group(1).replace(',', '')) * 10000, False))

    def contains(self, s: Any) -> bool:
        n = norm_text(s)
        return bool(n) and n in self.flat

    def coverage(self, s: Any) -> float:
        """字段文字中能在原文找到的最长连续片段占比（0~1）。"""
        n = norm_text(s)
        if not n:
            return 1.0
        if n in self.flat:
            return 1.0
        # 先用前后各取一段定位候选区域，避免对超长原文做全量比对
        best = 0
        step = max(2, len(n) // 4)
        for start in range(0, len(n), step):
            piece = n[start:start + max(2, step)]
            pos = self.flat.find(piece)
            while pos != -1 and best < len(n):
                window = self.flat[max(0, pos - len(n)):pos + 2 * len(n)]
                m = SequenceMatcher(None, n, window, autojunk=False).find_longest_match(0, len(n), 0, len(window))
                best = max(best, m.size)
                pos = self.flat.find(piece, pos + 1)
                if pos > 0 and best >= len(n) * 0.9:
                    break
        return best / len(n)

    def quote_found(self, quote: Any) -> bool:
        """引文可核实：引文中 ≥90% 的字符能由原文里逐字存在的片段（每段 ≥3 字）覆盖。

        OCR 文字和表格单元格在模型引文里常被换行、去掉竖线或拼成一行，整段比对会误判；
        分片覆盖仍要求每个片段真实存在于原文，编造的句子无法通过。
        """
        # 按换行/竖线切段，去掉“/”“-”这类占位单元格；每段内部再做分片覆盖
        segs = [norm_text(s) for s in QUOTE_SPLIT.split(str(quote or ''))]
        segs = [s for s in segs if s.strip('/-—') and len(s) >= 2]
        if not segs:
            return False
        total = covered = longest = 0
        for q in segs:
            total += len(q)
            i = 0
            while i < len(q):
                j = i + 1
                while j <= len(q) and q[i:j] in self.flat:
                    j += 1
                size = j - 1 - i
                # 短段（如“米”“国产”）整段存在即可；长段只认 ≥3 字片段，防止用常见字拼凑
                if size >= 3 or (size == len(q) and size >= 2):
                    covered += size
                    longest = max(longest, size)
                    i += size
                else:
                    i += 1
        return covered / total >= 0.9 and longest >= min(total, 4)

    def number_hits(self, v: float) -> tuple[int, int]:
        """返回 (原文中等于 v 的出现次数, 其中以预算/限价身份出现的次数)。"""
        tol = max(0.01, abs(v) * 0.001)
        hits = [b for x, b in self.numbers if abs(x - v) <= tol]
        return len(hits), sum(hits)

    def money_quote_supports(self, field: str, value: float, quote: str) -> bool:
        text = norm_text(quote)
        for row in self.wan_price_rows:
            if row['field'] != field or norm_text(row['row']) not in text:
                continue
            if any(abs(raw * 10000 - value) < max(0.01, abs(value) * 1e-6) for raw in row['values']):
                return True
        return False

    def category_quote_supports(self, value: str, quote: str) -> bool:
        quoted = norm_text(quote).strip('|')
        lines = self.text.splitlines()
        for i, line in enumerate(lines):
            if '|' not in line or '品目' not in line:
                continue
            headers = line.strip().strip('|').split('|')
            positions = [j for j, h in enumerate(headers) if '品目名称' in norm_text(h)
                         or norm_text(h) in ('品目', '采购品目')]
            for row in lines[i + 1:]:
                if '|' not in row:
                    break
                cells = row.strip().strip('|').split('|')
                if len(cells) != len(headers):
                    break
                if quoted not in norm_text(row).strip('|'):
                    continue
                if any(norm_text(cells[j]) == norm_text(value) for j in positions):
                    return True
        return False


def source_index(html: dict, bundle: Any) -> SourceIndex:
    """与抽取阶段同一份解析结果拼成全部可读文字（标题 + 正文 + 附件文字/OCR）。"""
    parts = [html.get('title') or '', html.get('model_text') or '']
    unreadable = 0
    for d in (bundle.docs if bundle else []):
        if d.text:
            parts.append(d.text)
        elif d.images:
            unreadable += len(d.images)  # 只有图片、OCR 也没读出文字
    index = SourceIndex('\n'.join(parts), unreadable)
    index.image_names = [image['name'] for d in (bundle.docs if bundle else []) for image in d.images]
    index.announcement_text = html.get('model_text') or ''
    return index


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _close(a: float, b: float, rel: float = 0.02) -> bool:
    return abs(a - b) <= max(0.01, abs(b) * rel)


def check(result: dict, src: SourceIndex) -> list[dict]:
    """对 normalize() 之后的抽取结果逐字段核验，返回可疑字段列表。

    每条：{row, field, code, detail}。code 为可疑原因；code 以 'image:' 开头表示
    原文文字里查不到但存在未识别图片，需看图确认，而不是判定为无依据。
    """
    flags: list[dict] = []
    items = result.get('items') or []
    packages = result.get('packages') or []
    pkg_amount = {p.get('package_no'): _num(p.get('amount')) for p in packages}
    rows_per_pkg: dict[Any, int] = {}
    for it in items:
        rows_per_pkg[it.get('package_no')] = rows_per_pkg.get(it.get('package_no'), 0) + 1
    project_total = _num(result.get('total_amount'))
    has_category_label = '品目' in src.text
    project_name = result.get('project_name')
    name_pkgs: dict[str, int] = {}
    for it in items:
        n = norm_text(it.get('item_name'))
        if n:
            name_pkgs[n] = name_pkgs.get(n, 0) + 1

    def flag(i: int, field: str, code: str, detail: str) -> None:
        if src.unreadable_images and code in ('num_not_in_source', 'text_not_in_source'):
            code = 'image:' + code
        flags.append({'row': i, 'field': field, 'code': code, 'detail': detail})

    for i, it in enumerate(items):
        up, q, tp = _num(it.get('unit_price')), _num(it.get('quantity')), _num(it.get('total_price'))

        name = it.get('item_name')
        if name and src.coverage(name) < 0.8:
            flag(i, 'item_name', 'text_not_in_source', f'名称「{name}」在原文中找不到完整对应')
        if name and PKG_PREFIX.match(str(name)):
            flag(i, 'item_name', 'name_has_package_prefix', f'名称「{name}」开头带有包号/标段号')
        elif name and PKG_SUFFIX.search(str(name)) and project_name and norm_text(project_name) in norm_text(name):
            flag(i, 'item_name', 'name_is_package_title', f'名称「{name}」是“项目名+包号”的包标题')

        cat = it.get('category')
        if cat:
            if not has_category_label:
                flag(i, 'category', 'no_category_label', '原文没有“品目”字段，品目可能是推断的')
            elif src.coverage(cat) < 0.9:
                flag(i, 'category', 'text_not_in_source', f'品目「{cat}」在原文中找不到')

        brand = it.get('brand')
        if brand:
            if norm_text(brand) in {norm_text(x) for x in NOT_BRANDS}:
                flag(i, 'brand', 'not_a_brand', f'「{brand}」不是品牌')
            elif src.coverage(brand) < 0.9:
                flag(i, 'brand', 'text_not_in_source', f'品牌「{brand}」在原文中找不到')

        model = it.get('model')
        references = {'详见附件', '见附件', '详见报价明细表', '详见投标文件分项报价表'}
        for field in ('brand', 'model'):
            if norm_text(it.get(field)) in references:
                flag(i, field, 'reference_only', '字段是指向附件的占位说明，需核对相应报价明细')
        if model and src.coverage(model) < 0.8:
            flag(i, 'model', 'text_not_in_source', f'型号「{model}」在原文中找不到完整对应')

        for row in src.wan_price_rows:
            if not name or norm_text(name) not in norm_text(row['row']):
                continue
            value = up if row['field'] == 'unit_price' else tp
            if value is not None and any(v > 0 and abs(v - value) < max(1e-8, abs(v) * 1e-6) for v in row['values']):
                flag(i, row['field'], 'money_unit_context', '价格与万元列原始数字相同，需对照表头、同行金额及成交金额确认单位')
                if row['field'] == 'unit_price' and tp is not None and q and _close(up * q, tp, 1e-6):
                    flag(i, 'total_price', 'money_unit_context', '总价与未确认单位的单价乘数量一致，需一并复核为元')

        for field, v in (('unit_price', up), ('quantity', q), ('total_price', tp)):
            if v is None:
                continue
            hits, budget_hits = src.number_hits(v)
            if hits == 0:
                # 总价=单价×数量 是提示词允许的唯一推算，不算无依据
                if field == 'total_price' and up is not None and q is not None and _close(up * q, v):
                    continue
                flag(i, field, 'num_not_in_source', f'{v:g} 在原文数字中找不到')
            elif field != 'quantity' and hits == budget_hits:
                flag(i, field, 'num_only_as_budget', f'{v:g} 在原文中只以预算/限价身份出现')

        if up is not None and q is not None and tp is not None and tp > 0 and not _close(up * q, tp):
            for field in NUMERIC:
                flag(i, field, 'arith_mismatch', f'单价×数量={up * q:g} 与总价 {tp:g} 不符')

        if tp is not None and rows_per_pkg.get(it.get('package_no'), 0) > 1:
            pa = pkg_amount.get(it.get('package_no'))
            if pa and _close(tp, pa, 0.001):
                flag(i, 'total_price', 'package_total_copied', f'该包有 {rows_per_pkg[it.get("package_no")]} 行明细，'
                     f'但本行总价等于包总额 {pa:g}')
            elif project_total and len(items) > 1 and _close(tp, project_total, 0.001):
                flag(i, 'total_price', 'project_total_copied', f'本行总价等于项目总额 {project_total:g}')

        if up is not None and tp is not None and q is not None and q > 1 and _close(up, tp, 0.001):
            flag(i, 'unit_price', 'unit_equals_total', f'数量为 {q:g}，单价却等于总价')
        if tp is None and rows_per_pkg.get(it.get('package_no')) == 1 and pkg_amount.get(it.get('package_no')):
            flag(i, 'total_price', 'single_item_total_missing', '该包当前只有一条标的但总价为空，需按原文确认成交金额是否明确对应此标的')
    from .evaluation import _package_key
    keys = {_package_key(p.get('package_no')) for p in packages} or {None}
    if len(packages) > 1 and any(_package_key(it.get('package_no')) not in keys for it in items):
        flags.append({'scope': 'inventory', 'row': None, 'field': None, 'key': None,
                      'code': 'inventory_incomplete', 'a_index': list(range(len(items))),
                      'all_packages': True, 'b_rows': src.candidate_items,
                      'detail': '多包公告存在未关联的共用行，需统一核对清单及包归属，避免重复追加'})
        return flags + _package_identity_flags(result, src) + _package_link_flags(result)
    for key in keys:
        indices = [i for i, it in enumerate(items) if _package_key(it.get('package_no')) == key]
        listed = [t for t in src.inventory_tables if t.get('kind') == '成交明细'
                  and _package_key(t.get('package')) == key and '附件' in str(t.get('where'))]
        expected = max((int(t.get('rows') or 0) for t in listed), default=0)
        combined = any(len(str(items[i].get('item_name') or '')) > 45
                       and re.search(r'[；;、]', str(items[i].get('item_name') or '')) for i in indices)
        enumerated = len(indices) <= 1 and any(
            len(re.findall(r'\d+(?:\.\d+)?\s*(?:台次|台|套)', cell)) >= 3
            and re.search(r'采购|租赁|提供|包含', cell)
            for line in src.announcement_text.splitlines() for cell in line.split('|'))
        if not indices or expected > len(indices) or combined or enumerated:
            flags.append({'scope': 'inventory', 'row': None, 'field': None, 'key': key,
                          'code': 'inventory_incomplete', 'a_index': indices,
                          'detail': '清单为空、合并了多项名称，或少于已盘点附件明细；核对真实行及重复层级'})
    return flags + _package_identity_flags(result, src) + _package_link_flags(result)
