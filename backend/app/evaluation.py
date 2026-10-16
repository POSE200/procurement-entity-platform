"""准确率评测：把实时抽取结果与人工复核答案（full_verified/*.json）逐篇对比。

指标口径（每项都给出 TP/FP/FN 与 P/R/F1，便于复算）：
- 标的物七字段（答疑 Q16 原文口径）：明细按“包号 + 名称相似度”一对一匹配后逐字段比较。
  答案有该字段 → P 样本：预测正确记 TP，预测错误或漏填记 FP；
  答案无该字段/缺失 → N 样本：预测也为空记 TN，预测填了值记 FN。
  多抽的明细行其非空字段记 FN，漏抽的明细行其答案非空字段记 FP；TN 只在已配对行上计。
  P=TP/(TP+FP)，R=TP/(TP+FN)，A=(TP+TN)/(TP+FP+FN+TN)。
- 明细行：匹配数 / 预测行数（P）、匹配数 / 答案行数（R）。
- 采购单位、项目总金额：逐篇比较。
- 中标供应商、全部投标主体：按规范化名称集合比较。
- 数值字段（单价、数量、总价、项目总金额）按答疑 Q15：统一保留两位小数比较，下限以整数部分为准
  （整数部分相同即一致，HTML 抹去或四舍五入小数的情况都算正确）；文本字段规范化后比较（去空白、全半角括号统一、忽略大小写）。
"""
from __future__ import annotations

import json
import math
import os
import re
from difflib import SequenceMatcher
from pathlib import Path

from . import storage
from .extraction import norm_org

def _find_gold_dir() -> Path:
    """Locate the authoritative full_verified directory beside the demo or project.

    The packaged demo is often started from a temporary folder while the checked
    in annotations stay in the project-level ``gold/full_verified`` directory.
    An explicit ICT_GOLD_DIR override is supported for other deployments.
    """
    override = os.environ.get('ICT_GOLD_DIR', '').strip()
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidates.extend((ancestor / 'full_verified', ancestor / 'gold' / 'full_verified'))
    candidates.extend((Path.cwd() / 'full_verified', Path.cwd() / 'gold' / 'full_verified'))
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_dir() and any(candidate.glob('*.json')):
            return candidate
    # Keep the documented default in the error message when no directory exists.
    return candidates[0] if candidates else here.parents[3] / 'full_verified'


GOLD_DIR = _find_gold_dir()
FIELDS = [('item_name', '标的名称'), ('category', '品目'), ('brand', '品牌'), ('model', '规格型号'),
          ('unit_price', '单价'), ('quantity', '数量'), ('total_price', '总价')]
NUMERIC = {'unit_price', 'quantity', 'total_price'}


def gold_available() -> bool:
    return GOLD_DIR.is_dir() and any(GOLD_DIR.glob('*.json'))


def load_gold(aid: str) -> dict | None:
    p = GOLD_DIR / f'{aid}.json'
    return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else None


def _norm_text(v) -> str | None:
    if v is None:
        return None
    s = re.sub(r'\s+', '', str(v)).replace('（', '(').replace('）', ')').replace('，', ',').lower()
    return s or None


_NUMERIC_PACKAGE_RE = re.compile(r'(?:第)?(?:(?:采购|合同)?(?:标)?包)?(\d+)(?:标包|包)?')
_CN_DIGITS = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
# 包/标段/标项/包件/子包等写法统一成纯序号；“第”和这些词可在数字前或后
_PKG_WORD = r'(?:采购包|合同包|分包|分标|子包|标包|包别|包件|包组|标段|标项|标的|包|段)'
_LABELED_PACKAGE_RE = re.compile(rf'(?:第)?{_PKG_WORD}?(?:第)?([0-9]+|[一二两三四五六七八九十]+)(?:{_PKG_WORD})?')
_EMPTY_PACKAGE = {'无分包', '不分包', '无', '/', '-', '—', 'none', 'null', '单包', '整包'}


def _cn_int(s: str) -> int | None:
    """一~九十九 的中文数字转整数。"""
    if s.isdigit():
        return int(s)
    if not s or any(ch not in _CN_DIGITS and ch != '十' for ch in s):
        return None
    if '十' not in s:
        return _CN_DIGITS[s] if len(s) == 1 else None
    tens, _, ones = s.partition('十')
    t = _CN_DIGITS.get(tens, None) if tens else 1
    o = _CN_DIGITS.get(ones, None) if ones else 0
    return None if t is None or o is None else t * 10 + o


