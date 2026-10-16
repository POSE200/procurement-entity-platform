"""后台任务执行器：单进程内的一个守护线程 + asyncio 并发调用模型。

- 任务状态全部存在 SQLite（job_tasks），服务重启后未完成的任务自动恢复排队。
- 每篇公告依次经过 parse → extract → validate → store 四个阶段；关系查询直接读 SQL，
  因此 graph（Neo4j 投影）阶段标记为跳过。
"""
from __future__ import annotations
from pathlib import Path

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

from . import extraction, ingestion, parsers, storage
from .config import settings
from .llm import create_provider
from .llm.openai_compatible import ModelOutputError
from . import recheck, source_check
from . import dual
from . import long_tables
from .prompt_b import PROMPT_B_VERSION, SYSTEM_B
from .recheck_prompt import RECHECK_PROMPT_VERSION

# 合并阶段规则版本（两路比对 dual、原文核验、定点重抽、extraction.postprocess）。
# 修改这些规则时把版本号改掉：服务启动后会自动把旧版本入库的公告按新规则重新合并（不重新抽取）。
MERGE_RULES_VERSION = 'merge-20261010'

STAGES = ['parse', 'extract', 'review', 'validate', 'store']
_wake = threading.Event()
_started = False
_timing_lock = threading.Lock()


class TaskFailure(Exception):
    def __init__(self, stage: str, code: str, message: str, retryable: bool = True):
        super().__init__(message)
        self.stage, self.code, self.retryable = stage, code, retryable


# ---------------- 任务创建与查询 ----------------
def create_job(dataset_id: str, only: list[str] | None = None, limit: int | None = None,
               bypass_cache: bool = False) -> str:
    if only:
        inv = ingestion.inventory(dataset_id)
        if not inv:
            raise ValueError('数据集不存在')
        entries = {e['announcement_id']: e for e in inv['entries']}
        invalid = [aid for aid in only if aid not in entries or entries[aid]['status'] not in ('paired', 'html_only')]
        active = [aid for aid in only if aid in entries and entries[aid].get('processing_status') in ('queued', 'running')]
        if invalid:
            raise ValueError(f'指定公告不属于该数据集的可处理公告：{", ".join(invalid[:5])}')
        if active:
            raise ValueError(f'指定公告已有任务正在排队或运行：{", ".join(active[:5])}')
        ids = list(dict.fromkeys(only))
    else:
        ids = ingestion.processable_ids(dataset_id)
    if limit:
        ids = ids[:limit]
    if not ids:
        raise ValueError('该数据集没有可处理的公告')
    job_id = ingestion.new_id('job')
    ts = storage.now()
    provider = settings.provider_name()
    with storage.db() as c:
        c.execute('INSERT INTO jobs(id, dataset_id, status, provider, model, created_at, updated_at, bypass_cache) VALUES(?,?,?,?,?,?,?,?)',
                  (job_id, dataset_id, 'queued', provider, settings.model_for(provider), ts, ts, int(bypass_cache)))
        c.executemany('INSERT INTO job_tasks(job_id, announcement_id, status) VALUES(?,?,?)',
                      [(job_id, aid, 'queued') for aid in ids])
    _wake.set()
    return job_id


def retry(job_id: str, announcement_ids: list[str], include_succeeded: bool = False) -> None:
    with storage.db() as c:
        c.executemany("""UPDATE job_tasks SET status='queued', progress=0, failed_stage=NULL, error_code=NULL,
                         message=NULL WHERE job_id=? AND announcement_id=?
                         AND ((status='failed' AND retryable=1) OR (status='succeeded' AND ?))""",
                      [(job_id, a, int(include_succeeded)) for a in announcement_ids])
        c.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (storage.now(), job_id))
    _wake.set()


