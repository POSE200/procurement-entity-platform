"""Cloud database adapter; preserves the existing extraction and scoring queries."""
from pathlib import Path
import os
import re
import sqlite3
import threading

from . import storage

URL = os.environ.get('DATABASE_URL','')
_ready = False
_init_lock = threading.Lock()


class Connection:
    def __init__(self):
        import psycopg
        from psycopg.rows import dict_row
        self.raw = psycopg.connect(URL, row_factory=dict_row, connect_timeout=20)

    def translate(self, sql):
        sql = re.sub(r'SUM\((status=\'[^\']+\')\)',r'SUM((\1)::integer)',sql,flags=re.I)
        if re.match(r'\s*INSERT OR REPLACE',sql,re.I):
            sql = re.sub(r'INSERT OR REPLACE','INSERT',sql,count=1,flags=re.I)
            match = re.search(r'INSERT INTO\s+(\w+)\s*\(([^)]+)\)',sql,re.I)
            table, columns = match.group(1), [x.strip() for x in match.group(2).split(',')]
            key = {'announcements':'announcement_id','llm_cache':'key'}[table]
            sql += ' ON CONFLICT ('+key+') DO UPDATE SET '+','.join(c+'=EXCLUDED.'+c for c in columns if c!=key)
        return sql.replace('?', '%s')

    def execute(self, sql, params=()):
        return self.raw.execute(self.translate(sql),params)

    def executemany(self, sql, params):
        cursor = self.raw.cursor()
        cursor.executemany(self.translate(sql),params)
        return cursor

    def commit(self): self.raw.commit()
    def rollback(self): self.raw.rollback()
    def close(self): self.raw.close()


def init_db():
    global _ready
    with _init_lock:
        if _ready: return
        conn=Connection()
        try:
            conn.raw.execute('SELECT pg_advisory_xact_lock(103820261015)')
            schema=re.sub(r'\bREAL\b','DOUBLE PRECISION',storage.SCHEMA)
            schema=re.sub(r'(CREATE TABLE IF NOT EXISTS \w+\s*\()',r'\1rowid BIGSERIAL UNIQUE,',schema)
            for statement in schema.split(';'):
                if statement.strip(): conn.raw.execute(statement)
            conn.raw.execute('''CREATE OR REPLACE FUNCTION json_extract(document TEXT, path TEXT)
                RETURNS TEXT LANGUAGE SQL IMMUTABLE AS $$
                SELECT CASE jsonb_typeof(v) WHEN 'string' THEN v #>> '{}' ELSE v::text END
                FROM (SELECT document::jsonb #> string_to_array(substr(path,3),'.') AS v) q $$''')
            conn.raw.execute('CREATE TABLE IF NOT EXISTS cloud_state(id INTEGER PRIMARY KEY, revision BIGINT NOT NULL DEFAULT 0, seeded INTEGER NOT NULL DEFAULT 0)')
            conn.raw.execute('INSERT INTO cloud_state(id) VALUES(1) ON CONFLICT DO NOTHING')
            seeded=conn.raw.execute('SELECT seeded FROM cloud_state WHERE id=1').fetchone()['seeded']
            if not seeded:
                seed=sqlite3.connect(storage.DB_PATH)
                for table in ('datasets','jobs','job_tasks','announcements','items','llm_cache','evaluations'):
                    columns=[r[1] for r in seed.execute('PRAGMA table_info('+table+')')]
                    rows=seed.execute('SELECT * FROM '+table).fetchall()
                    sql='INSERT INTO '+table+'('+','.join(columns)+') VALUES('+','.join(['%s']*len(columns))+') ON CONFLICT DO NOTHING'
                    conn.raw.cursor().executemany(sql,rows)
                seed.close()
                conn.raw.execute('UPDATE cloud_state SET seeded=1 WHERE id=1')
            conn.commit()
            _ready=True
        except Exception:
            conn.rollback()
            raise
        finally: conn.close()


def install():
    if not URL: return False
    storage._connect=Connection
    storage.init_db=init_db
    def revision():
        row=storage.one('SELECT revision FROM cloud_state WHERE id=1')
        return row['revision'] if row else 0
    def bump():
        with storage.db() as db: db.execute('UPDATE cloud_state SET revision=revision+1 WHERE id=1')
    storage.revision=revision
    storage.bump_revision=bump
    return True