def _package_key(v) -> str | None:
    """包号规范化：1、第1包、1包、采购包1、一标段、标段一、标项1、包件2、子包3、第一包 都归为同一序号。

    评测只用它判断“两边说的是不是同一个包”，不改变任何抽取值。
    """
    value = _norm_text(v)
    if not value or value in _EMPTY_PACKAGE or value == '未分包':
        return None
    alnum = re.fullmatch(r'(?:包号|标段)?([a-z])0*(\d{1,2})(?:标段|标包|包)?', value)
    if alnum:
        return f'alnum:{alnum.group(1).upper()}{int(alnum.group(2))}'
    full_alnum = re.fullmatch(r'.*\d{4,}[^\s]*?([a-z])0*(\d{1,2})', value)
    if full_alnum:
        return f'alnum:{full_alnum.group(1).upper()}{int(full_alnum.group(2))}'
    match = _NUMERIC_PACKAGE_RE.fullmatch(value)
    if match:
        return str(int(match.group(1)))
    labeled = _LABELED_PACKAGE_RE.fullmatch(value)
    if labeled:
        n = _cn_int(labeled.group(1))
        if n is not None:
            return str(n)
    # “1-1”“3_1”：包号-子序号（品目号写法），按主包号归并
    sub = re.fullmatch(r'(\d+)[-_]\d+', value)
    if sub:
        return str(int(sub.group(1)))
    # 采购编号 + 包序号，如“豫政采(2)20252163-1”“邓财磋商采购-2026-4-3”“SDGP...-A”：取末段序号
    code = re.fullmatch(r'(.*\d{4,}.*)[-_/]([0-9]{1,2}|[a-z])', value)
    if code and re.search(r'[^\d\-_/]', code.group(1)):
        tail = code.group(2)
        return f'letter:{tail.upper()}' if tail.isalpha() else str(int(tail))
    # Some result notices use the procurement code as the package identifier
    # (for example, "驿政采购-2026-01-1A"), while the reviewed answer records
    # only its letter label ("A"). Treat explicit letter-package labels and
    # delimited numeric-code suffixes as the same package for evaluation.
    letter = re.fullmatch(r'(?:第)?(?:标段|标包|包别|包)?([a-z])(?:标段|标包|包别|包)?', value)
    if letter:
        return f'letter:{letter.group(1).upper()}'
    suffix = re.search(r'[-_/]\d+([a-z])$', value)
    if suffix:
        return f'letter:{suffix.group(1).upper()}'
    # 无分隔符的政府采购完整包号：SDGP...A 与 A 是同一包标签。
    code_letter = re.fullmatch(r'(?:sdgp|zfcg)[0-9]{8,}([a-z])', value)
    if code_letter:
        return f'letter:{code_letter.group(1).upper()}'
    return value


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def same_number(x: float, y: float) -> bool:
    """答疑 Q15：金额正确率下限以整数部分为准，统一保留两位小数。

    依次接受：两位小数后相等；整数部分相等（HTML 抹去小数）；四舍五入到整数后相等
    （HTML 把小数四舍五入）。不再使用旧版 1% 相对误差，那会把千元级差异判为正确。
    """
    if round(x, 2) == round(y, 2):
        return True
    if math.trunc(round(x, 2)) == math.trunc(round(y, 2)):
        return True
    return round(x) == round(y) and abs(x - y) < 1


def same(field: str, a, b) -> bool:
    if field in NUMERIC or field == 'amount':
        x, y = _num(a), _num(b)
        if x is None or y is None:
            return x is None and y is None
        return same_number(x, y)
    if field == 'item_name':
        return re.sub(r'^[▲△■□★☆]+', '', _norm_text(a) or '') == \
               re.sub(r'^[▲△■□★☆]+', '', _norm_text(b) or '')
    return _norm_text(a) == _norm_text(b)


def _present(field: str, v) -> bool:
    return (_num(v) if field in NUMERIC else _norm_text(v)) is not None