def job_view(job_id: str) -> dict | None:
    job = storage.one('SELECT * FROM jobs WHERE id=?', (job_id,))
    if not job:
        return None
    tasks = storage.rows('SELECT * FROM job_tasks WHERE job_id=?', (job_id,))
    total = len(tasks)
    stage_ms = json.loads(job['stage_ms_json'] or '{}')
    active = any(t['status'] in ('queued', 'running') for t in tasks)
    stages = []
    for i, name in enumerate(STAGES):
        done = sum(1 for t in tasks if t['progress'] > i)
        skipped = sum(1 for t in tasks if t['status'] == 'skipped')
        if name == 'store':
            done += skipped
            skipped = 0
        failed = sum(1 for t in tasks if t['failed_stage'] == name)
        running = any(t['status'] == 'running' and t['progress'] == i for t in tasks)
        if done + failed + skipped == total:
            status = 'failed' if failed == total else 'succeeded'
        elif running or (active and done):
            status = 'running'
        else:
            status = 'pending'
        stages.append({'name': name, 'status': status, 'done': done, 'failed': failed, 'skipped': skipped, 'total': total,
                       'elapsed_ms': stage_ms.get(name)})
    stages.append({'name': 'graph', 'status': 'skipped', 'done': 0, 'failed': 0, 'total': 0, 'elapsed_ms': None})
    succeeded = sum(1 for t in tasks if t['status'] == 'succeeded')
    failed = sum(1 for t in tasks if t['status'] == 'failed')
    skipped = sum(1 for t in tasks if t['status'] == 'skipped')
    if job['status'] == 'paused' and not any(t['status'] == 'running' for t in tasks):
        status = 'paused'
    elif active:
        status = 'running' if any(t['status'] != 'queued' for t in tasks) else 'queued'
    elif skipped:
        status = 'partial'
    elif failed == 0:
        status = 'succeeded'
    elif succeeded == 0:
        status = 'failed'
    else:
        status = 'partial'
    failures = [{'announcement_id': t['announcement_id'], 'stage': t['failed_stage'], 'error_code': t['error_code'],
                 'message': t['message'], 'retryable': bool(t['retryable'])} for t in tasks if t['status'] == 'failed']
    # 实际经过时间（墙钟）与吞吐；各阶段 elapsed_ms 是并发任务的累计耗时，会大于墙钟时间
    from datetime import datetime
    start = datetime.fromisoformat(job['created_at'])
    end = datetime.fromisoformat(job['updated_at']) if not active else datetime.now(start.tzinfo)
    wall_s = max(0.0, (end - start).total_seconds())
    finished = succeeded + failed + skipped
    return {'display_name': next((r['display_name'] for r in list_jobs() if r['job_id']==job_id), job_id),
            'job_id': job['id'], 'dataset_id': job['dataset_id'], 'status': status, 'provider': job['provider'],
            'model': job['model'], 'created_at': job['created_at'], 'updated_at': job['updated_at'], 'total': total,
            'bypass_cache': bool(job['bypass_cache']) if 'bypass_cache' in job.keys() else False,
            'wall_seconds': round(wall_s, 1),
            'per_minute': round(finished / wall_s * 60, 1) if wall_s > 1 and finished else None,
            'eta_seconds': round((total - finished) / (finished / wall_s)) if active and finished and wall_s > 1 else None,
            'succeeded': succeeded, 'failed': failed, 'needs_review': sum(1 for t in tasks if t['needs_review']),
            'skipped': skipped, 'stored': succeeded + skipped,
            'placeholders': [{'announcement_id': t['announcement_id'], 'message': t['message']}
                             for t in tasks if t['status'] == 'skipped'],
            'paused': sum(1 for t in tasks if t['status'] == 'paused'),
            'stages': stages, 'failures': failures}


def list_jobs(limit: int | None = None) -> list[dict]:
    rows = storage.rows("""SELECT j.id AS job_id,j.dataset_id,j.provider,j.model,j.created_at,
        COUNT(t.announcement_id) AS total,
        SUM(CASE WHEN t.status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
        SUM(CASE WHEN t.status='skipped' THEN 1 ELSE 0 END) AS skipped,
        SUM(CASE WHEN t.status='failed' THEN 1 ELSE 0 END) AS failed,
        SUM(CASE WHEN t.status IN ('queued','running') THEN 1 ELSE 0 END) AS active,
        d.html_zip FROM jobs j LEFT JOIN job_tasks t ON t.job_id=j.id
        LEFT JOIN datasets d ON d.id=j.dataset_id
        GROUP BY j.id,d.html_zip ORDER BY j.created_at DESC""")
    for row in rows:
        source = Path(row.pop('html_zip') or '').name
        row['is_official'] = (source.startswith('赛题五.基准测试数据_html') or (row['dataset_id']=='ds_20261014_43c21f' and row['total']==1038))
        row['display_name'] = (f"官方数据集全量提取（{row['total']}篇）" if row['is_official'] else
                               f"导入提取任务（{row['total']}篇）· {row['job_id'].rsplit('_',1)[-1]}")
        row['stored'] = row['succeeded'] + row['skipped']
    return rows[:limit] if limit else rows


