"""方案一+二：可疑字段 / 两路分歧 → 定点重抽问题；模型回答 → 按原文依据决定采用哪个值。

程序在这里只做三件事：组织问题、核对模型给出的引文是否真在原文里、按模型的选择替换整包清单。
最终写入的每个字段值都来自模型的某一次输出（A、B 或重抽），程序不生成字段值。
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

from .recheck_prompt import FIELD_LABEL, HINT, PROJECT_LABEL, RECHECK_INSTRUCTION
from .source_check import NUMERIC, SourceIndex, norm_text

ROW_CONTEXT = ('package_no', 'item_name', 'brand', 'model', 'quantity', 'unit')
LIST_SHOW = 600


def build_questions(result: dict, flags: list[dict], diff: dict | None = None) -> list[dict]:
    """同一行同一字段只问一次。

    只来自原文核对的问题不给被问字段的原值；两路分歧的问题给出甲、乙两个候选值。
    """
    items = result.get('items') or []
    diff = diff or {'fields': [], 'packages': [], 'project': []}
    whole = next((f for f in flags if f.get('all_packages')), None)
    if whole:
        diff = {**diff, 'fields': [], 'packages': [{'key': None, 'a_rows': whole['a_index'],
                                                  'b_rows': whole.get('b_rows') or []}]}
    grouped: dict[tuple[int, str], list[str]] = {}
    cands: dict[tuple[int, str], dict] = {}
    for f in flags:
        if f.get('scope') in ('package_identity', 'package_link', 'inventory'):
            continue
        grouped.setdefault((f['row'], f['field']), []).append(f['code'])
    for d in diff['fields']:
        grouped.setdefault((d['row'], d['field']), []).append('dual_disagree')
        cands[(d['row'], d['field'])] = {'甲': d['a'], '乙': d['b']}
    asked: dict[int, set[str]] = {}
    for row, field in grouped:
        asked.setdefault(row, set()).add(field)
    questions = []
    n = 0
    for (row, field), codes in sorted(grouped.items()):
        n += 1
        it = items[row]
        # 定位信息里去掉该行所有被问到的字段，避免把原值从别的问题里泄露出去
        ctx = {k: it.get(k) for k in ROW_CONTEXT if k not in asked[row] and it.get(k) not in (None, '')}
        questions.append({'id': f'q{n}', 'kind': 'field', 'row': row, 'field': field, 'codes': sorted(set(codes)),
                          'label': FIELD_LABEL[field], 'context': ctx, 'candidates': cands.get((row, field)),
                          'hints': [HINT.get(c, c) for c in sorted(set(codes))]})
    for d in diff['project']:
        n += 1
        questions.append({'id': f'q{n}', 'kind': 'project', 'field': d['field'], 'codes': ['dual_disagree'],
                          'label': PROJECT_LABEL[d['field']], 'candidates': {'甲': d['a'], '乙': d['b']}})
    for d in diff['packages']:
        n += 1
        a_rows = [items[i] for i in d['a_rows']]
        questions.append({'id': f'q{n}', 'kind': 'package', 'key': d['key'], 'codes': ['dual_disagree'],
                          'a_index': d['a_rows'], 'lists': {'甲': a_rows, '乙': d['b_rows']}})
        categories = {row.get('category') for row in a_rows if row.get('category')}
        if len(categories) == 1 and d['b_rows'] and all(not row.get('category') for row in d['b_rows']):
            n += 1
            questions.append({'id': f'q{n}', 'kind': 'package_shared', 'key': d['key'],
                              'field': 'category', 'codes': ['shared_category_missing'],
                              'label': '同一采购包明确共用的采购品目'})
    for f in flags:
        if f.get('scope') != 'inventory':
            continue
        existing = next((q for q in questions if q['kind'] == 'package' and q['key'] == f['key']), None)
        if existing:
            existing['codes'].append(f['code'])
            continue
        n += 1
        rows = [items[i] for i in f['a_index']]
        questions.append({'id': f'q{n}', 'kind': 'package', 'key': f['key'],
                          'codes': [f['code']], 'a_index': f['a_index'],
                          'lists': {'甲': rows, '乙': rows}})
    for f in flags:
        if f.get('scope') != 'package_identity':
            continue
        n += 1
        package = result['packages'][f['package_index']]
        questions.append({'id': f'q{n}', 'kind': 'package_identity', 'field': 'package_no',
                          'package_index': f['package_index'], 'codes': [f['code']],
                          'context': {k: package.get(k) for k in ('package_name', 'winner', 'amount')
                                      if package.get(k) not in (None, '')},
                          'source_snippets': f.get('source_snippets') or []})
    for f in flags:
        if f.get('scope') != 'package_link':
            continue
        n += 1
        row = result[f['collection']][f['row']]
        questions.append({'id': f'q{n}', 'kind': 'package_link', 'field': 'package_no',
                          'row': f['row'], 'collection': f['collection'], 'codes': [f['code']],
                          'context': {k: v for k, v in row.items()
                                      if k != 'package_no' and v not in (None, '')},
                          'packages': result.get('packages') or []})
    return questions


def _fmt(v: Any) -> str:
    return '空' if v in (None, '') else (f'{v:g}' if isinstance(v, float) else str(v))


def _row_line(it: dict) -> str:
    return '｜'.join(_fmt(it.get(k)) for k in ('item_name', 'brand', 'model', 'quantity', 'total_price'))


def question_text(questions: list[dict]) -> str:
    lines = [RECHECK_INSTRUCTION, '【需要核对的字段】']
    for q in questions:
        if q['kind'] == 'field':
            ctx = '，'.join(f'{FIELD_LABEL.get(k, "包号" if k == "package_no" else "单位")}={v}'
                           for k, v in q['context'].items())
            line = (f'{q["id"]}. 标的行（第{q["row"] + 1}行；{ctx}）的「{q["label"]}」是多少？'
                    f'核对原因：{"；".join(q["hints"])}。')
            if q['candidates']:
                line += f'候选值：甲={_fmt(q["candidates"]["甲"])}；乙={_fmt(q["candidates"]["乙"])}。'
        elif q['kind'] == 'project':
            line = (f'{q["id"]}. 本公告的「{q["label"]}」是多少？两次独立抽取的结果不一致，'
                    f'候选值：甲={_fmt(q["candidates"]["甲"])}；乙={_fmt(q["candidates"]["乙"])}。')
        elif q['kind'] == 'package_identity':
            ctx = '，'.join(f'{k}={_fmt(v)}' for k, v in q['context'].items())
            line = (f'{q["id"]}. 包信息（{ctx}）对应的包标识及其短标签对应关系是什么？'
                    '请区分项目编号、包编号与包名称，保留包号末尾字母或分包序号；'
                    '依据本项目已成交包的上下文核验，不把其他包或示例编号带入。'
                    '\n原文定位片段：' + '；'.join(q['source_snippets']))
        elif q['kind'] == 'package_link':
            line = (f'{q["id"]}. {q["collection"]}第{q["row"] + 1}条记录应属于哪个已成交包？'
                    f'定位信息：{dumps(q["context"])}。'
                    f'当前包信息：{dumps(q["packages"])}。'
                    '依据名称、供应商、对应明细表与公告原文确认归属，'
                    'value填写对应包的完整标识；quote抄出能支持该记录包归属的原文。'
                    '无法确认时填null，不默认归入第一个包。')
        elif q['kind'] == 'package_shared':
            line = (f'{q["id"]}. 包「{q["key"]}」若采用逐项清单，原文是否明确为该包全部标的公布了同一采购品目？'
                    '只在品目字段及适用包归属明确时，value逐字填写该共用品目；quote包含品目标签和值。'
                    '未明确共用或不同标的有不同品目时填null，不按商品名称推断。')
        else:
            pkg = '本公告' if q['key'] is None else f'包「{q["key"]}」'
            line = f'{q["id"]}. {pkg}实际成交的标的明细是哪一套？两次独立抽取的清单不一致（每行：名称｜品牌｜型号｜数量｜总价）。'
            for side in ('甲', '乙'):
                rows = q['lists'][side]
                body = '；'.join(f'{k + 1}) {_row_line(r)}' for k, r in enumerate(rows[:LIST_SHOW]))
                more = f'；……共{len(rows)}行' if len(rows) > LIST_SHOW else ''
                line += f'\n   候选清单{side}（{len(rows)}行）：{body or "无标的"}{more}'
            if 'inventory_incomplete' in q['codes']:
                line += '\n清单存在空值、合并多项或与附件盘点行数不同，请检查实际明细与汇总的层级；候选相同也可返回重抽清单。'
        lines.append(line)
    return '\n'.join(lines)


def messages_for(system: str, source_user: str | list[dict], questions: list[dict]) -> list[dict[str, Any]]:
    """系统提示与原文部分和首轮 A 完全一致（利于前缀缓存），问题作为追加的用户消息。"""
    return [{'role': 'system', 'content': system},
            {'role': 'user', 'content': source_user},
            {'role': 'user', 'content': question_text(questions)}]


def _num(v: Any) -> float | None:
    from .extraction import to_number
    return to_number(v)


def _value_in_quote(field: str, value: Any, quote: str) -> bool:
    """新值必须能在它自己给出的引文里找到（数值按数值比对，文字按规范化包含）。"""
    if field in NUMERIC or field == 'total_amount':
        v = _num(value)
        # OCR 常把一个数拆成两行（如“5000\n0. 00”），先去掉数字间的断行与空格再找
        joined = re.sub(r'(?<=[\d.])\s+(?=[\d.])', '', quote)
        return v is not None and (SourceIndex(quote).number_hits(v)[0] > 0 or SourceIndex(joined).number_hits(v)[0] > 0)
    return norm_text(value) in norm_text(quote)


def _answers_by_id(answers: Any) -> dict[str, dict]:
    by_id = {}
    if isinstance(answers, dict):
        answers = answers.get('answers')
    for a in answers if isinstance(answers, list) else []:
        if isinstance(a, dict) and a.get('id'):
            by_id[str(a['id'])] = a
    return by_id


def _merge_value(q: dict, old: Any, a: dict | None, src: SourceIndex) -> tuple[str, Any, str, dict]:
    """返回 (action, 新值, 原因, 附加日志)。action ∈ keep/clear/confirm/correct。"""
    field = q['field']
    numeric = field in NUMERIC or field == 'total_amount'
    if a is None:
        return 'keep', old, '模型未回答该问题', {}
    raw_value, quote = a.get('value'), a.get('quote')
    extra = {'answer': raw_value, 'quote': quote}
    if numeric and isinstance(raw_value, str) and raw_value.strip() and \
            not re.fullmatch(r'\s*-?[\d,，]+(\.\d+)?\s*(万)?\s*(元)?\s*', raw_value):
        return 'keep', old, '重抽的数值不是单个数字，保留首轮值待查', extra
    quote_ok = isinstance(quote, str) and quote.strip() and src.quote_found(quote)
    new = _num(raw_value) if numeric else (str(raw_value).strip() if raw_value not in (None, '') else None)
    if isinstance(new, str) and new.lower() in ('null', 'none'):
        new = None
    if new is None:
        if field in ('item_name', 'buyer'):
            # 名称是行的身份：只允许改正，不允许清空成无名行（删行不属于定点重抽的职责）
            return 'keep', old, '名称只能改正不能清空，保留首轮值待查', extra
        if quote_ok:
            return 'clear', None, '重抽判定原文无该值，且给出的原文片段可核实', extra
        return 'keep', old, '重抽答无值但给不出可核实的原文片段，保留首轮值待查', extra
    if not quote_ok:
        return 'keep', old, '重抽给的原文片段在原文中找不到，保留首轮值待查', extra
    if field == 'package_no' and q.get('kind') == 'package_identity':
        from .source_check import package_id_mentions
        from .evaluation import _package_key
        if _package_key(new) not in {_package_key(v) for v, _ in package_id_mentions(quote)}:
            return 'keep', old, '引文需包含明确包号标签和完整包号，不能以项目编号或截短值代替', extra
    in_quote = _value_in_quote(field, new, quote)
    if not in_quote and field in ('unit_price', 'total_price'):
        in_quote = src.money_quote_supports(field, new, quote)
    # 成交总额可能是各包金额之和、原文不直接出现：此时只接受等于某路候选（模型自己的输出）的值
    if not in_quote and field == 'total_amount' and q.get('candidates') and \
            any(_num(c) is not None and abs(_num(c) - new) <= max(0.01, new * 0.001) for c in q['candidates'].values()):
        in_quote = True
    if not in_quote:
        return 'keep', old, '重抽的值不在它自己给出的原文片段里，保留首轮值待查', extra
    same = (old is not None and (abs(_num(old) - new) <= max(0.01, abs(new) * 0.001)
                                 if numeric else norm_text(old) == norm_text(new)))
    return ('confirm' if same else 'correct'), new, '重抽值与原文片段一致', extra


def _package_amount(result: dict, key: Any) -> float | None:
    """该包的成交金额（取自模型输出的 packages；单包项目也可用总中标金额）。找不到返回 None。"""
    from .evaluation import _package_key
    pkgs = result.get('packages') or []
    hit = [p for p in pkgs if _package_key(p.get('package_no')) == key]
    if not hit and len(pkgs) == 1:
        hit = pkgs
    if len(hit) == 1 and _num(hit[0].get('amount')) is not None:
        return _num(hit[0]['amount'])
    if len(pkgs) <= 1:
        return _num(result.get('total_amount'))
    return None


def _overcounts(result: dict, key: Any, rows: list[dict]) -> bool:
    """候选清单的总价合计明显超过包成交金额：说明清单把汇总行和其下的规格明细同时列出，重复计价。

    只做拒绝判断（不生成任何值）；金额未知或总价有空缺时不拦截。
    """
    amount = _package_amount(result, key)
    totals = [_num(r.get('total_price')) for r in rows]
    if not amount or amount <= 0 or not totals or any(t is None for t in totals):
        return False
    return sum(totals) > amount * 1.02 + 1


def _fresh_rows(answer: dict, src: SourceIndex) -> tuple[list[dict] | None, str]:
    from .extraction import normalize

    raw = answer.get('items')
    if not isinstance(raw, list) or len(raw) > 1200:
        return None, '重抽清单缺少有效items数组'
    if not raw and not (answer.get('empty_reason') and src.quote_found(answer.get('quote'))):
        return None, '空清单缺少原文排除依据'
    rows = []
    for item in raw:
        if not isinstance(item, dict) or not item.get('item_name'):
            return None, '新清单存在无名称的行'
        quote = item.get('quote')
        source = norm_text(item.get('source'))
        image = bool(len(source) > 5 and any(source in norm_text(n) or norm_text(n) in source
                                            for n in src.image_names))
        if not quote or not (src.quote_found(quote) or image):
            return None, '新清单有行缺少可定位的文字或原图依据'
        if norm_text(item['item_name']) not in norm_text(quote) and not src.contains(item['item_name']):
            return None, '新清单的名称无法在所引原文中定位'
        normalized, _ = normalize({'items': [item]})
        if not normalized['items']:
            return None, '新清单有行无法解析'
        rows.extend(normalized['items'])
    return rows, ''


def merge(result: dict, questions: list[dict], answers: Any, src: SourceIndex) -> tuple[dict, list[dict]]:
    """按不对称规则合并：有原文依据才改；依据不足保留 A 的值并记待查。"""
    merged = copy.deepcopy(result)
    items = merged.get('items') or []
    by_id = _answers_by_id(answers)
    log = []
    replace: dict[int, list[dict]] = {}   # A 中被整包替换的首行号 → 乙清单
    drop: set[int] = set()
    append: list[dict] = []
    identity_updates = []
    link_updates = []
    field_updates = []
    shared_updates = []
    for q in questions:
        a = by_id.get(q['id'])
        kind = q.get('kind', 'field')
        if kind == 'field':
            row, field = q['row'], q['field']
            old = items[row].get(field)
            action, new, reason, extra = _merge_value(q, old, a, src)
            if action != 'keep':
                items[row][field] = new
                field_updates.append((row, field, new, len(log)))
            entry = {'row': row, 'field': field, 'codes': q['codes'], 'before': old} | extra
            log.append(entry | ({'after': new} if action != 'keep' else {}) | {'action': action, 'reason': reason})
        elif kind == 'project':
            field = q['field']
            old = merged.get(field)
            action, new, reason, extra = _merge_value(q, old, a, src)
            if action != 'keep':
                merged[field] = new
            entry = {'row': None, 'field': f'project.{field}', 'codes': q['codes'], 'before': old} | extra
            log.append(entry | ({'after': new} if action != 'keep' else {}) | {'action': action, 'reason': reason})
        elif kind == 'package_identity':
            index = q['package_index']
            old = merged['packages'][index].get('package_no')
            action, new, reason, extra = _merge_value(q, old, a, src)
            # 包级更名最后统一校验，允许模型有证据地纠正整组包号置换。
            packages = merged['packages']
            if action != 'keep' and new is None and len(packages) > 1:
                action, reason = 'keep', '包号无法唯一关联或与另一个包冲突，保留原值待查'
            if action != 'keep':
                identity_updates.append((index, old, new, len(log)))
                aliases = []
                for alias in (a or {}).get('aliases') or []:
                    if not isinstance(alias, dict) or not alias.get('value') or not new:
                        continue
                    evidence = alias.get('quote')
                    from .source_check import package_id_mentions
                    mentioned = {norm_text(v) for v, _ in package_id_mentions(str(evidence or ''))}
                    if (src.quote_found(evidence) and norm_text(new) in norm_text(evidence)
                            and norm_text(alias['value']) in mentioned):
                        aliases.append({'value': str(alias['value']), 'quote': evidence, 'verified': True})
                if aliases:
                    merged['packages'][index]['source_aliases'] = aliases
            log.append({'row': None, 'field': f'packages[{index}].package_no',
                        'codes': q['codes'], 'before': old, **extra,
                        **({'after': new} if action != 'keep' else {}),
                        'action': action, 'reason': reason})
        elif kind == 'package_link':
            old = result[q['collection']][q['row']].get('package_no')
            action, new, reason, extra = _merge_value(q, old, a, src)
            if action != 'keep' and new is None:
                action, reason = 'keep', '包归属无法确认，保留待查'
            entry = {'row': q['row'], 'field': f'{q["collection"]}[{q["row"]}].package_no',
                     'codes': q['codes'], 'before': old, **extra,
                     **({'after': new} if action != 'keep' else {}), 'action': action, 'reason': reason}
            log.append(entry)
            if action != 'keep':
                link_updates.append((q, new, len(log) - 1))
        elif kind == 'package_shared':
            action, new, reason, extra = _merge_value(q, None, a, src)
            if new is not None and '品目' not in str((a or {}).get('quote') or '') \
                    and not src.category_quote_supports(new, str((a or {}).get('quote') or '')):
                action, reason = 'keep', '共用品目引文需包含品目字段标签'
            log.append({'row':None,'field':f'package[{q["key"]}].category',
                        'codes':q['codes'],**extra,'action':action,'reason':reason})
            if action != 'keep' and new is not None:
                shared_updates.append((q,new,len(log)-1))
        else:
            entry = {'row': None, 'field': f'package[{q["key"]}]', 'codes': q['codes'],
                     'before': f'{len(q["lists"]["甲"])}行', 'candidate': f'{len(q["lists"]["乙"])}行'}
            choice = str((a or {}).get('choice') or '').strip()
            quote = (a or {}).get('quote')
            entry |= {'answer': choice or None, 'quote': quote}
            b_rows = q['lists']['乙']
            fresh_reason = ''
            if choice in ('重抽', '都不对') and a.get('items') is not None:
                b_rows, fresh_reason = _fresh_rows(a, src)
                if b_rows is not None:
                    choice = '乙'
                    quote_ok = True
                    entry['new_items'] = a['items']
                else:
                    b_rows = []
            quote_ok = isinstance(quote, str) and quote.strip() and src.quote_found(quote)
            if 'new_items' in entry:
                quote_ok = True
            if a is None:
                log.append(entry | {'action': 'keep', 'reason': '模型未回答该问题'})
            elif choice == '甲':
                log.append(entry | {'action': 'confirm', 'reason': '重抽选择甲清单（首轮 A）'})
            elif choice != '乙':
                log.append(entry | {'action': 'keep', 'reason': fresh_reason or '两套清单均未被确认，重抽清单未提供'})
            elif not quote_ok:
                log.append(entry | {'action': 'keep', 'reason': '重抽选乙但原文片段在原文中找不到，保留首轮清单待查'})
            elif 'new_items' not in entry and not any(norm_text(r.get('item_name')) and norm_text(r.get('item_name')) in norm_text(quote)
                                                    for r in b_rows):
                log.append(entry | {'action': 'keep', 'reason': '重抽选乙但引文里没有乙清单中的标的名称，保留首轮清单待查'})
            elif _overcounts(merged, q['key'], b_rows):
                log.append(entry | {'action': 'keep',
                                    'reason': '乙清单总价合计超过该包成交金额（疑似汇总行与明细重复计价），保留首轮清单待查'})
            else:
                rows = copy.deepcopy(b_rows)
                if q['a_index']:
                    pkg = items[q['a_index'][0]].get('package_no')
                    for r in rows:
                        r['package_no'] = pkg   # 与 A 的包号写法保持一致，便于关联 packages
                    replace[q['a_index'][0]] = rows
                    drop.update(q['a_index'])
                else:
                    append.extend(rows)
                log.append(entry | {'after': f'{len(rows)}行', 'action': 'correct', 'reason': '重抽选择乙清单，引文可核实'})
    if replace or append:
        new_items = []
        for i, it in enumerate(items):
            if i in replace:
                new_items.extend(replace[i])
            if i not in drop:
                new_items.append(it)
        merged['items'] = new_items + append
    # 校验最终包号集合，整组更名一次完成，避免13→17、17→19等置换被连锁覆盖。
    from .evaluation import _package_key, match_items
    packages = merged.get('packages') or []
    proposed = [p.get('package_no') for p in packages]
    for index, old, new, _ in identity_updates:
        proposed[index] = new
    keys = [_package_key(p) for p in proposed]
    mapping = {}
    old_keys = [_package_key(p.get('package_no')) for p in packages]
    nonempty_old_keys = [key for key in old_keys if key is not None]
    if identity_updates and (len(keys) != len(set(keys)) or len(nonempty_old_keys) != len(set(nonempty_old_keys))):
        for _, _, _, log_index in identity_updates:
            log[log_index].update(action='keep', reason='整组包号更正后产生重复归属，保留原值待查')
            log[log_index].pop('after', None)
    else:
        mapping = {_package_key(old): new for _, old, new, _ in identity_updates
                   if _package_key(old) is not None or len(packages) == 1}
        for index, _, new, _ in identity_updates:
            packages[index]['package_no'] = new
        for row in [*(merged.get('items') or []), *(merged.get('bidders') or [])]:
            key = _package_key(row.get('package_no'))
            if key in mapping:
                row['package_no'] = mapping[key]
    # 定点关联只应用到明确定位的记录；更换清单时先确认新旧行的一对一对应。
    reference_items = copy.deepcopy(result.get('items') or [])
    for row in reference_items:
        if _package_key(row.get('package_no')) in mapping:
            row['package_no'] = mapping[_package_key(row.get('package_no'))]
    item_map = {j: i for i, j in match_items(merged.get('items') or [], reference_items)}
    if replace or append:
        for old_index, field, new, log_index in field_updates:
            index = item_map.get(old_index)
            if index is None:
                log[log_index].update(action='keep', reason='所选清单中无法确认字段对应行，保留待查')
                log[log_index].pop('after', None)
            else:
                merged['items'][index][field] = new
    known = {_package_key(p.get('package_no')) for p in packages}
    for q, value, log_index in shared_updates:
        key = _package_key(mapping[q['key']]) if q['key'] in mapping else q['key']
        if key not in known:
            log[log_index].update(action='keep',reason='共用品目无法对应已确认包')
            continue
        changed = 0
        for row in merged.get('items') or []:
            if _package_key(row.get('package_no')) == key and not row.get('category'):
                row['category'] = value
                changed += 1
        log[log_index].update(after=value,changed_rows=changed)
    for q, new, log_index in link_updates:
        index = q['row'] if q['collection'] == 'bidders' or not (replace or append) else item_map.get(q['row'])
        if index is None or _package_key(new) not in known:
            log[log_index].update(action='keep', reason='重抽包号未对应已确认包或原记录无法唯一定位，保留待查')
            log[log_index].pop('after', None)
            continue
        merged[q['collection']][index]['package_no'] = new
    return merged, log


def dumps(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False)