class Counter3:
    """混淆矩阵计数。tn 只对逐字段比较有意义（答案为空且预测也为空，即“正确空值”）。"""
    __slots__ = ('tp', 'fp', 'fn', 'tn')

    def __init__(self):
        self.tp = self.fp = self.fn = self.tn = 0

    def add(self, other: 'Counter3') -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn
        self.tn += other.tn

    def out(self, label: str) -> dict:
        # 答疑 Q16：P = TP/(TP+FP)，R = TP/(TP+FN)，A = (TP+TN)/(TP+FP+FN+TN)
        p = self.tp / (self.tp + self.fp) if self.tp + self.fp else None
        r = self.tp / (self.tp + self.fn) if self.tp + self.fn else None
        f1 = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
        total = self.tp + self.fp + self.fn + self.tn
        # 答疑 Q16：按二分类混淆矩阵计算准确率 A=(TP+TN)/(TP+FP+FN+TN)
        acc = (self.tp + self.tn) / total if total else None
        return {'label': label, 'tp': self.tp, 'fp': self.fp, 'fn': self.fn, 'tn': self.tn,
                'precision': _r(p), 'recall': _r(r), 'f1': _r(f1), 'accuracy': _r(acc)}


def _r(v):
    return None if v is None else round(v, 4)


def _item_score(p: dict, g: dict) -> float:
    p_package, g_package = _package_key(p.get('package_no')), _package_key(g.get('package_no'))
    if p_package and g_package and p_package != g_package:
        return 0.0
    a, b = _norm_text(p.get('item_name')) or '', _norm_text(g.get('item_name')) or ''
    s = SequenceMatcher(None, a, b).ratio() if a and b else 0.0
    if _norm_text(p.get('model')) and _norm_text(p.get('model')) == _norm_text(g.get('model')):
        s += 0.3
    if _norm_text(p.get('brand')) and _norm_text(p.get('brand')) == _norm_text(g.get('brand')):
        s += 0.3
    if p_package and p_package == g_package:
        s += 0.1
    return s


def match_items(pred: list[dict], gold: list[dict]) -> list[tuple[int, int]]:
    """先按唯一包号配对，再对剩余行按名称/型号相似度贪心匹配。"""
    pred_by_package: dict[str, list[int]] = {}
    gold_by_package: dict[str, list[int]] = {}
    for i, item in enumerate(pred):
        package = _package_key(item.get('package_no'))
        if package:
            pred_by_package.setdefault(package, []).append(i)
    for i, item in enumerate(gold):
        package = _package_key(item.get('package_no'))
        if package:
            gold_by_package.setdefault(package, []).append(i)

    # 包号在预测和答案中都唯一时，它足以确定行身份；名称/型号为空不应
    # 让整行掉出匹配，后续仍逐字段比较，缺失或错误值照常计入 FN/FP。
    pairs: list[tuple[int, int]] = []
    used_p, used_g = set(), set()
    for package, p_indices in pred_by_package.items():
        g_indices = gold_by_package.get(package, [])
        if len(p_indices) == len(g_indices) == 1:
            i, j = p_indices[0], g_indices[0]
            pairs.append((i, j))
            used_p.add(i)
            used_g.add(j)

    # 同名/同品牌/同型号候选同分时，用数量价格消歧；身份阈值与计分公式保持原样。
    cand = sorted(((_item_score(p, g), sum(_present(k, p.get(k)) and _present(k, g.get(k))
                                        and same(k, p.get(k), g.get(k)) for k in NUMERIC), i, j)
                   for i, p in enumerate(pred) if i not in used_p
                   for j, g in enumerate(gold) if j not in used_g), reverse=True)
    for s, _, i, j in cand:
        if s < 0.5:
            break
        if i in used_p or j in used_g:
            continue
        used_p.add(i)
        used_g.add(j)
        pairs.append((i, j))
    return pairs


def _set_counter(pred: set, gold: set) -> Counter3:
    c = Counter3()
    c.tp, c.fp, c.fn = len(pred & gold), len(pred - gold), len(gold - pred)
    return c