# ---------------- 执行 ----------------
def _set_task(job_id: str, aid: str, **fields: Any) -> None:
    cols = ', '.join(f'{k}=?' for k in fields)
    with storage.db() as c:
        c.execute(f'UPDATE job_tasks SET {cols} WHERE job_id=? AND announcement_id=?', (*fields.values(), job_id, aid))
        c.execute('UPDATE jobs SET updated_at=? WHERE id=?', (storage.now(), job_id))


def _add_ms(job_id: str, stage: str, ms: float) -> None:
    with _timing_lock, storage.db() as c:
        row = c.execute('SELECT stage_ms_json FROM jobs WHERE id=?', (job_id,)).fetchone()
        d = json.loads(row['stage_ms_json'] or '{}')
        d[stage] = round(d.get(stage, 0) + ms, 1)
        c.execute('UPDATE jobs SET stage_ms_json=? WHERE id=?', (json.dumps(d), job_id))


def _cache_key(job: dict, messages: list[dict], phase: str = 'extract') -> str:
    prompt_version = {'extract': extraction.PROMPT_VERSION, 'extract_b': PROMPT_B_VERSION,
                      'extract_b_chunk': PROMPT_B_VERSION}.get(phase, RECHECK_PROMPT_VERSION)
    payload = [phase, job['provider'], job['model'], prompt_version, messages]
    if job.get('bypass_cache'):
        payload.append({'fresh_job': job['id']})
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


async def _chat_json(job: dict, provider, messages: list[dict], phase: str = 'extract') -> tuple[dict, dict]:
    """单个流水线阶段最多发一个请求；抽取和定点重抽分别缓存完整合法 JSON。"""
    key = _cache_key(job, messages, phase)
    failed_stage = 'review' if phase == 'review' else 'extract'
    cached = storage.one('SELECT content, model FROM llm_cache WHERE key=?', (key,))
    if cached:
        res = {'content': cached['content'], 'model': cached['model'], 'finish_reason': 'stop',
               'usage': None, 'cache_hit': True}
        return extraction.parse_json(res['content']), res

    try:
        # 长清单公告（数百行明细）的完整抽取 JSON 可能很长，保留足够输出预算。
        max_tokens = 32768
        res = await provider.chat(messages, max_tokens=max_tokens)
    except ModelOutputError as exc:
        raise TaskFailure(failed_stage, exc.code, str(exc), exc.retryable) from exc
    if res['finish_reason'] == 'length':
        usage = res.get('usage') or {}
        completion_tokens = usage.get('completion_tokens')
        content = res.get('content') or ''
        detail = f"finish_reason=length, 输出字符数={len(content)}, max_tokens={max_tokens}"
        if completion_tokens is not None:
            detail += f", completion_tokens={completion_tokens}"
        if res.get('request_id'):
            detail += f", request_id={res['request_id']}"
        raise TaskFailure(failed_stage, 'output_truncated',
                          f'模型输出被长度上限截断，结果不完整（{detail}）')
    try:
        obj = extraction.parse_json(res['content'])
    except extraction.ParseError as exc:
        content = res.get('content') or ''
        detail = '模型返回空内容' if not content.strip() else str(exc)
        meta = f"finish_reason={res.get('finish_reason') or 'unknown'}, 输出字符数={len(content)}"
        if res.get('request_id'):
            meta += f", request_id={res['request_id']}"
        raise TaskFailure(failed_stage, 'invalid_json', f'模型输出无法解析：{detail}（{meta}；本阶段未自动重问）')

    with storage.db() as c:
        c.execute('INSERT OR REPLACE INTO llm_cache(key, content, model, created_at) VALUES(?,?,?,?)',
                  (key, json.dumps(obj, ensure_ascii=False), res.get('model') or job['model'], storage.now()))
    res['cache_hit'] = False
    return obj, res


