"""用当前代码重新合并已入库公告：复用存档的 A/B 两路首轮结果，不重新调用抽取模型。

修复了两路比对、后处理等合并阶段的规则后，已抽取的公告不必整篇重抽（贵且结果会有随机波动）：
本模块读取 sources_json.dual 里保存的 a_result / b_result，按当前代码重新做
两路比对 → 原文核验 → 定点重抽（缓存命中不发请求；问题变化时才发一次重抽请求）→ 后处理 → 入库。

新抽取的公告在 worker 入库时已按当前规则处理，并在 sources_json.merge_rules 记下规则版本。
服务启动时 migrate() 在后台自动找出 merge_rules 不是当前版本的旧记录逐篇整理，无需手动操作。

命令行（在 backend 目录下，先关闭网页服务）：
    python -m app.reapply                 # 全部已入库公告
    python -m app.reapply job_xxx job_yyy # 只处理指定任务里成功的公告
"""
from __future__ import annotations

import asyncio
import json
import sys

from . import dual, extraction, ingestion, source_check, storage, worker
from .config import settings
from .llm import create_provider


def _targets(job_ids: list[str] | None) -> list[tuple[dict, str]]:
    if job_ids:
        out = []
        for jid in job_ids:
            job = storage.one('SELECT * FROM jobs WHERE id=?', (jid,))
            if not job:
                raise ValueError(f'任务不存在：{jid}')
            for r in storage.rows("SELECT announcement_id FROM job_tasks WHERE job_id=? AND status='succeeded'", (jid,)):
                out.append((job, r['announcement_id']))
        return out
    out = []
    for r in storage.rows('SELECT announcement_id, job_id FROM announcements'):
        job = storage.one('SELECT * FROM jobs WHERE id=?', (r['job_id'],))
        if job:
            out.append((job, r['announcement_id']))
    return out


async def _one(job: dict, aid: str, provider, stats: dict) -> None:
    row = storage.one('SELECT sources_json FROM announcements WHERE announcement_id=?', (aid,))
    sources = storage.loads(row['sources_json'], {}) if row else {}
    d = sources.get('dual') or {}
    first = d.get('a_result')
    if not first:
        stats['skipped'] += 1  # 旧版本入库、没有存档首轮结果的公告：只能整篇重抽
        return
    dataset = ingestion.get_dataset(job['dataset_id'])
    html, bundle = await worker._parse_async(dataset, aid)
    user, _ = extraction.build_user_message(aid, html, bundle)
    b = d.get('b_result')
    diff = dual.compare(first, b) if b else None
    src = source_check.source_index(html, bundle)
    result, audit, res = await worker._recheck(job, provider, user, json.loads(json.dumps(first)), src, diff)
    issues: list[str] = []
    if audit.get('error'):
        stats['recheck_failed'] += 1
        issues.append('定点重抽请求失败，已保留首轮值')
    pending = [e for e in audit['log'] if e['action'] == 'keep']
    if pending:
        issues.append(f'原文核验仍有 {len(pending)} 个字段待查（已保留首轮值）')
    result = extraction.postprocess(result, html)
    sources['review'] = {**(sources.get('review') or {}), 'audit': audit, 'reapplied': True}
    if diff is not None:
        sources['dual'] = {**d, 'agree': diff['agree'],
                           'disagree_fields': len(diff['fields']) + len(diff['project']),
                           'disagree_packages': len(diff['packages'])}
    model = (res or {}).get('model') or job['model']
    # 保留原抽取时间：平均处理速度按任务创建到最后一篇 extracted_at 计算，重新合并不应改写它
    old = storage.one('SELECT extracted_at, job_id, sources_json FROM announcements WHERE announcement_id=?', (aid,))
    # 整理期间这篇若已被新任务重新抽取（任务不同或已是当前规则），以新结果为准，不再覆盖
    if old and (old['job_id'] != job['id']
                or storage.loads(old['sources_json'], {}).get('merge_rules') == worker.MERGE_RULES_VERSION):
        stats['skipped'] += 1
        return
    worker._store(job, aid, html, bundle, result, issues, sources, model)
    if old and old['extracted_at']:
        with storage.db() as conn:
            conn.execute('UPDATE announcements SET extracted_at=? WHERE announcement_id=?',
                         (old['extracted_at'], aid))
    stats['done'] += 1


