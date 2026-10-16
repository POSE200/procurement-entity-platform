"""对第二路已确认的长报价表按页段转写，保留模型给出的字段值。"""
from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath

from . import extraction
from .prompt_b import SYSTEM_B


async def expand(job, provider, html, bundle, candidate, result, chat_json):
    if len(result.get('items') or []) > 1 or not bundle:
        return candidate, result, [], []
    documents = []
    for document in bundle.docs:
        if document.kind != 'pdf' or document.images or len(document.text) < 16000:
            continue
        name = PurePosixPath(document.name.replace('\\', '/')).name
        selected = any(t.get('kind') == '成交明细' and name in str(t.get('where') or '')
                       for t in candidate.get('tables') or [])
        if selected:
            documents.append(document)
    calls, audits, combined = [], [], []
    semaphore = asyncio.Semaphore(2)
    for document in documents:
        pages = []
        for block in re.split(r'(?m)(?=^\[第\d+页\])', document.text):
            match = re.match(r'\[第(\d+)页\]', block)
            if match:
                pages.append((int(match.group(1)), block))
        if len(pages) < 10:
            continue

        async def transcribe(start):
            owned = pages[start:start + 8]
            context = pages[max(0, start - 1):min(len(pages), start + 9)]
            first, last = owned[0][0], owned[-1][0]
            instruction = (
                f'本轮只转写附件《{document.name}》第{first}至{last}页的逐项报价。'
                '公告正文仅用于确认项目、成交包、供应商和品目，不从正文生成项目或包汇总项。'
                '前后相邻页仅用于接续单元格，记录按起始页归属；只输出起始页在负责范围的明细。'
                '按每条货物/服务及其对应价格数量逐条输出，不把整张采购清单归并为项目1批。'
                '本轮输出结构改为仅一个JSON对象：{"items":[{"source_page":1,"package_no":"1",'
                '"item_name":"名称","category":null,"brand":null,"model":null,'
                '"unit_price":null,"quantity":null,"unit":null,"total_price":null}]}。'
                '每条记录在自身对象中填写source_page，使用输入的[第n页]标记，不使用印刷页码。'
                '章节名称、系统分项标题、单项汇总价、小计和合计不作为标的行。'
                '保持全部真实商品服务行，数量、金额、型号按同一行核对，缺失字段填null。')
            user = (f'【公告正文，用于项目身份】\n{html["model_text"]}\n'
                    f'【负责附件，第{first}至{last}页；含相邻页上下文】\n'
                    + '\n'.join(block for _, block in context))
            async with semaphore:
                obj, response = await chat_json(job, provider,
                    [{'role': 'system', 'content': SYSTEM_B + '\n' + instruction},
                     {'role': 'user', 'content': user}], phase='extract_b_chunk')
            selected, origin_pages = [], []
            for item in obj.get('items') or []:
                if not isinstance(item, dict) or not isinstance(item.get('source_page'), int):
                    raise ValueError('页段记录缺少自身起始页，保留原候选并标记复核')
                if not first <= item['source_page'] <= last:
                    continue
                parsed, _ = extraction.normalize({'items':[item]})
                if parsed['items']:
                    selected.extend(parsed['items'])
                    origin_pages.append(item['source_page'])
            return selected, response, {'document': document.name, 'first_page': first,
                                       'last_page': last, 'rows': len(selected),
                                       'row_pages': origin_pages}

        parts = await asyncio.gather(*(transcribe(start) for start in range(0, len(pages), 8)), return_exceptions=True)
        failed = False
        for part in parts:
            if isinstance(part, BaseException):
                audits.append({'document':document.name,'error':type(part).__name__,'message':str(part)[:300]})
                failed = True
                continue
            rows, response, audit = part
            combined.extend(rows)
            calls.append({'role': 'extractor_b_chunk', 'model': response.get('model') or job['model'],
                          'cache_hit': bool(response.get('cache_hit')), 'usage': response.get('usage')})
            audits.append(audit)
        if failed:
            return candidate, result, calls, audits
    if not combined:
        return candidate, result, calls, audits
    updated = {**candidate, 'items': combined}
    normalized, _ = extraction.normalize(updated)
    return updated, normalized, calls, audits