async def _extract_validated(job: dict, provider, messages: list[dict], semaphore=None):
    """Call once and normalize the structured response; do not infer item validity by string matching."""
    obj, res = await _chat_json(job, provider, messages)
    result, issues = extraction.normalize(obj)
    return result, issues, res


async def _recheck(job: dict, provider, source_user: str | list[dict], result: dict, src,
                   diff: dict | None = None) -> tuple[dict, dict, dict | None]:
    """方案一+二：原文核对的可疑字段 + A/B 两路分歧 → 定点重抽 → 有可核实原文依据才采用。

    没有可疑字段也没有分歧时不发请求。只来自原文核对的问题不提供首轮值；
    两路分歧给出两个候选值。合并时依据不足一律保留 A 的值。
    """
    flags = source_check.check(result, src)
    questions = recheck.build_questions(result, flags, diff)
    audit: dict = {'flags': flags,
                   'questions': [{k: q.get(k) for k in ('id', 'kind', 'row', 'field', 'key', 'codes')} for q in questions],
                   'log': []}
    if not questions:
        return result, audit, None
    messages = recheck.messages_for(extraction.SYSTEM, source_user, questions)
    try:
        answers, res = await _chat_json(job, provider, messages, phase='review')
    except TaskFailure as exc:
        # 重抽只是纠错层：它失败时保留首轮结果入库（不自动重问），并标记待复核
        audit['error'] = {'code': exc.code, 'message': str(exc)}
        audit['log'] = [{'row': q.get('row'), 'field': q.get('field') or f'package[{q.get("key")}]',
                         'codes': q['codes'], 'action': 'keep',
                         'reason': '定点重抽请求失败，保留首轮值'} for q in questions]
        return result, audit, None
    merged, log = recheck.merge(result, questions, answers, src)
    audit['log'] = log
    remaining = source_check.check(merged, src)
    closure = [f for f in remaining if f.get('code') in ('arith_mismatch', 'inventory_incomplete')]
    if closure:
        followup = recheck.build_questions(merged, closure)
        audit['followup_questions'] = [{k: q.get(k) for k in ('id', 'kind', 'row', 'field', 'key', 'codes')}
                                       for q in followup]
        follow_messages = recheck.messages_for(extraction.SYSTEM, source_user, followup)
        follow_messages[-1]['content'] += '\n【复核后检查】这些矛盾在上一轮后仍存在。请重新定位对应原图或真实分项表；原文有冲突时明确标记source_conflict，不为凑算式修改数值。'
        try:
            new_answers, new_res = await _chat_json(job, provider, follow_messages, phase='review')
            merged, new_log = recheck.merge(merged, followup, new_answers, src)
            audit['followup_log'] = new_log
            audit['log'].extend(dict(entry, pass_no=2) for entry in new_log)
            res = dict(res)
            res['followup_calls'] = [{'role': 'recheck_closure', 'model': new_res.get('model') or job['model'],
                                      'cache_hit': bool(new_res.get('cache_hit')), 'usage': new_res.get('usage')}]
        except TaskFailure as exc:
            audit['followup_error'] = {'code': exc.code, 'message': str(exc)}
    audit['remaining_flags'] = source_check.check(merged, src)
    return merged, audit, res


def _diff_model_values(before: dict, after: dict) -> dict:
    """Build a small local change log; the reviewer itself returns only the first-call schema."""
    changes = []

    def compare(path: str, old: Any, new: Any) -> None:
        if old != new:
            changes.append({'path': path, 'before': old, 'after': new})

    for field in extraction.PROJECT_FIELDS:
        compare(f'project.{field}', before.get('project', {}).get(field), after.get('project', {}).get(field))
    old_project = before.get('project', {})
    new_project = after.get('project', {})
    def compare_rows(label: str, old_rows: Any, new_rows: Any,
                     fields: tuple[str, ...], objects: bool) -> None:
        old_rows = old_rows if isinstance(old_rows, list) else []
        new_rows = new_rows if isinstance(new_rows, list) else []
        for i in range(max(len(old_rows), len(new_rows))):
            if i >= len(old_rows):
                changes.append({'path': f'{label}[{i}]', 'before': None, 'after': new_rows[i]})
                continue
            if i >= len(new_rows):
                changes.append({'path': f'{label}[{i}]', 'before': old_rows[i], 'after': None})
                continue
            old_row, new_row = old_rows[i], new_rows[i]
            for j, field in enumerate(fields):
                old_value = old_row.get(field) if objects and isinstance(old_row, dict) else (
                    old_row[j] if not objects and isinstance(old_row, list) and j < len(old_row) else None)
                new_value = new_row.get(field) if objects and isinstance(new_row, dict) else (
                    new_row[j] if not objects and isinstance(new_row, list) and j < len(new_row) else None)
                compare(f'{label}[{i}].{field}', old_value, new_value)

    compare_rows('project.packages', old_project.get('packages'), new_project.get('packages'),
                 extraction.PACKAGE_FIELDS, objects=True)
    compare_rows('items', before.get('items'), after.get('items'), extraction.ITEM_EVIDENCE_FIELDS, objects=False)
    compare_rows('bidders', before.get('bidders'), after.get('bidders'), extraction.BIDDER_EVIDENCE_FIELDS,
                 objects=False)
    return {'corrections': changes, 'removed': [], 'added': [], 'uncertain': []}