async def reapply(job_ids: list[str] | None = None, concurrency: int = 4) -> dict:
    storage.init_db()
    stats = {'done': 0, 'skipped': 0, 'recheck_failed': 0, 'failed': 0, 'errors': []}
    targets = _targets(job_ids)
    sem = asyncio.Semaphore(max(1, concurrency))
    providers: dict[str, object] = {}

    async def run(job: dict, aid: str) -> None:
        async with sem:
            try:
                if job['provider'] not in providers:
                    providers[job['provider']] = create_provider(settings, job['provider'])
                await _one(job, aid, providers[job['provider']], stats)
            except Exception as exc:  # 单篇失败不影响其他公告，原结果保持不变
                stats['failed'] += 1
                stats['errors'].append({'announcement_id': aid, 'error': type(exc).__name__})

    await asyncio.gather(*(run(j, a) for j, a in targets))
    stats['total'] = len(targets)
    stats['errors'] = stats['errors'][:20]
    return stats


# 启动时自动迁移的进度，供任务一总览页显示“正在按当前规则整理旧记录”
progress: dict = {'running': False, 'total': 0, 'done': 0, 'failed': 0, 'rules': worker.MERGE_RULES_VERSION}


def _stale() -> list[tuple[dict, str]]:
    """合并规则版本不是当前版本、且存有首轮 A 路结果的已入库公告。"""
    out = []
    jobs: dict[str, dict | None] = {}
    for r in storage.rows("""SELECT announcement_id, job_id FROM announcements
                             WHERE COALESCE(json_extract(sources_json, '$.merge_rules'), '') != ?
                               AND json_extract(sources_json, '$.dual.a_result') IS NOT NULL""",
                          (worker.MERGE_RULES_VERSION,)):
        if r['job_id'] not in jobs:
            jobs[r['job_id']] = storage.one('SELECT * FROM jobs WHERE id=?', (r['job_id'],))
        if jobs[r['job_id']]:
            out.append((jobs[r['job_id']], r['announcement_id']))
    return out


async def _migrate(concurrency: int) -> None:
    targets = _stale()
    progress.update(running=bool(targets), total=len(targets), done=0, failed=0)
    if not targets:
        return
    stats = {'done': 0, 'skipped': 0, 'recheck_failed': 0, 'failed': 0, 'errors': []}
    sem = asyncio.Semaphore(max(1, concurrency))
    providers: dict[str, object] = {}

    async def run(job: dict, aid: str) -> None:
        async with sem:
            try:
                if job['provider'] not in providers:
                    providers[job['provider']] = create_provider(settings, job['provider'])
                # 单篇最多等 3 分钟，避免个别请求卡住整个迁移；失败的保留原结果，下次启动再试
                await asyncio.wait_for(_one(job, aid, providers[job['provider']], stats), 180)
            except Exception:  # noqa: BLE001
                progress['failed'] += 1
            finally:
                progress['done'] += 1

    try:
        await asyncio.gather(*(run(j, a) for j, a in targets))
    finally:
        progress['running'] = False


def migrate() -> None:
    """服务启动时在后台线程调用：把旧规则入库的公告自动整理到当前合并规则。"""
    try:
        asyncio.run(_migrate(max(1, settings.llm_max_concurrency)))
    except Exception:  # noqa: BLE001 — 迁移失败不影响服务，旧记录保持原样
        progress['running'] = False


def main(argv: list[str]) -> int:
    print(json.dumps(asyncio.run(reapply(argv or None)), ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
