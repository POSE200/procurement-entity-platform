"""Anonymous collaboration, per-task credentials and persistent uploaded files."""
import asyncio
import base64
from contextvars import ContextVar
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from pydantic import SecretStr
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from . import config, storage, cloud_pg

ROOT=Path(__file__).resolve().parents[2]
INSTANCE=secrets.token_hex(16)
CURRENT=ContextVar('visitor_settings',default=None)
DEFAULT=config.Settings(_env_file=None,LLM_PROVIDER='deepseek',DEEPSEEK_MODEL='deepseek-flash',
                        DEEPSEEK_API_KEY=None,QWEN_API_KEY=None,LLM_TIMEOUT_SECONDS=180,LLM_MAX_CONCURRENCY=3)
_cipher=None
_base_jobs=set()
_base_ids=set()


def settings_values(obj):
    return {alias:(getattr(obj,key).get_secret_value() if isinstance(getattr(obj,key),SecretStr) else getattr(obj,key))
            for key,alias in config.EDITABLE.items()}


def validate(values):
    allowed=set(config.EDITABLE.values())
    if not isinstance(values,dict) or set(values)-allowed: raise ValueError('Invalid configuration')
    obj=config.Settings(_env_file=None,**{**settings_values(DEFAULT),**values})
    for provider in ('deepseek','qwen'):
        url=urlparse(obj.base_url_for(provider))
        if url.scheme!='https' or not url.hostname or url.username or url.password:
            raise ValueError('模型服务地址须为公网HTTPS地址')
        addresses=socket.getaddrinfo(url.hostname,url.port or 443,type=socket.SOCK_STREAM)
        if any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
            raise ValueError('模型服务地址须为公网HTTPS地址')
    return obj


class SettingsProxy:
    def __getattr__(self,name): return getattr(CURRENT.get() or DEFAULT,name)


def update_settings(changes):
    current=CURRENT.get()
    if current is None: raise ValueError('请在当前浏览器配置API Key')
    values=settings_values(current)
    values.update({config.EDITABLE[k]:v for k,v in changes.items()})
    validated=validate(values)
    for key in config.EDITABLE: setattr(current,key,getattr(validated,key))


class VisitorMiddleware(BaseHTTPMiddleware):
    async def dispatch(self,request,call_next):
        if request.method=='DELETE' and request.url.path=='/api/v1/records':
            return JSONResponse({'detail':'公开协作平台保留共享结果与缓存'},status_code=403)
        if request.url.path=='/api/v1/datasets/local' and request.method=='POST':
            return JSONResponse({'detail':'请使用浏览器上传文件'},status_code=400)
        try:
            encoded=request.headers.get('x-ict-config','')
            values=json.loads(base64.b64decode(encoded,validate=True)) if encoded else {}
            obj=await asyncio.to_thread(validate,values) if encoded else DEFAULT.model_copy(deep=True)
        except Exception:
            return JSONResponse({'detail':'当前浏览器的模型配置无效，请重新配置'},status_code=400)
        token=CURRENT.set(obj)
        try: return await call_next(request)
        finally: CURRENT.reset(token)


def bind_task_keys(job_id,ids):
    current=CURRENT.get()
    if current is None or not current.has_api_key(): raise ValueError('请先配置自己的API Key')
    encrypted=_cipher.encrypt(json.dumps(settings_values(current)).encode()).decode()
    with storage.db() as db:
        for aid in ids:
            db.execute('DELETE FROM cloud_task_keys WHERE job_id=? AND announcement_id=?',(job_id,aid))
            db.execute('INSERT INTO cloud_task_keys(job_id,announcement_id,ciphertext) VALUES(?,?,?)',(job_id,aid,encrypted))


def claim_tasks(limit):
    with storage.db() as db:
        suffix=' FOR UPDATE OF t SKIP LOCKED' if cloud_pg.URL else ''
        rows=db.execute('''SELECT t.job_id,t.announcement_id FROM job_tasks t
            JOIN jobs j ON j.id=t.job_id JOIN cloud_task_keys k
            ON k.job_id=t.job_id AND k.announcement_id=t.announcement_id
            WHERE t.status='queued' AND COALESCE(j.status,'')!='paused'
            ORDER BY j.created_at LIMIT ?'''+suffix,(limit,)).fetchall()
        for row in rows:
            db.execute("UPDATE job_tasks SET status='running' WHERE job_id=? AND announcement_id=?",(row['job_id'],row['announcement_id']))
            db.execute('UPDATE cloud_task_keys SET worker_id=?,lease_until=? WHERE job_id=? AND announcement_id=?',
                       (INSTANCE,time.time()+120,row['job_id'],row['announcement_id']))
        return [dict(r) for r in rows]


def heartbeat():
    while True:
        try:
            with storage.db() as db:
                db.execute('''UPDATE cloud_task_keys SET lease_until=? WHERE worker_id=? AND EXISTS
                    (SELECT 1 FROM job_tasks t WHERE t.job_id=cloud_task_keys.job_id
                     AND t.announcement_id=cloud_task_keys.announcement_id AND t.status='running')''',
                           (time.time()+120,INSTANCE))
                expired=db.execute('''SELECT t.job_id,t.announcement_id FROM job_tasks t JOIN cloud_task_keys k
                    ON k.job_id=t.job_id AND k.announcement_id=t.announcement_id
                    WHERE t.status='running' AND k.lease_until<?''',(time.time(),)).fetchall()
                for row in expired:
                    db.execute("UPDATE job_tasks SET status='failed',retryable=1,error_code='process_interrupted',message='云端运行进程中断，可使用自己的Key重试' WHERE job_id=? AND announcement_id=?",
                               (row['job_id'],row['announcement_id']))
        except Exception: pass
        time.sleep(20)