def _parse(dataset: dict, aid: str) -> tuple[dict, parsers.AttachmentBundle | None]:
    try:
        html = parsers.parse_html(ingestion.read_html(dataset, aid))
    except FileNotFoundError:
        raise TaskFailure('parse', 'html_missing', '数据集中找不到该公告 HTML', retryable=False)
    if len(html['model_text']) < 50:
        raise TaskFailure('parse', 'html_empty', '公告正文为空或无法解析（可能是错误页/验证码页）', retryable=False)
    raw = ingestion.read_attachment(dataset, aid)
    return html, (parsers.parse_attachment_zip(raw) if raw else None)


def _parse_in_child(html_zip: str, attachment_zip: str | None, aid: str):
    """子进程入口：解析是 CPU 密集型（PDF），放进进程池可真正并行，不受 GIL 限制。"""
    return _parse({'html_zip': html_zip, 'attachment_zip': attachment_zip}, aid)


_pool: ProcessPoolExecutor | None = None
_pool_broken = False


def _parse_pool() -> ProcessPoolExecutor | None:
    """ICT_PARSE_WORKERS=0 时不用进程池（测试环境）；默认 CPU 核数-1，最多 6 个。"""
    global _pool
    n = int(os.environ.get('ICT_PARSE_WORKERS') or max(1, min(6, (os.cpu_count() or 2) - 1)))
    if n <= 0:
        return None
    if _pool is None and not _pool_broken:
        _pool = ProcessPoolExecutor(max_workers=n)
    return _pool


async def _parse_async(dataset: dict, aid: str):
    global _pool, _pool_broken
    pool = _parse_pool()
    if pool is not None:
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(pool, _parse_in_child, dataset['html_zip'], dataset['attachment_zip'], aid)
        except BrokenProcessPool:
            _pool_broken, _pool = True, None  # 进程池不可用时回退到线程，保证任务能继续
    return await asyncio.to_thread(_parse, dataset, aid)


