"""数据集登记与校验清单。

两种来源：
1. 网页上传 ZIP：流式写入 backend/data/datasets/<id>/，不在内存中整包读取；
2. 本机路径导入：直接引用磁盘上的 ZIP（适合 3.5 GB 附件包，免去浏览器上传）。
"""
from __future__ import annotations

import json
import secrets
import zipfile
from collections import Counter
from pathlib import Path

from . import storage
from .parsers import announcement_id_of

DATASETS_DIR = storage.DATA_DIR / 'datasets'


def new_id(prefix: str) -> str:
    return f'{prefix}_{storage.now()[:10].replace("-", "")}_{secrets.token_hex(3)}'


def _ids_in(zip_path: Path, ext: str) -> tuple[Counter, list[str]]:
    counts: Counter = Counter()
    invalid: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if not name.lower().endswith(ext):
                continue
            aid = announcement_id_of(name)
            if aid:
                counts[aid] += 1
            else:
                invalid.append(name)
    return counts, invalid


def build_inventory(dataset_id: str, html_zip: Path, attachment_zip: Path | None) -> dict:
    try:
        html_ids, html_invalid = _ids_in(html_zip, '.html')
    except zipfile.BadZipFile as exc:
        raise ValueError('公告 HTML 包不是有效的 ZIP 文件') from exc
    att_ids: Counter = Counter()
    att_invalid: list[str] = []
    if attachment_zip:
        try:
            att_ids, att_invalid = _ids_in(attachment_zip, '.zip')
        except zipfile.BadZipFile as exc:
            raise ValueError('附件包不是有效的 ZIP 文件') from exc
    if not html_ids:
        raise ValueError('公告包中没有找到形如 t20260202_26139731.html 的公告文件')

    entries = []
    for aid in sorted(set(html_ids) | set(att_ids)):
        h, a = html_ids.get(aid, 0), att_ids.get(aid, 0)
        if h > 1 or a > 1:
            status, msg = 'duplicate', f'同一公告 ID 出现多次（HTML {h} 个，附件 {a} 个），已阻止导入'
        elif h and a:
            status, msg = 'paired', None
        elif h:
            status, msg = 'html_only', '未提供附件，仅处理公告正文' if attachment_zip else '未上传附件包'
        else:
            status, msg = 'attachment_only', '只有附件没有公告，不进入抽取'
        entries.append({'announcement_id': aid, 'html_present': h > 0, 'attachment_present': a > 0,
                        'status': status, 'message': msg})
    for name in html_invalid + att_invalid:
        entries.append({'announcement_id': name, 'html_present': False, 'attachment_present': False,
                        'status': 'invalid', 'message': '文件名中没有可识别的公告 ID'})
    st = Counter(e['status'] for e in entries)
    return {
        'dataset_id': dataset_id, 'created_at': storage.now(),
        'html_count': sum(html_ids.values()), 'attachment_count': sum(att_ids.values()),
        'paired': st['paired'], 'html_only': st['html_only'], 'attachment_only': st['attachment_only'],
        'conflicts': st['duplicate'] + st['invalid'], 'entries': entries,
    }


def register(dataset_id: str, source: str, html_zip: Path, attachment_zip: Path | None) -> dict:
    inv = build_inventory(dataset_id, html_zip, attachment_zip)
    with storage.db() as c:
        c.execute('INSERT INTO datasets(id, created_at, source, html_zip, attachment_zip, inventory_json) VALUES(?,?,?,?,?,?)',
                  (dataset_id, inv['created_at'], source, str(html_zip), str(attachment_zip) if attachment_zip else None,
                   json.dumps(inv, ensure_ascii=False)))
    return inv


def _single_html_zip(dataset_id: str, html_path: Path) -> tuple[str, Path]:
    """把单篇 HTML 整理成标准的外层公告 ZIP。"""
    aid = announcement_id_of(html_path.name)
    if not aid:
        raise ValueError('单篇 HTML 文件名必须包含公告 ID，例如 t20260202_26139731.html')
    if html_path.suffix.lower() not in ('.html', '.htm'):
        raise ValueError(f'单篇公告必须是 .html 或 .htm 文件：{html_path.name}')
    folder = DATASETS_DIR / dataset_id
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / 'html.zip'
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(html_path, arcname=html_path.name)
    return aid, out


def _single_attachment_zip(dataset_id: str, aid: str, attachment_path: Path) -> Path:
    """把单篇附件 ZIP 包装成标准的 <公告ID>.zip 外层附件包。

    如果传入的 ZIP 已经包含同名 <公告ID>.zip，则直接复用；否则把它视为
    该公告自己的附件包，放进一个新的外层 ZIP 中。
    """
    if attachment_path.suffix.lower() != '.zip':
        raise ValueError(f'单篇附件必须是 .zip 文件：{attachment_path.name}')
    try:
        with zipfile.ZipFile(attachment_path) as zf:
            nested = [i.filename for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith('.zip')]
    except zipfile.BadZipFile as exc:
        raise ValueError('单篇附件不是有效的 ZIP 文件') from exc
    if any(announcement_id_of(name) == aid for name in nested):
        return attachment_path
    if len(nested) > 1:
        raise ValueError(f'单篇附件 ZIP 中包含多个内部 ZIP，且没有找到 {aid}.zip')
    folder = DATASETS_DIR / dataset_id
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / 'attachments.zip'
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(attachment_path, arcname=f'{aid}.zip')
    return out