def materialize(dataset):
    d=dict(dataset)
    if d.get('id')=='ds_20261014_43c21f':
        d.update(html_zip=str(ROOT/'seed/official_html.zip'),attachment_zip=None)
        return d
    for field in ('html_zip','attachment_zip'):
        if not d.get(field): continue
        old=Path(d[field])
        if old.is_file(): continue
        parts=old.as_posix().split('/datasets/')
        relative=parts[-1] if len(parts)>1 else d['id']+'/'+old.name
        destination=storage.DATA_DIR/'datasets'/relative
        row=storage.one('SELECT payload FROM cloud_uploads WHERE path=?',(relative,))
        if not row: raise FileNotFoundError('未找到该数据集已保存的上传文件')
        destination.parent.mkdir(parents=True,exist_ok=True)
        temporary=destination.with_suffix(destination.suffix+'.'+secrets.token_hex(4)+'.tmp')
        temporary.write_bytes(bytes(row['payload'])); temporary.replace(destination)
        d[field]=str(destination)
    return d


def install():
    global _cipher,_base_jobs,_base_ids
    cloud_pg.install()
    storage.init_db()
    secret=os.environ.get('DATABASE_URL')
    if secret:
        cipher_key=base64.urlsafe_b64encode(hashlib.sha256(('ict-cloud:'+secret).encode()).digest())
    else:
        path=storage.DATA_DIR/'cloud.key'
        if not path.exists(): path.write_bytes(Fernet.generate_key())
        cipher_key=path.read_bytes()
    _cipher=Fernet(cipher_key)
    with storage.db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS cloud_task_keys(job_id TEXT,announcement_id TEXT,
            ciphertext TEXT NOT NULL,worker_id TEXT,lease_until DOUBLE PRECISION,
            PRIMARY KEY(job_id,announcement_id))''')
        db.execute('CREATE TABLE IF NOT EXISTS cloud_uploads(path TEXT PRIMARY KEY,payload BYTEA NOT NULL)')
    import sqlite3
    seed=sqlite3.connect(storage.DB_PATH)
    _base_jobs={r[0] for r in seed.execute('SELECT id FROM jobs')}
    _base_ids={r[0] for r in seed.execute('SELECT announcement_id FROM announcements')}
    seed.close()
    config.settings=SettingsProxy()
    config.update_settings=update_settings


def connect_app(app):
    from . import main,worker,ingestion,reapply
    app.add_middleware(VisitorMiddleware)
    original_create=worker.create_job
    def create_job(*args,**kwargs):
        jid=original_create(*args,**kwargs)
        ids=[r['announcement_id'] for r in storage.rows('SELECT announcement_id FROM job_tasks WHERE job_id=?',(jid,))]
        bind_task_keys(jid,ids); worker._wake.set()
        return jid
    worker.create_job=create_job
    original_retry=worker.retry
    def retry(job_id,ids,*args,**kwargs):
        if job_id in _base_jobs: raise ValueError('内置历史结果已保留；新增处理请上传文件并建立任务')
        bind_task_keys(job_id,ids)
        return original_retry(job_id,ids,*args,**kwargs)
    worker.retry=retry
    original_run=worker._run_task
    async def run(job,dataset,aid):
        token=None
        try:
            row=storage.one('SELECT ciphertext FROM cloud_task_keys WHERE job_id=? AND announcement_id=?',(job['id'],aid))
            values=json.loads(_cipher.decrypt(row['ciphertext'].encode()))
            token=CURRENT.set(config.Settings(_env_file=None,**values))
            await original_run(job,materialize(dataset),aid)
        except Exception:
            worker._set_task(job['id'],aid,status='failed',retryable=1,failed_stage='parse',
                             error_code='cloud_input_unavailable',message='云端任务输入暂不可用，请重新上传后处理')
        finally:
            if token is not None: CURRENT.reset(token)
    worker._run_task=run
    def start():
        if worker._started: return
        worker._started=True
        threading.Thread(target=lambda:asyncio.run(worker._loop()),daemon=True).start()
        threading.Thread(target=heartbeat,daemon=True).start()
    worker.start=start
    reapply.migrate=lambda:None
    original_register=ingestion.register
    def register(dataset_id,source,html_zip,attachment_zip):
        result=original_register(dataset_id,source,html_zip,attachment_zip)
        with storage.db() as db:
            for path in (html_zip,attachment_zip):
                if path is None: continue
                path=Path(path)
                key=path.relative_to(storage.DATA_DIR/'datasets').as_posix()
                db.execute('INSERT INTO cloud_uploads(path,payload) VALUES(?,?)',(key,path.read_bytes()))
        return result
    ingestion.register=register
    original_clear=storage.clear_job_record
    def clear_job_record(job_id):
        result=original_clear(job_id)
        with storage.db() as db: db.execute('DELETE FROM cloud_task_keys WHERE job_id=?',(job_id,))
        return result
    storage.clear_job_record=clear_job_record
    original_read=ingestion.read_html
    ingestion.read_html=lambda d,aid:original_read(materialize(d),aid)
    original_attachment=ingestion.read_attachment
    ingestion.read_attachment=lambda d,aid:original_attachment(materialize(d),aid)