async def _run_task(job: dict, dataset: dict, aid: str) -> None:
    job_id = job['id']
    _set_task(job_id, aid, status='running', progress=0, attempts=1)
    stage = 'parse'
    try:
        t0 = time.perf_counter()
        html, bundle = await _parse_async(dataset, aid)
        _add_ms(job_id, 'parse', (time.perf_counter() - t0) * 1000)
        _set_task(job_id, aid, progress=1)

        stage = 'extract'
        t0 = time.perf_counter()
        user, sources = extraction.build_user_message(aid, html, bundle)
        provider = create_provider(settings, job['provider'])
        # 方案二：A（V16r2）与 B（盘点-转写）两路独立上下文并发抽取；B 失败不影响 A
        a_call = _chat_json(job, provider, extraction.messages_for(user), phase='extract')
        b_call = _chat_json(job, provider, extraction.messages_for(user, SYSTEM_B), phase='extract_b')
        a_out, b_out = await asyncio.gather(a_call, b_call, return_exceptions=True)
        if isinstance(a_out, BaseException):
            raise a_out
        candidate, extract_res = a_out
        first, issues = extraction.normalize(candidate)
        b_result, b_res, b_error, diff = None, None, None, None
        chunk_calls, chunk_audit = [], []
        if isinstance(b_out, BaseException):
            b_error = {'code': getattr(b_out, 'code', type(b_out).__name__), 'message': str(b_out)}
        else:
            b_candidate, b_res = b_out
            b_result, _ = extraction.normalize(b_candidate)
            try:
                b_candidate, b_result, chunk_calls, chunk_audit = await long_tables.expand(
                    job, provider, html, bundle, b_candidate, b_result, _chat_json)
                if any(scope.get('error') for scope in chunk_audit):
                    issues.append('长报价表页段转写未完成，保留原候选待核验')
            except Exception as error:
                chunk_audit = [{'error': type(error).__name__, 'message': str(error)[:300]}]
                issues.append('长报价表页段转写未完成，保留原候选待核验')
            diff = dual.compare(first, b_result)
        _add_ms(job_id, 'extract', (time.perf_counter() - t0) * 1000)
        _set_task(job_id, aid, progress=2)

        stage = 'review'
        t0 = time.perf_counter()
        src = source_check.source_index(html, bundle)
        src.inventory_tables = (b_candidate.get('tables') or []) if b_result is not None else []
        src.candidate_items = (b_result.get('items') or []) if b_result is not None else []
        result, audit, recheck_res = await _recheck(job, provider, user, first, src, diff)
        _add_ms(job_id, 'review', (time.perf_counter() - t0) * 1000)
        _set_task(job_id, aid, progress=3)

        stage = 'validate'
        t0 = time.perf_counter()
        calls = [{'role': 'extractor', 'model': extract_res.get('model') or job['model'],
                  'cache_hit': bool(extract_res.get('cache_hit')), 'usage': extract_res.get('usage'),
                  'image_adaptations': extract_res.get('image_adaptations', [])}]
        if b_res is not None:
            calls.append({'role': 'extractor_b', 'model': b_res.get('model') or job['model'],
                          'cache_hit': bool(b_res.get('cache_hit')), 'usage': b_res.get('usage'),
                          'image_adaptations': b_res.get('image_adaptations', [])})
        calls.extend(chunk_calls)
        sources['dual'] = {
            'prompt_b_version': PROMPT_B_VERSION,
            'error': b_error,
            'tables': (b_candidate.get('tables') if b_result is not None and isinstance(b_candidate, dict) else None),
            'agree': diff['agree'] if diff else None,
            'disagree_fields': len(diff['fields']) + len(diff['project']) if diff else None,
            'disagree_packages': len(diff['packages']) if diff else None,
            'b_items': len(b_result['items']) if b_result else None,
            'page_scopes': chunk_audit,
            # 首轮 A、B 原样存档，便于分开评估“合并前/合并后”
            'a_result': first, 'b_result': b_result,
        }
        if b_error:
            issues.append('第二路抽取失败，本篇只用了单路结果')
        if recheck_res is not None:
            calls.append({'role': 'recheck', 'model': recheck_res.get('model') or job['model'],
                          'cache_hit': bool(recheck_res.get('cache_hit')), 'usage': recheck_res.get('usage'),
                          'image_adaptations': recheck_res.get('image_adaptations', [])})
            calls.extend(recheck_res.get('followup_calls') or [])
        sources['review'] = {
            'prompt_version': RECHECK_PROMPT_VERSION,
            'api_call_count': sum(int(not c['cache_hit']) for c in calls),
            'calls': calls,
            'audit': audit,
        }
        pending = [e for e in audit['log'] if e['action'] == 'keep']
        if pending:
            issues.append(f'原文核验仍有 {len(pending)} 个字段待查（已保留首轮值）')
        unresolved = [f for f in audit.get('remaining_flags', []) if f.get('code') in ('arith_mismatch', 'inventory_incomplete')]
        if unresolved:
            issues.append(f'复核后仍有 {len(unresolved)} 处数值或清单疑点，保留原文核验记录')
        _add_ms(job_id, 'validate', (time.perf_counter() - t0) * 1000)
        _set_task(job_id, aid, progress=4)

        # 确定性补全：公告概要栏的唯一品目复制到空品目行；同行单价×数量补总价（见 extraction.postprocess）
        result = extraction.postprocess(result, html)

        stage = 'store'
        t0 = time.perf_counter()
        _store(job, aid, html, bundle, result, issues, sources,
               (recheck_res or extract_res).get('model') or job['model'])
        _add_ms(job_id, 'store', (time.perf_counter() - t0) * 1000)
        _set_task(job_id, aid, progress=5, status='succeeded', needs_review=1 if issues else 0,
                  message='；'.join(issues) or None)
    except TaskFailure as f:
        _set_task(job_id, aid, status='failed', failed_stage=f.stage, error_code=f.code, message=str(f), retryable=int(f.retryable))
    except ModelOutputError as e:
        _set_task(job_id, aid, status='failed', failed_stage='extract', error_code=e.code, message=str(e), retryable=int(e.retryable))
    except extraction.ParseError as e:
        _set_task(job_id, aid, status='failed', failed_stage='extract', error_code='invalid_json', message=str(e), retryable=1)
    except Exception as e:  # 未预期错误：记录类型，不泄露内部细节
        logging.getLogger(__name__).exception('任务处理异常 job=%s announcement=%s stage=%s', job_id, aid, stage)
        _set_task(job_id, aid, status='failed', failed_stage=stage, error_code=type(e).__name__,
                  message=f'读取返回数据时缺少字段：{e}' if isinstance(e, KeyError) else '处理时发生未预期错误', retryable=1)


