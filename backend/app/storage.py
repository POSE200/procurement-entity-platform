"""SQLite 事实库（标准库 sqlite3，无需额外安装数据库）。

SQL 是唯一事实源；关系查询直接基于这里的数据计算（SQL 关系投影，未接 Neo4j）。
数据文件位于 backend/data/app.db，上传的原始 ZIP 保存在 backend/data/datasets/<id>/。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import os

# 可用 ICT_DATA_DIR 覆盖（测试时指向临时目录，避免污染真实数据）
DATA_DIR = Path(os.environ.get('ICT_DATA_DIR') or Path(__file__).resolve().parents[1] / 'data')
DB_PATH = DATA_DIR / 'app.db'

_lock = threading.RLock()
_revision = 0  # 事实数据变更计数，用于查询缓存失效

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets(
  id TEXT PRIMARY KEY, created_at TEXT, source TEXT,
  html_zip TEXT NOT NULL, attachment_zip TEXT, inventory_json TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, dataset_id TEXT, status TEXT, provider TEXT, model TEXT,
  created_at TEXT, updated_at TEXT, stage_ms_json TEXT DEFAULT '{}',
  bypass_cache INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS job_tasks(
  job_id TEXT, announcement_id TEXT, status TEXT, progress INTEGER DEFAULT 0,
  failed_stage TEXT, error_code TEXT, message TEXT, retryable INTEGER DEFAULT 1,
  needs_review INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
  PRIMARY KEY(job_id, announcement_id));
CREATE TABLE IF NOT EXISTS announcements(
  announcement_id TEXT PRIMARY KEY, dataset_id TEXT, job_id TEXT, title TEXT,
  publish_time TEXT, project_no TEXT, buyer TEXT, project_name TEXT, total_amount REAL,
  packages_json TEXT, bidders_json TEXT, has_attachment INTEGER, summary_text TEXT,
  body_text TEXT, review_note TEXT, model TEXT, extracted_at TEXT, needs_review INTEGER,
  sources_json TEXT);
CREATE TABLE IF NOT EXISTS items(
  item_id TEXT PRIMARY KEY, announcement_id TEXT, package_no TEXT, item_name TEXT,
  category TEXT, brand TEXT, model TEXT, unit_price REAL, quantity REAL, unit TEXT,
  total_price REAL, supplier TEXT);
CREATE TABLE IF NOT EXISTS llm_cache(
  key TEXT PRIMARY KEY, content TEXT, model TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS evaluations(
  id TEXT PRIMARY KEY, created_at TEXT, job_id TEXT, model TEXT, report_json TEXT);
CREATE INDEX IF NOT EXISTS idx_items_ann ON items(announcement_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON job_tasks(job_id, status);
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """串行化写入；每次操作独立连接，线程安全。"""
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    with db() as c:
        c.executescript(SCHEMA)
        # Existing local databases predate per-job cache bypass. Keep the migration additive.
        columns = {row['name'] for row in c.execute('PRAGMA table_info(jobs)').fetchall()}
        if 'bypass_cache' not in columns:
            c.execute('ALTER TABLE jobs ADD COLUMN bypass_cache INTEGER NOT NULL DEFAULT 0')


def clear_task_records() -> dict[str, int]:
    """清除任务与抽取结果，保留已登记的数据集及其原始文件。"""
    with db() as c:
        active = c.execute("SELECT COUNT(*) FROM job_tasks WHERE status IN ('queued', 'running')").fetchone()[0]
        if active:
            raise ValueError('当前仍有任务排队或运行，请等待任务结束后再清除记录')
        counts: dict[str, int] = {}
        # 数据集保留，清理任务、结果、评测和模型缓存；ZIP/附件文件不会被删除。
        for table in ('evaluations', 'items', 'announcements', 'job_tasks', 'jobs', 'llm_cache'):
            cur = c.execute(f'DELETE FROM {table}')
            counts[table] = max(0, cur.rowcount)
    bump_revision()
    return counts


def clear_job_record(job_id: str) -> dict:
    """清除指定任务历史，保留公告结果、原始文件和可复用模型缓存。"""
    with db() as c:
        job = c.execute('SELECT id,dataset_id FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job:
            raise LookupError('任务不存在')
        active = c.execute("SELECT COUNT(*) AS n FROM job_tasks WHERE job_id=? AND status IN ('queued','running')", (job_id,)).fetchone()['n']
        if active:
            raise ValueError('该任务仍在排队或运行，请在处理结束后清除')
        dataset = c.execute('SELECT inventory_json FROM datasets WHERE id=?', (job['dataset_id'],)).fetchone()
        if dataset:
            inventory = json.loads(dataset['inventory_json'])
            latest = {}
            for task in c.execute('''SELECT t.* FROM job_tasks t JOIN jobs j ON j.id=t.job_id
                WHERE j.dataset_id=? ORDER BY j.created_at DESC,t.job_id DESC''', (job['dataset_id'],)).fetchall():
                latest.setdefault(task['announcement_id'], task)
            for entry in inventory.get('entries', []):
                task = latest.get(entry.get('announcement_id'))
                if task and task['status'] not in ('queued', 'running'):
                    entry.update(processing_status=task['status'], processing_job_id=task['job_id'],
                                 processing_message=task['message'], processing_stage=task['failed_stage'])
            c.execute('UPDATE datasets SET inventory_json=? WHERE id=?',
                      (json.dumps(inventory, ensure_ascii=False), job['dataset_id']))
        c.execute('DELETE FROM evaluations WHERE job_id=?', (job_id,))
        c.execute('DELETE FROM job_tasks WHERE job_id=?', (job_id,))
        c.execute('DELETE FROM jobs WHERE id=?', (job_id,))
    bump_revision()
    return {'job_id': job_id, 'results_preserved': True, 'cache_preserved': True,
            'source_files_preserved': True}


def revision() -> int:
    return _revision


def bump_revision() -> None:
    global _revision
    _revision += 1


def rows(sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    with db() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def one(sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    r = rows(sql, params)
    return r[0] if r else None


def loads(s: str | None, default: Any) -> Any:
    return json.loads(s) if s else default
