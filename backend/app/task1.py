"""任务一评分总览，按赛题的四项指标汇总当前抽取结果。

覆盖率是公告原文/附件两种来源；提取率按模型输出明细的七个关键字段数，
依赛题分档计分。人工答案非空字段的逐字段提取情况另行展示；字段值是否正确
由提取准确性单独评价。处理速率按每个任务的墙钟时间计算。
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from datetime import datetime

from . import evaluation, storage

FIELDS = evaluation.FIELDS  # [(key, 中文名)] 7 个关键字段

_eval_lock = threading.Lock()
_eval_cache: tuple[int, str | None, dict] | None = None


def _norm(s: str | None) -> str:
    return re.sub(r'\s+', '', s or '')


def coverage(job_id: str | None = None) -> dict:
    """按已解析并送入模型的来源统计标的覆盖；同一条明细可同时覆盖正文与附件。"""
    ann_sql = 'SELECT announcement_id, has_attachment, body_text, summary_text, sources_json FROM announcements'
    ann_params: tuple = ()
    if job_id:
        ann_sql += " WHERE announcement_id IN (SELECT announcement_id FROM job_tasks WHERE job_id=? AND status IN ('succeeded','skipped'))"
        ann_params = (job_id,)
    anns = {r['announcement_id']: r for r in storage.rows(ann_sql, ann_params)}
    html_used: set[str] = set()
    used_att: set[str] = set()
    kinds: Counter = Counter()
    image_anns = 0
    for aid, a in anns.items():
        s = storage.loads(a['sources_json'], {})
        # 新任务用明确记录；兼容旧任务时，以已保存的正文/概要作为正文曾被解析并输入的依据。
        if s.get('html_used', bool(_norm(a['body_text']) or _norm(a['summary_text']))):
            html_used.add(aid)
        used = s.get('attachments_used') or []
        if used or s.get('image_count'):
            used_att.add(aid)
        if s.get('image_count'):
            image_anns += 1
        for u in used:
            kinds[(u.get('kind') if isinstance(u, dict) else None) or 'other'] += 1
    from_html = from_att = unknown = 0
    item_total = 0
    html_anns: set[str] = set()
    att_anns: set[str] = set()
    item_sql = 'SELECT i.announcement_id, i.item_name FROM items i'
    item_params: tuple = ()
    if job_id:
        item_sql += " WHERE i.announcement_id IN (SELECT announcement_id FROM job_tasks WHERE job_id=? AND status IN ('succeeded','skipped'))"
        item_params = (job_id,)
    for it in storage.rows(item_sql, item_params):
        item_total += 1
        aid = it['announcement_id']
        if aid not in anns:
            unknown += 1
            continue
        # 覆盖率衡量处理链路是否覆盖该来源，不推断某字段唯一来自哪一份材料。
        # 正文和附件可同时参与同一公告抽取，因此两个来源的明细计数允许重叠。
        has_source = False
        if aid in html_used:
            from_html += 1
            html_anns.add(aid)
            has_source = True
        if aid in used_att:
            from_att += 1
            att_anns.add(aid)
            has_source = True
        if not has_source:
            unknown += 1
    announcement_count = len(anns)
    with_attachment = sum(1 for a in anns.values() if a['has_attachment'])
    html_covered, attachment_covered = from_html > 0, from_att > 0
    return {
        'announcements': announcement_count,
        'with_attachment': with_attachment,
        'attachment_used': len(used_att),
        'image_announcements': image_anns,
        'attachment_kinds': dict(kinds.most_common()),
        'items_total': item_total,
        'items_from_html': from_html, 'items_from_attachment': from_att, 'items_unattributed': unknown,
        'html_covered': html_covered, 'attachment_covered': attachment_covered,
        'covered_categories': int(html_covered) + int(attachment_covered),
        'announcements_items_from_html': len(html_anns),
        'announcements_items_from_attachment': len(att_anns),
        'html_announcement_rate': len(html_anns) / announcement_count if announcement_count else None,
        'attachment_announcement_rate': len(att_anns) / with_attachment if with_attachment else None,
    }


_POINTS = {'7': 10, '5_6': 5, '3_4': 3, '0_2': 0}


def _band(count: int) -> str:
    return '7' if count == 7 else '5_6' if count >= 5 else '3_4' if count >= 3 else '0_2'


def extraction_rate(job_id: str | None = None) -> dict:
    fields = {key: {'annotated': 0, 'extracted': 0} for key, _ in FIELDS}
    if job_id:
        id_rows = storage.rows(
            "SELECT announcement_id FROM job_tasks WHERE job_id=? AND status IN ('succeeded','skipped')", (job_id,))
    else:
        id_rows = storage.rows('SELECT announcement_id FROM announcements')
    predictions = evaluation._preds_bulk()
    gold_available = evaluation.gold_available()
    official_bands: Counter = Counter()
    annotated_items = annotated_total = extracted_total = 0
    predicted_items = 0
    null_ok_bands: Counter = Counter()
    null_ok_items = null_ok_slots = 0

    for id_row in id_rows:
        aid = id_row['announcement_id']
        pred = predictions.get(aid)
        if not pred:
            continue

        pred_items = pred['items']
        predicted_items += len(pred_items)
        for pred_item in pred_items:
            present_count = sum(evaluation._present(key, pred_item.get(key)) for key, _ in FIELDS)
            if present_count == 7:
                official_bands['7'] += 1
            elif present_count >= 5:
                official_bands['5_6'] += 1
            elif present_count >= 3:
                official_bands['3_4'] += 1
            else:
                official_bands['0_2'] += 1

        if not gold_available:
            continue
        gold = evaluation.load_gold(aid)
        if not gold:
            continue
        gold_items = gold.get('items') or []
        pairs = evaluation.match_items(pred_items, gold_items)
        pred_by_gold = {gold_index: pred_items[pred_index] for pred_index, gold_index in pairs}

        # 对照答案的分档：答案为空、预测也为空的槽位视为“如实提取为空”，与有值字段一样计入字段数。
        # 未配对上答案的预测明细只计有值字段。
        gold_by_pred = {pred_index: gold_items[gold_index] for pred_index, gold_index in pairs}
        for pred_index, pred_item in enumerate(pred_items):
            gold_item = gold_by_pred.get(pred_index)
            count = 0
            for key, _ in FIELDS:
                if evaluation._present(key, pred_item.get(key)):
                    count += 1
                elif gold_item is not None and not evaluation._present(key, gold_item.get(key)):
                    count += 1
                    null_ok_slots += 1
            null_ok_items += 1
            null_ok_bands[_band(count)] += 1

        # 逐条以人工答案的非空字段作为应抽字段；人工答案为空的槽位不进入提取率分母。
        for gold_index, gold_item in enumerate(gold_items):
            required = [key for key, _ in FIELDS
                        if evaluation._present(key, gold_item.get(key))]
            if not required:
                continue
            annotated_items += 1
            annotated_total += len(required)
            pred_item = pred_by_gold.get(gold_index)
            for key in required:
                fields[key]['annotated'] += 1
                if pred_item and evaluation._present(key, pred_item.get(key)):
                    fields[key]['extracted'] += 1
                    extracted_total += 1

    field_rows = [
        {'key': key, 'label': label, **fields[key],
         'rate': fields[key]['extracted'] / fields[key]['annotated']
         if fields[key]['annotated'] else None}
        for key, label in FIELDS
    ]
    band_rows = [
        {'key': key, 'label': label, 'items': official_bands[key], 'points': points,
         'rate': official_bands[key] / predicted_items if predicted_items else None}
        for key, label, points in (
            ('7', '7/7 个字段', 10),
            ('5_6', '5–6 个字段', 5),
            ('3_4', '3–4 个字段', 3),
            ('0_2', '0–2 个字段', 0),
        )
    ]
    points_by_band = {'7': 10, '5_6': 5, '3_4': 3, '0_2': 0}
    score_points = sum(official_bands[key] * points for key, points in points_by_band.items())
    max_points = predicted_items * 10
    field_annotation_available = gold_available and annotated_total > 0
    null_ok_points = sum(null_ok_bands[k] * p for k, p in _POINTS.items())
    gold_aware = {
        'available': null_ok_items > 0,
        'items': null_ok_items,
        'null_ok_slots': null_ok_slots,
        'score_points': null_ok_points, 'max_points': null_ok_items * 10,
        'rate': null_ok_points / (null_ok_items * 10) if null_ok_items else None,
        'average_points': null_ok_points / null_ok_items if null_ok_items else None,
        'bands': [{'key': k, 'items': null_ok_bands[k], 'points': p,
                   'rate': null_ok_bands[k] / null_ok_items if null_ok_items else None}
                  for k, p in _POINTS.items()],
    }
    return {
        'gold_aware': gold_aware,
        'available': predicted_items > 0, 'field_annotation_available': field_annotation_available,
        'items': predicted_items,
        'predicted_items': predicted_items,
        'annotated_fields': annotated_total, 'extracted_fields': extracted_total,
        # The contest rubric scores each extracted row by how many of the seven
        # key fields are populated.  This is independent of gold-field coverage;
        # value correctness is evaluated separately under extraction accuracy.
        'rate': score_points / max_points if max_points else None,
        'score_points': score_points, 'max_points': max_points,
        'average_points': score_points / predicted_items if predicted_items else None,
        'field_annotation_rate': extracted_total / annotated_total if annotated_total else None,
        'fields': field_rows, 'bands': band_rows,
        'complete_items': official_bands['7'],
        'complete_rate': official_bands['7'] / predicted_items if predicted_items else None,
    }


def _accuracy(job_id: str | None = None) -> dict | None:
    global _eval_cache
    rev = storage.revision()
    with _eval_lock:
        if _eval_cache and _eval_cache[0] == rev and _eval_cache[1] == job_id:
            return _eval_cache[2]
        if not evaluation.gold_available():
            return None
        rep = evaluation.evaluate(job_id)
        _eval_cache = (rev, job_id, rep)
        return rep


def accuracy(refresh: bool = False, job_id: str | None = None) -> dict | None:
    global _eval_cache
    if refresh:
        with _eval_lock:
            _eval_cache = None
    # Keep the no-filter call compatible with callers/tests that patch _accuracy()
    # without accepting an argument.
    rep = _accuracy() if job_id is None else _accuracy(job_id)
    if not rep or not rep['evaluated']:
        return None
    s = rep['summary']
    m = s['fields_micro']
    # 答疑 Q16：按二分类混淆矩阵计算。有正确字段的记 P 样本，无字段/缺失的记 N 样本；
    # A=(TP+TN)/(TP+FP+FN+TN)，P=TP/(TP+FP)，R=TP/(TP+FN)。
    # TP=有字段且正确；FP=有字段但错/漏；TN=无字段且未填；FN=无字段却填了值（见 evaluation.compare_one）。
    # TP/(TP+FP+FN)（不计 TN）另以 accuracy_no_tn 保留，便于对照。
    tp, fp, fn, tn = m['tp'], m['fp'], m['fn'], m.get('tn', 0)
    acc = (tp + tn) / (tp + fp + fn + tn) if tp + fp + fn + tn else None
    acc_no_tn = tp / (tp + fp + fn) if tp + fp + fn else None
    p, r = m['precision'], m['recall']
    score = 0.4 * acc + 0.3 * p + 0.3 * r if None not in (acc, p, r) else None
    field_metrics = []
    for field in rep['fields']:
        field_tn = field.get('tn', 0)
        denominator = field['tp'] + field['fp'] + field['fn'] + field_tn
        field_acc = (field['tp'] + field_tn) / denominator if denominator else None
        field_score = (0.4 * field_acc + 0.3 * field['precision'] + 0.3 * field['recall']
                       if None not in (field_acc, field['precision'], field['recall']) else None)
        field_metrics.append({key: value for key, value in field.items() if key != 'f1'}
                             | {'accuracy': field_acc, 'score': field_score})
    return {
        'evaluated': rep['evaluated'], 'gold_source': rep['gold_source'],
        'accuracy': acc, 'precision': p, 'recall': r, 'score': score,
        'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn, 'accuracy_no_tn': acc_no_tn,
        'fields': field_metrics, 'worst': rep.get('worst', []),
    }


def speed(job_id: str | None = None) -> dict:
    jobs = []
    tot_s = tot_n = 0.0
    job_sql = 'SELECT id, model, created_at FROM jobs'
    job_params: tuple = ()
    if job_id:
        job_sql += ' WHERE id=?'
        job_params = (job_id,)
    job_sql += ' ORDER BY created_at'
    for j in storage.rows(job_sql, job_params):
        t = storage.one("""SELECT COUNT(*) AS n, SUM(status='succeeded') AS ok, SUM(status='failed') AS bad
                           FROM job_tasks WHERE job_id=?""", (j['id'],)) or {}
        last = storage.one('SELECT MAX(extracted_at) AS t FROM announcements WHERE job_id=?', (j['id'],))
        if not last or not last['t'] or not t.get('n'):
            continue
        sec = (datetime.fromisoformat(last['t']) - datetime.fromisoformat(j['created_at'])).total_seconds()
        n = (t.get('ok') or 0) + (t.get('bad') or 0)
        if sec <= 0 or n <= 0:
            continue
        jobs.append({'job_id': j['id'], 'model': j['model'], 'announcements': n, 'succeeded': t.get('ok') or 0,
                     'failed': t.get('bad') or 0, 'wall_seconds': round(sec, 1), 'per_announcement_s': round(sec / n, 2)})
        tot_s += sec
        tot_n += n
    return {'jobs': jobs, 'total_announcements': int(tot_n), 'total_seconds': round(tot_s, 1),
            'per_announcement_s': round(tot_s / tot_n, 2) if tot_n else None}


def failures(job_id: str | None = None) -> dict:
    # 只统计至今仍未成功入库的公告（同一篇在多个任务里失败只算一次）
    failure_filter = ' AND job_id=?' if job_id else ''
    params = (job_id,) if job_id else ()
    rows = storage.rows(f"""SELECT error_code, COUNT(DISTINCT announcement_id) AS n FROM job_tasks
                           WHERE status='failed' AND announcement_id NOT IN (SELECT announcement_id FROM announcements)
                           {failure_filter} GROUP BY error_code ORDER BY n DESC""", params)
    unresolved = storage.one(f"""SELECT COUNT(DISTINCT announcement_id) AS n FROM job_tasks
                           WHERE status='failed' AND announcement_id NOT IN (SELECT announcement_id FROM announcements)
                           {failure_filter}""", params)
    ds = storage.one('SELECT d.inventory_json FROM datasets d JOIN jobs j ON j.dataset_id=d.id WHERE j.id=?',
                     (job_id,)) if job_id else storage.one('SELECT inventory_json FROM datasets ORDER BY created_at DESC LIMIT 1')
    inv = json.loads(ds['inventory_json']) if ds and ds['inventory_json'] else {}
    return {'dataset_total': inv.get('html_count'), 'paired': inv.get('paired'), 'html_only': inv.get('html_only'),
            'unresolved': unresolved['n'] if unresolved else 0, 'by_code': rows}


def warm() -> None:
    """服务启动时在后台调用：提前算好准确率缓存。失败不影响服务。"""
    try:
        _accuracy()
    except Exception:  # noqa: BLE001 — 预热失败时，页面请求会再算一次并给出错误
        pass


def overview(refresh: bool = False, job_id: str | None = None) -> dict:
    t0 = time.perf_counter()
    out = {'coverage': coverage(job_id), 'extraction': extraction_rate(job_id),
           'accuracy': accuracy(refresh, job_id), 'speed': speed(job_id), 'failures': failures(job_id),
           'job_id': job_id}
    if job_id:
        m = storage.one('SELECT model FROM jobs WHERE id=?', (job_id,))
    else:
        m = storage.one('SELECT model, COUNT(*) AS n FROM announcements GROUP BY model ORDER BY n DESC LIMIT 1')
    out['model'] = m['model'] if m else None
    from . import reapply  # 启动时旧记录按当前合并规则自动整理的进度（整理中指标会逐步更新）
    out['merge_migration'] = dict(reapply.progress)
    out['source_revision'] = f'sql:rev{storage.revision()}'
    out['query_ms'] = (time.perf_counter() - t0) * 1000
    return out