def _store(job: dict, aid: str, html: dict, bundle, result: dict, issues: list[str], sources: dict, model: str) -> None:
    # 标记本条记录按哪一版合并规则入库；启动时 reapply.migrate 会把旧版本记录自动整理到当前规则
    sources['merge_rules'] = MERGE_RULES_VERSION
    if html.get('channel'):
        sources['channel'] = html['channel']  # 标准样例表导出时复原源地址 URL 用
    with storage.db() as c:
        c.execute('DELETE FROM items WHERE announcement_id=?', (aid,))  # 同一公告重跑时覆盖旧明细，不重复计数
        c.execute("""INSERT OR REPLACE INTO announcements(announcement_id, dataset_id, job_id, title, publish_time, project_no,
                     buyer, project_name, total_amount, packages_json, bidders_json, has_attachment, summary_text, body_text,
                     review_note, model, extracted_at, needs_review, sources_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (aid, job['dataset_id'], job['id'], html['title'] or aid, html['publish_time'], html['project_no'],
                   result['buyer'], result['project_name'], result['total_amount'],
                   json.dumps(result['packages'], ensure_ascii=False), json.dumps(result['bidders'], ensure_ascii=False),
                   1 if bundle else 0, html['summary_text'], html['body_text'], '；'.join(issues) or None, model,
                   storage.now(), 1 if issues else 0, json.dumps(sources, ensure_ascii=False)))
        c.executemany("""INSERT INTO items(item_id, announcement_id, package_no, item_name, category, brand, model, unit_price,
                         quantity, unit, total_price, supplier) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      [(f'{aid}#{i + 1}', aid, it['package_no'], it['item_name'], it['category'], it['brand'], it['model'],
                        it['unit_price'], it['quantity'], it['unit'], it['total_price'],
                        extraction.supplier_for_item(it, result['packages'])) for i, it in enumerate(result['items'])])
    storage.bump_revision()


async def _loop() -> None:
    running: set[asyncio.Task] = set()
    while True:
        limit = max(1, settings.llm_max_concurrency)
        if len(running) < limit:
            from .cloud_runtime import claim_tasks
            batch = claim_tasks(limit - len(running))
            for row in batch:
                job = storage.one('SELECT * FROM jobs WHERE id=?', (row['job_id'],))
                dataset = ingestion.get_dataset(job['dataset_id'])
                _set_task(row['job_id'], row['announcement_id'], status='running')
                t = asyncio.create_task(_run_task(job, dataset, row['announcement_id']))
                running.add(t)
                t.add_done_callback(running.discard)
            if not batch and not running:
                await asyncio.to_thread(_wake.wait, 5)
                _wake.clear()
                continue
        await asyncio.sleep(0.3)


def start() -> None:
    """服务启动时调用：恢复中断的任务并启动后台线程（重复调用无副作用）。"""
    global _started
    if _started:
        return
    _started = True
    with storage.db() as c:
        c.execute("UPDATE job_tasks SET status='queued', progress=0 WHERE status='running'")
    threading.Thread(target=lambda: asyncio.run(_loop()), name='extract-worker', daemon=True).start()