def compare_one(pred: dict, gold: dict) -> dict:
    """单篇对比，返回各指标的计数（未汇总）与差异示例。"""
    p_items, g_items = pred['items'], gold.get('items') or []
    # 本公告原文明示的两套编号单独留存；别名只用于配对，不改抽取值。
    aliases = {}
    for package in pred.get('packages') or []:
        canonical = _package_key(package.get('package_no'))
        if canonical is None:
            continue
        for alias in package.get('source_aliases') or []:
            if alias.get('verified') and alias.get('quote'):
                key = _package_key(alias.get('value'))
                if key is not None:
                    aliases[key] = canonical
    for group in gold.get('source_package_aliases') or []:
        if not group.get('evidence'):
            continue
        canonical = group.get('canonical')
        for label in [canonical, *(group.get('aliases') or [])]:
            key = _package_key(label)
            if key is not None:
                aliases[key] = canonical
    if aliases:
        def aligned(rows):
            result = []
            for row in rows:
                key = _package_key(row.get('package_no'))
                seen = set()
                while key in aliases and key not in seen:
                    seen.add(key)
                    key = _package_key(aliases[key])
                result.append({**row, 'package_no': key})
            return result
        p_items, g_items = aligned(p_items), aligned(g_items)
    pairs = match_items(p_items, g_items)
    fields = {k: Counter3() for k, _ in FIELDS}
    diffs = []
    # 答疑 Q16 原文口径：答案有该字段 → P 样本；答案无该字段/缺失 → N 样本。
    #   P 样本：预测正确 → TP；预测错误或漏填 → FP（“有字段，但标注错误”）。
    #   N 样本：预测也为空 → TN；预测填了值 → FN（“N 样本同理”：无字段，但标注错误）。
    # 每个样本只计一次，TP+FP+TN+FN = 样本总数。
    for i, j in pairs:
        for k, _ in FIELDS:
            pv, gv = p_items[i].get(k), g_items[j].get(k)
            pp, gp = _present(k, pv), _present(k, gv)
            if gp:
                if pp and same(k, pv, gv):
                    fields[k].tp += 1
                else:
                    fields[k].fp += 1
                    diffs.append({'field': k, 'item': g_items[j].get('item_name'), 'pred': pv, 'gold': gv})
            elif pp:
                fields[k].fn += 1
                diffs.append({'field': k, 'item': g_items[j].get('item_name'), 'pred': pv, 'gold': gv})
            else:
                fields[k].tn += 1
    mp, mg = {i for i, _ in pairs}, {j for _, j in pairs}
    # 多抽的明细行：答案里没有这一行，其非空字段都是“无字段但填了值” → FN
    for i, it in enumerate(p_items):
        if i not in mp:
            for k, _ in FIELDS:
                if _present(k, it.get(k)):
                    fields[k].fn += 1
                    diffs.append({'field': k, 'item': it.get('item_name'), 'pred': it.get(k), 'gold': None})
    # 漏抽的明细行：答案有值的字段没有被正确标注 → FP
    for j, it in enumerate(g_items):
        if j not in mg:
            for k, _ in FIELDS:
                if _present(k, it.get(k)):
                    fields[k].fp += 1
                    diffs.append({'field': k, 'item': it.get('item_name'), 'pred': None, 'gold': it.get(k)})
    # TN 只在已配对的明细行上计：整行漏抽/多抽时不把其空字段算作“判对”，避免虚高。
    rows = Counter3()
    rows.tp, rows.fp, rows.fn = len(pairs), len(p_items) - len(pairs), len(g_items) - len(pairs)

    gp = gold.get('project') or {}
    g_bidders = gold.get('bidders') or []
    g_win = {norm_org(p.get('winner')) for p in gp.get('packages') or [] if p.get('winner')} | \
            {norm_org(b['name']) for b in g_bidders if b.get('role') == 'winner' and b.get('name')}
    p_win = {norm_org(b['name']) for b in pred['bidders'] if b['role'] == 'winner'}
    g_all = {norm_org(b['name']) for b in g_bidders if b.get('name')} | g_win
    p_all = {norm_org(b['name']) for b in pred['bidders']}
    return {
        'fields': fields, 'rows': rows,
        'winners': _set_counter(p_win, g_win), 'bidders': _set_counter(p_all, g_all),
        'buyer_ok': same('buyer', pred['buyer'], gp.get('buyer')),
        'amount_ok': same('amount', pred['total_amount'], gp.get('total_amount')),
        'item_exact': bool(pairs) and len(pairs) == len(p_items) == len(g_items) and
                      all(same(k, p_items[i].get(k), g_items[j].get(k)) for i, j in pairs for k, _ in FIELDS),
        # Keep the API payload bounded, but preserve the true total for the UI.
        'diffs': diffs[:20], 'diff_count': len(diffs),
        'pred_buyer': pred['buyer'], 'gold_buyer': gp.get('buyer'),
        'pred_amount': pred['total_amount'], 'gold_amount': gp.get('total_amount'),
        'pred_items': len(p_items), 'gold_items': len(g_items),
    }


def _pred_from_db(aid: str) -> dict | None:
    a = storage.one('SELECT buyer, total_amount, bidders_json, packages_json FROM announcements WHERE announcement_id=?', (aid,))
    if not a:
        return None
    items = storage.rows('SELECT package_no, item_name, category, brand, model, unit_price, quantity, total_price '
                         'FROM items WHERE announcement_id=? ORDER BY rowid', (aid,))
    return {'buyer': a['buyer'], 'total_amount': a['total_amount'], 'items': items,
            'bidders': storage.loads(a['bidders_json'], []), 'packages': storage.loads(a['packages_json'], [])}


