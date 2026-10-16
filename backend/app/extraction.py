"""模型抽取：构建证据输入 → 调模型 → 严格解析 JSON → 规则校验。

分工：实体、字段值与金额（含总金额）由模型抽取；程序只做格式解析（如“万元”字符串转数值）和算术核验标记，不补算字段值。
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from .parsers import AttachmentBundle, doc_priority
from .prompt_v5 import SYSTEM
from .review_prompt import REVIEW_PROMPT_VERSION, REVIEW_SYSTEM

# 抽取提示词版本参与模型缓存键。
PROMPT_VERSION = 'extract-v18-semantic-items-unit-context'

def build_user_message(ann_id: str, html: dict, bundle: AttachmentBundle | None) -> tuple[str | list[dict], dict]:
    """构建完整正文、附件文本和原图输入；不在应用层按字符数截断文本。"""
    body = html['model_text']
    parts = [f'公告ID：{ann_id}', f'标题：{html["title"]}', '【公告正文】', body]
    image_parts, used, skipped = [], [], []
    if bundle and bundle.docs:
        docs = sorted([d for d in bundle.docs if d.text or d.images],
                      key=lambda d: (doc_priority(d.name), len(d.text)))
        for d in docs:
            if d.text:
                parts += [f'【附件：{d.name}】', d.text]
                used.append({'name': d.name, 'kind': d.kind, 'chars': len(d.text), 'truncated': False})
            for image in d.images:
                encoded = base64.b64encode(image['data']).decode('ascii')
                image_parts.append({'type': 'text', 'text': f'【附件原图：{image["name"]}】'})
                image_parts.append({'type': 'image_url', 'image_url': {
                    'url': f"data:{image['mime']};base64,{encoded}"}})
                used.append({'name': image['name'], 'kind': 'image', 'chars': 0, 'truncated': False})
        for d in bundle.docs:
            if not d.text and not d.images:
                skipped.append(f'{d.name}（{d.note or d.kind}）')
    if skipped:
        parts += ['【未能解析的附件】', *skipped,
                  '上述附件未提供可读内容。涉及这些附件的包归属或字段无法确认时，保留为空，不按出现顺序猜测。']
    user_text = '\n'.join(parts)
    user: str | list[dict] = user_text
    if image_parts:
        user = [{'type': 'text', 'text': user_text}] + image_parts
    sources = {'html_used': bool(body.strip()), 'html_chars': len(body),
               'html_truncated': False, 'attachments_used': used,
               'attachments_skipped': skipped, 'image_count': sum(p['type'] == 'image_url' for p in image_parts),
               'attachment_error': bundle.error if bundle else None,
               'prompt_version': PROMPT_VERSION}
    return user, sources


def messages_for(user: str | list[dict], system: str = SYSTEM) -> list[dict[str, Any]]:
    return [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]


def review_messages_for(user: str | list[dict], candidate: dict) -> list[dict[str, Any]]:
    """构造第二次独立请求：复用完整解析输入，并另附首轮候选，不共享对话历史。"""
    candidate_text = (
        '【首轮候选结果（仅供核验，不是事实证据）】\n'
        + json.dumps(candidate, ensure_ascii=False, separators=(',', ':'))
        + '\n请依据本条消息中完整的公告、附件文字/OCR和图片重新核验。'
    )
    if isinstance(user, str):
        review_user: str | list[dict] = f'{user}\n\n{candidate_text}'
    else:
        # 复制列表，避免给首轮消息对象追加内容；原文和图片仍完整复用，不重新解析附件。
        review_user = [*user, {'type': 'text', 'text': candidate_text}]
    return [{'role': 'system', 'content': REVIEW_SYSTEM}, {'role': 'user', 'content': review_user}]


class ParseError(ValueError):
    pass


FENCE_RE = re.compile(r'^\s*```(?:json)?\s*(.*?)\s*```\s*$', re.S)


def parse_json(content: str) -> dict:
    """只接受一个完整的顶层 JSON 对象；允许剥离一层代码围栏。"""
    text = content.strip()
    if m := FENCE_RE.match(text):
        text = m.group(1)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        left, right = max(0, exc.pos - 40), min(len(text), exc.pos + 40)
        excerpt = text[left:right].replace('\r', ' ').replace('\n', ' ')
        raise ParseError(f'模型输出不是合法 JSON（{exc.msg}，第{exc.lineno}行第{exc.colno}列；附近内容：{excerpt!r}）') from exc
    if not isinstance(obj, dict):
        raise ParseError('模型输出不是 JSON 对象')
    return obj


PROJECT_FIELDS = ('buyer', 'project_name', 'total_amount')
PACKAGE_FIELDS = ('package_no', 'package_name', 'winner', 'amount')
ITEM_EVIDENCE_FIELDS = ('package_no', 'item_name', 'category', 'brand', 'model',
                        'unit_price', 'quantity', 'unit', 'total_price')
BIDDER_EVIDENCE_FIELDS = ('package_no', 'name', 'role', 'total_score', 'rank', 'amount')


def validate_output_evidence(obj: dict) -> None:
    """Require the first-pass template and per-field provenance before review or storage."""
    if not isinstance(obj, dict) or not all(k in obj for k in ('project', 'items', 'bidders', 'evidence')):
        raise ParseError('输出缺少project/items/bidders/evidence之一')
    project, evidence = obj['project'], obj['evidence']
    if not isinstance(project, dict) or not isinstance(evidence, list):
        raise ParseError('project必须是对象，evidence必须是扁平数组')
    packages, items, bidders = project.get('packages'), obj.get('items'), obj.get('bidders')
    if not isinstance(packages, list) or not isinstance(items, list) or not isinstance(bidders, list):
        raise ParseError('packages/items/bidders必须是数组')
    expected: dict[str, Any] = {}
    for field in PROJECT_FIELDS:
        if field not in project:
            raise ParseError(f'project缺少字段{field}')
        expected[f'project.{field}'] = project[field]
    for i, row in enumerate(packages):
        if not isinstance(row, dict):
            raise ParseError(f'project.packages[{i}]必须是对象')
        for field in PACKAGE_FIELDS:
            if field not in row:
                raise ParseError(f'packages[{i}]缺少字段{field}')
            expected[f'project.packages[{i}].{field}'] = row[field]

    def collect_rows(rows: list, fields: tuple[str, ...], label: str, width: int) -> None:
        for i, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != width:
                raise ParseError(f'{label}[{i}]必须是{width}列数组')
            for j, field in enumerate(fields):
                expected[f'{label}[{i}].{field}'] = row[j]

    collect_rows(items, ITEM_EVIDENCE_FIELDS, 'items', 9)
    collect_rows(bidders, BIDDER_EVIDENCE_FIELDS, 'bidders', 6)

    supplied: dict[str, Any] = {}
    for i, proof in enumerate(evidence):
        if not isinstance(proof, dict):
            raise ParseError(f'evidence[{i}]必须是对象')
        path = proof.get('path')
        if not isinstance(path, str) or path not in expected:
            raise ParseError(f'evidence[{i}].path不是有效字段路径')
        if path in supplied:
            raise ParseError(f'{path}重复提供证据')
        value = expected[path]
        empty_sentinel = isinstance(value, str) and to_text(value) is None
        if value is None or empty_sentinel:
            raise ParseError(f'{path}为空值，不应生成证据')
        supplied[path] = proof

    for path, value in expected.items():
        empty_sentinel = isinstance(value, str) and to_text(value) is None
        if value is None or empty_sentinel:
            continue
        proof = supplied.get(path)
        if (not isinstance(proof, dict) or not isinstance(proof.get('location'), str)
                or not proof['location'].strip() or not isinstance(proof.get('quote'), str)
                or not proof['quote'].strip()):
            raise ParseError(f'{path}有值但缺少独立的location/quote证据')


# ---------------- 规范化与校验 ----------------
NUM_RE = re.compile(r'-?\d+(?:\.\d+)?')


def to_number(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(',', '').replace('，', '').replace('¥', '').replace('￥', '')
    if not s or s in {'null', 'None', '/', '-', '—', '未披露', '无'}:
        return None
    if '%' in s:
        return None  # 费率不是金额
    mult = 10000.0 if '万' in s else 1.0
    m = NUM_RE.search(s)
    return float(m.group()) * mult if m else None


def to_text(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in {'null', 'none', '/', '-', '—', '无', '未披露'} else None


def norm_org(name: str | None) -> str:
    return re.sub(r'\s+', '', (name or '').replace('（', '(').replace('）', ')'))


ITEM_COLS = ('package_no', 'item_name', 'category', 'brand', 'model', 'unit_price', 'quantity', 'unit', 'total_price')
BIDDER_COLS = ('package_no', 'name', 'role', 'total_score', 'rank', 'amount')


def _as_dict(row: Any, cols: tuple[str, ...]) -> dict | None:
    """模型按紧凑数组输出；也兼容对象格式（旧提示词或模型自行改用对象时）。"""
    if isinstance(row, dict):
        return row
    if isinstance(row, (list, tuple)):
        return {k: (row[i] if i < len(row) else None) for i, k in enumerate(cols)}
    return None


def normalize(obj: dict) -> tuple[dict, list[str]]:
    """把模型输出整理成入库结构，返回 (结果, 待复核原因)。"""
    issues: list[str] = []
    proj = obj.get('project') if isinstance(obj.get('project'), dict) else {}
    packages = []
    for p in proj.get('packages') or []:
        if not isinstance(p, dict):
            continue
        packages.append({'package_no': to_text(p.get('package_no')), 'package_name': to_text(p.get('package_name')),
                         'winner': to_text(p.get('winner')), 'amount': to_number(p.get('amount'))})
    items = []
    for it in obj.get('items') or []:
        it = _as_dict(it, ITEM_COLS)
        if it is None:
            continue
        row = {'package_no': to_text(it.get('package_no')), 'item_name': to_text(it.get('item_name')),
               'category': to_text(it.get('category')), 'brand': to_text(it.get('brand')), 'model': to_text(it.get('model')),
               'unit_price': to_number(it.get('unit_price')), 'quantity': to_number(it.get('quantity')),
               'unit': to_text(it.get('unit')), 'total_price': to_number(it.get('total_price'))}
        if not any(row[k] for k in ('item_name', 'brand', 'model', 'unit_price', 'total_price')):
            continue  # 整行空，丢弃
        up, q, tp = row['unit_price'], row['quantity'], row['total_price']
        if up is not None and q is not None and tp is not None and tp > 0 and abs(up * q - tp) / tp > 0.02:
            issues.append(f'明细「{row["item_name"]}」单价×数量与总价不符')
        items.append(row)
    # 模型偶尔把“包名称”填进明细的包号列（如单包项目只有分包名称、无包号）。包号只用于把明细关联到包，
    # 这里按 packages 把包名称还原为该包的 package_no，避免后续比对/评测把同一包当成不同包。
    name_to_no = {p['package_name']: p['package_no'] for p in packages
                  if p['package_name'] and p['package_name'] != p['package_no']}
    known_nos = {p['package_no'] for p in packages}
    for row in items:
        if row['package_no'] not in known_nos and row['package_no'] in name_to_no:
            row['package_no'] = name_to_no[row['package_no']]
    bidders, seen = [], set()
    for b in obj.get('bidders') or []:
        b = _as_dict(b, BIDDER_COLS)
        if b is None or not to_text(b.get('name')):
            continue
        role = 'winner' if str(b.get('role', '')).strip().lower() in ('winner', 'w', '中标') else 'participant'
        key = (norm_org(b['name']), to_text(b.get('package_no')), role)
        if key in seen:
            continue
        seen.add(key)
        bidders.append({'package_no': to_text(b.get('package_no')), 'name': to_text(b['name']), 'role': role,
                        'total_score': to_number(b.get('total_score')), 'rank': to_number(b.get('rank')),
                        'amount': to_number(b.get('amount'))})
    # 包中标人补入投标主体，保证关系统计不漏中标方
    for p in packages:
        if p['winner'] and not any(b['role'] == 'winner' and norm_org(b['name']) == norm_org(p['winner'])
                                   and (b['package_no'] in (None, p['package_no'])) for b in bidders):
            bidders.append({'package_no': p['package_no'], 'name': p['winner'], 'role': 'winner',
                            'total_score': None, 'rank': None, 'amount': p['amount']})
    total = to_number(proj.get('total_amount'))
    pkg_amounts = [p['amount'] for p in packages if p['amount'] is not None]
    # 总金额只取模型输出（提示词已定义为全部包成交总额），程序不求和补值；只核对并标记不一致
    if total is not None and pkg_amounts and len(pkg_amounts) == len(packages) and abs(sum(pkg_amounts) - total) > max(1.0, total * 0.01):
        issues.append('各包金额合计与总中标金额不一致')
    buyer = to_text(proj.get('buyer'))
    if not buyer:
        issues.append('未识别采购单位')
    if not any(b['role'] == 'winner' for b in bidders):
        issues.append('未识别中标供应商')
    result = {'buyer': buyer, 'project_name': to_text(proj.get('project_name')), 'total_amount': total,
              'packages': packages, 'bidders': bidders, 'items': items}
    return result, issues


# 公告概要中的“品目”值必须像真实品目：品目代码（A/B/C+数字）或“货物/服务/工程/…”路径
_CATEGORY_VALUE = re.compile(r'^(?:[ABC]\d{6,}(?:\s*\S+)?|(?:货物|服务|工程)/\S+)$')
# 按期/按人次计价（人/月、元/考生、年、月…）时单价×数量不等于总价（还要乘期数），不推算
_PERIODIC_UNIT = re.compile(r'/|月|年|天|日|期|次|小时|人')
# 只匹配“去别处看”的整句占位语；“按招标文件要求”“定制”等在原文里作为实际填报值出现，不在此列
_REFERENCE_ONLY = re.compile(r'(?:详见|见)(?:招标|中标|投标|采购|磋商|响应)文件(?:中的)?(?:分项报价表|报价明细表|附件)?[。.]?'
                             r'|按照招标要求提供[。.]?')


def summary_category(html: dict) -> str | None:
    """从公告概要（页面结构化的“公告信息”栏）取唯一的公告级品目。

    只接受“品目”标签紧跟的一个值；多值（逗号分隔，品目与包对应关系不明）或标签后直接是
    下一个标签（如“采购单位”，说明品目为空）时返回 None。这是原文明确公布的字段值，不是推断。
    """
    lines = [ln.strip() for ln in (html.get('summary_text') or '').split('\n')]
    values = [lines[i + 1] for i, ln in enumerate(lines[:-1]) if ln == '品目']
    if len(values) != 1:
        return None
    v = values[0]
    if ',' in v or '，' in v or not _CATEGORY_VALUE.match(v):
        return None
    return v


# 名称里混入的“包1 ”“3 条 ”“，计19条”“ 309 条”：数量被写进了标的名称（规则 19b：名称只抄明细名称）
_NAME_PKG_PREFIX = re.compile(r'^\s*(?:第?[0-9一二三四五六七八九十]+\s*(?:包|标段)|包\s*[0-9一二三四五六七八九十]+)\s+')
_NAME_COUNT_LEAD = re.compile(r'^\s*(\d+)\s*条\s+(?=\S)')
_NAME_COUNT_TAIL = re.compile(r'(?:[，,]\s*计\s*|\s+)(\d+)\s*条\s*$')
_NAME_COUNT_MID = re.compile(r'[，,]\s*计\s*(\d+)\s*条(?=[，,])')


def _split_count_from_name(it: dict) -> None:
    """数量为空、且名称里带“N 条”时，把数量拆回 quantity，名称去掉数量（及紧邻的“包1 ”前缀）。"""
    name = it.get('item_name')
    if not name:
        return
    if it.get('quantity') is not None:
        return  # 已有数量时名称照原样保留，不做任何改写
    new = str(name)
    # “包1 ”前缀只有在后面还拆出了“N 条”时才一并去掉；单独的“4标段 名称”可能就是原文明细名称
    stripped = _NAME_PKG_PREFIX.sub('', new)
    count = None
    for rx in (_NAME_COUNT_LEAD, _NAME_COUNT_TAIL, _NAME_COUNT_MID):
        m = rx.search(stripped)
        if m:
            count = float(m.group(1))
            new = (stripped[:m.start()] + stripped[m.end():]).strip(' ，,')
            break
    if count is None or not new:
        return
    it['item_name'] = new
    it['quantity'] = count


def postprocess(result: dict, html: dict) -> dict:
    """确定性补全（不调用模型、不猜测），只做提示词规则已允许、且原文能直接支撑的两件事：

    1. 规则 12：公告级品目复制到各条标的。模型看不到公告概要栏，常把概要里明确公布的品目漏掉；
       仅当概要栏有唯一品目值、且该条标的 category 为空时填入，模型已填的值一律不改。
    2. 规则 18：同一行单价×数量唯一推算总价。仅在总价为空、单价与数量都有、且数量>1 或单价≥100
       时填写（数量=1 且单价很小多为“单价/费率型”报价，总价并未公布，不推算）。
    """
    cat = summary_category(html)
    for it in result.get('items') or []:
        _split_count_from_name(it)
        # 规则 19a/19c：品牌、型号只填原文值；“详见招标文件”“按照招标要求提供”是指向别处的占位语，不是值
        for k in ('brand', 'model'):
            if it.get(k) and _REFERENCE_ONLY.fullmatch(str(it[k]).strip()):
                it[k] = None
        if cat and not it.get('category'):
            it['category'] = cat
        up, q = it.get('unit_price'), it.get('quantity')
        unit = str(it.get('unit') or '')
        if it.get('total_price') is None and up is not None and q is not None and up > 0 and q > 0 \
                and (q > 1 or up >= 100) and up >= 1 and not _PERIODIC_UNIT.search(unit):
            it['total_price'] = round(up * q, 2)
    return result


def supplier_for_item(item: dict, packages: list[dict]) -> str | None:
    pkg = item.get('package_no')
    if pkg is not None:
        for p in packages:
            if p['package_no'] == pkg:
                return p['winner']
    return packages[0]['winner'] if len(packages) == 1 else None