def register_paths(dataset_id: str, source: str, html_path: Path, attachment_path: Path | None,
                   kind: str = 'batch') -> dict:
    """登记批量 ZIP，或将单篇 HTML + 附件 ZIP 归一化后登记。"""
    if kind == 'batch':
        if html_path.suffix.lower() != '.zip':
            raise ValueError(f'批量公告包必须是 .zip 文件：{html_path}')
        if attachment_path is not None and attachment_path.suffix.lower() != '.zip':
            raise ValueError(f'批量附件包必须是 .zip 文件：{attachment_path}')
        return register(dataset_id, source, html_path, attachment_path)
    if kind != 'single':
        raise ValueError('输入类型只能是 batch 或 single')
    aid, html_zip = _single_html_zip(dataset_id, html_path)
    att_zip = _single_attachment_zip(dataset_id, aid, attachment_path) if attachment_path else None
    return register(dataset_id, source, html_zip, att_zip)


def get_dataset(dataset_id: str) -> dict | None:
    return storage.one('SELECT * FROM datasets WHERE id=?', (dataset_id,))


def inventory(dataset_id: str) -> dict | None:
    d = get_dataset(dataset_id)
    if not d:
        return None
    inv = json.loads(d['inventory_json'])
    # inventory_json 保存的是导入时的静态配对结果。任务开始后，页面还需要
    # 知道每个公告是否排队、处理中、已完成或失败，因此把最近一次任务状态
    # 动态叠加到清单上，避免修改原始导入记录。
    task_rows = storage.rows(
        """SELECT t.announcement_id, t.job_id, t.status, t.message, t.failed_stage,
                  j.created_at
             FROM job_tasks t JOIN jobs j ON j.id=t.job_id
            WHERE j.dataset_id=?
            ORDER BY j.created_at DESC, t.job_id DESC""",
        (dataset_id,),
    )
    latest: dict[str, dict] = {}
    for row in task_rows:
        # 同一公告可能经历重试，只采用最近创建的任务。
        latest.setdefault(row['announcement_id'], row)
    stored = {r['announcement_id']: r for r in storage.rows(
        'SELECT announcement_id,job_id FROM announcements WHERE dataset_id=?', (dataset_id,))}
    for entry in inv.get('entries', []):
        task = latest.get(entry.get('announcement_id'))
        if not task:
            # 兼容已清除历史任务的数据集，只认本数据集实际入库的记录。
            record = stored.get(entry.get('announcement_id'))
            if record and not entry.get('processing_status'):
                entry['processing_status'] = 'succeeded'
                entry['processing_job_id'] = record['job_id']
            continue
        entry['processing_status'] = task['status']
        entry['processing_job_id'] = task['job_id']
        entry['processing_message'] = task['message']
        entry['processing_stage'] = task['failed_stage']
    return inv


def processable_ids(dataset_id: str) -> list[str]:
    inv = inventory(dataset_id) or {'entries': []}
    # 已排队、正在处理和已成功的公告不能再次被“开始处理”重复加入任务。
    # 失败任务保留在清单里，允许用户从一页重新发起处理。
    return [
        e['announcement_id']
        for e in inv['entries']
        if e['status'] in ('paired', 'html_only')
        and e.get('processing_status') not in ('queued', 'running', 'succeeded', 'skipped')
    ]


def read_html(dataset: dict, aid: str) -> bytes:
    with zipfile.ZipFile(dataset['html_zip']) as zf:
        for info in zf.infolist():
            if not info.is_dir() and info.filename.lower().endswith('.html') and announcement_id_of(info.filename) == aid:
                return zf.read(info)
    raise FileNotFoundError(aid)


def read_attachment(dataset: dict, aid: str) -> bytes | None:
    if not dataset['attachment_zip']:
        return None
    with zipfile.ZipFile(dataset['attachment_zip']) as zf:
        for info in zf.infolist():
            if not info.is_dir() and info.filename.lower().endswith('.zip') and announcement_id_of(info.filename) == aid:
                return zf.read(info)
    return None


# 本机基准数据的默认位置（ICT 文件夹下）。项目可能被多套一层文件夹，向上查找而不是写死层数。
def _find_ict_root() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / '赛题五.基准测试数据_html.zip').is_file():
            return p
    return here.parents[3]


ICT_ROOT = _find_ict_root()
DEFAULT_HTML = ICT_ROOT / '赛题五.基准测试数据_html.zip'
DEFAULT_ATTACH = ICT_ROOT / '赛题五.基准测试数据_file.zip'


def local_defaults() -> dict:
    return {'html_path': str(DEFAULT_HTML) if DEFAULT_HTML.is_file() else None,
            'attachment_path': str(DEFAULT_ATTACH) if DEFAULT_ATTACH.is_file() else None}