def _preds_bulk() -> dict[str, dict]:
    """一次性读出全部公告与明细（两条 SQL），避免逐篇建连接；千篇规模下由分钟级降到秒级。"""
    out: dict[str, dict] = {}
    for a in storage.rows('SELECT announcement_id, buyer, total_amount, bidders_json, packages_json FROM announcements'):
        out[a['announcement_id']] = {'buyer': a['buyer'], 'total_amount': a['total_amount'], 'items': [],
                                     'bidders': storage.loads(a['bidders_json'], []),
                                     'packages': storage.loads(a['packages_json'], [])}
    for it in storage.rows('SELECT announcement_id, package_no, item_name, category, brand, model, unit_price, '
                           'quantity, total_price FROM items ORDER BY rowid'):
        p = out.get(it.pop('announcement_id'))
        if p is not None:
            p['items'].append(it)
    return out


def evaluate(job_id: str | None = None) -> dict:
    """评测库中（或指定任务）已抽取的公告。只统计有复核答案的公告。"""
    if not gold_available():
        raise ValueError(f'未找到复核答案目录 {GOLD_DIR}')
    if job_id:
        ids = [r['announcement_id'] for r in storage.rows(
            "SELECT announcement_id FROM job_tasks WHERE job_id=? AND status IN ('succeeded','skipped')", (job_id,))]
        job = storage.one('SELECT provider, model FROM jobs WHERE id=?', (job_id,))
    else:
        ids = [r['announcement_id'] for r in storage.rows('SELECT announcement_id FROM announcements')]
        job = None
    fields = {k: Counter3() for k, _ in FIELDS}
    rows, winners, bidders = Counter3(), Counter3(), Counter3()
    n = buyer_ok = amount_ok = exact = 0
    per_doc = []
    preds = _preds_bulk()
    for aid in sorted(ids):
        gold, pred = load_gold(aid), preds.get(aid)
        if not gold or not pred:
            continue
        r = compare_one(pred, gold)
        n += 1
        for k in fields:
            fields[k].add(r['fields'][k])
        rows.add(r['rows'])
        winners.add(r['winners'])
        bidders.add(r['bidders'])
        buyer_ok += r['buyer_ok']
        amount_ok += r['amount_ok']
        exact += r['item_exact']
        f = Counter3()
        for c in r['fields'].values():
            f.add(c)
        fields_denominator = f.tp + f.fp + f.fn + f.tn
        fields_accuracy = (f.tp + f.tn) / fields_denominator if fields_denominator else None
        per_doc.append({'announcement_id': aid, 'fields_f1': f.out('')['f1'],
                        'fields_accuracy': _r(fields_accuracy), 'buyer_ok': r['buyer_ok'],
                        'amount_ok': r['amount_ok'], 'pred_items': r['pred_items'], 'gold_items': r['gold_items'],
                        'item_exact': r['item_exact'], 'diffs': r['diffs'], 'diff_count': r['diff_count'],
                        'pred_buyer': r['pred_buyer'], 'gold_buyer': r['gold_buyer'],
                        'pred_amount': r['pred_amount'], 'gold_amount': r['gold_amount']})
    micro = Counter3()
    for c in fields.values():
        micro.add(c)
    field_out = [fields[k].out(label) for k, label in FIELDS]
    f1s = [f['f1'] for f in field_out if f['f1'] is not None]
    per_doc.sort(key=lambda d: (d['fields_accuracy'] if d['fields_accuracy'] is not None else 2))
    return {
        'job_id': job_id, 'provider': job['provider'] if job else None, 'model': job['model'] if job else None,
        'created_at': storage.now(), 'evaluated': n, 'requested': len(ids),
        'gold_source': 'full_verified（人工复核答案）',
        'summary': {
            'fields_micro': micro.out('七字段（微平均）'),
            'fields_macro_f1': _r(sum(f1s) / len(f1s)) if f1s else None,
            'rows': rows.out('标的明细行'),
            'winners': winners.out('中标供应商'),
            'bidders': bidders.out('投标主体（含未中标）'),
            'buyer_accuracy': _r(buyer_ok / n) if n else None,
            'amount_accuracy': _r(amount_ok / n) if n else None,
            'doc_exact_rate': _r(exact / n) if n else None,
        },
        'fields': field_out,
        'worst': per_doc[:30],
    }
