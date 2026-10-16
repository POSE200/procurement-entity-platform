import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ingestion, queries, scenarios, storage, worker
from .config import settings, update_settings
from .llm import create_provider
from .llm.openai_compatible import ModelOutputError
from . import evaluation
from . import profiles
from . import task2


@asynccontextmanager
async def lifespan(_: FastAPI):
    storage.init_db()
    worker.start()
    # 后台预热任务一准确率评测缓存（千篇约十余秒），避免首次打开总览页时等待
    import threading
    from . import task1
    threading.Thread(target=task1.warm, name='task1-warm', daemon=True).start()
    # 旧规则入库的公告自动按当前合并规则整理（复用存档的两路首轮结果，不重新抽取）；新抽取的入库即为当前规则
    from . import reapply
    threading.Thread(target=reapply.migrate, name='merge-migrate', daemon=True).start()
    yield


app = FastAPI(title="ICT Procurement Harness", version="0.2.0", lifespan=lifespan)

# 前端构建产物（frontend/dist）。存在时由后端同源托管，只需启动一个服务即可访问完整界面。
FRONTEND_DIST = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'


@app.middleware('http')
async def page_cache(request: Request, call_next):
    response = await call_next(request)
    # 入口页不能被浏览器缓存，否则重新构建前端后窗口仍显示旧界面；assets 带哈希文件名，可正常缓存
    if request.url.path in ('/', '/index.html'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


@app.get('/', include_in_schema=False)
async def index():
    dist_index = FRONTEND_DIST / 'index.html'
    if dist_index.is_file():
        return FileResponse(dist_index)
    return FileResponse(Path(__file__).parent / 'index.html')


@app.get('/healthz')
async def healthz():
    return {'status': 'ok'}


@app.get('/instance', include_in_schema=False)
async def instance():
    """供启动器判断端口上运行的是不是本目录的这份程序。"""
    return {'root': str(Path(__file__).resolve().parents[2])}


@app.get('/readyz')
async def readyz():
    configured = settings.has_api_key() and bool(settings.model_for().strip())
    return JSONResponse(status_code=200 if configured else 503, content={
        'status': 'ready' if configured else 'not_ready',
        'scope': 'sqlite_pipeline',
        'provider_configured': configured,
        'parser_initialized': True,
        'neo4j': {'configured': False, 'reason': 'SQLite（关系查询直接由 SQL 计算）'},
    })


# ---------------- 模型配置 ----------------
def provider_info() -> dict:
    return {'provider': settings.provider_name(), 'model': settings.model_for(),
            'base_url': settings.base_url_for(), 'allowed': True, 'key_configured': settings.has_api_key()}


@app.get('/api/v1/llm/provider')
async def llm_provider():
    return provider_info()


def _mask(secret) -> str | None:
    if not secret:
        return None
    v = secret.get_secret_value()
    return ('*' * 6 + v[-4:]) if len(v) > 8 else '已配置'


@app.get('/api/v1/llm/config')
async def llm_config():
    """两个 provider 的完整配置（密钥只返回掩码）。"""
    return {
        'provider': settings.provider_name(),
        'max_concurrency': settings.llm_max_concurrency,
        'timeout_seconds': settings.llm_timeout_seconds,
        'providers': {
            'qwen': {'base_url': settings.qwen_base_url, 'model': settings.qwen_model,
                     'key_configured': settings.has_api_key('qwen'), 'key_hint': _mask(settings.qwen_api_key)},
            'deepseek': {'base_url': settings.deepseek_base_url, 'model': settings.deepseek_model,
                         'key_configured': settings.has_api_key('deepseek'), 'key_hint': _mask(settings.deepseek_api_key)},
        },
    }


class ConfigUpdate(BaseModel):
    provider: Literal['qwen', 'deepseek']
    base_url: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=500, description='留空表示不修改')
    make_default: bool = True
    max_concurrency: int | None = Field(default=None, ge=1, le=64)
    timeout_seconds: float | None = Field(default=None, ge=5, le=600)


@app.put('/api/v1/llm/config')
async def put_llm_config(body: ConfigUpdate):
    p = body.provider
    changes: dict[str, object] = {}
    if body.base_url is not None:
        url = body.base_url.strip()
        if not url.startswith(('https://', 'http://')):
            raise HTTPException(400, 'Base URL 必须以 http:// 或 https:// 开头')
        changes[f'{p}_base_url'] = url.rstrip('/')
    if body.model is not None:
        changes[f'{p}_model'] = body.model.strip()
    if body.api_key:
        changes[f'{p}_api_key'] = body.api_key.strip()
    if body.make_default:
        changes['llm_provider'] = p
    if body.max_concurrency is not None:
        changes['llm_max_concurrency'] = body.max_concurrency
    if body.timeout_seconds is not None:
        changes['llm_timeout_seconds'] = body.timeout_seconds
    try:
        update_settings(changes)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return await llm_config()


class ModelsProbe(BaseModel):
    provider: Literal['qwen', 'deepseek']
    base_url: str | None = Field(default=None, max_length=300)
    api_key: str | None = Field(default=None, max_length=500)


@app.post('/api/v1/llm/models')
async def llm_models(body: ModelsProbe):
    """用表单里的 Base URL / Key（未填则用已保存的）查询服务商可用模型。
    已保存的 Key 只发往已保存的 Base URL，避免把密钥发给临时输入的其他地址。"""
    from .config import Settings
    p = body.provider
    saved_url = settings.base_url_for(p).rstrip('/')
    url = (body.base_url or saved_url).strip().rstrip('/')
    if not url.startswith(('https://', 'http://')):
        raise HTTPException(400, 'Base URL 必须以 http:// 或 https:// 开头')
    from .cloud_runtime import validate
    try: validate({f'{p.upper()}_BASE_URL': url})
    except Exception as exc: raise HTTPException(400, '模型服务地址须为公网 HTTPS 地址') from exc
    key = (body.api_key or '').strip()
    if not key:
        if url != saved_url:
            raise HTTPException(400, '更换了 Base URL，请同时填写该地址对应的 API Key')
        if not settings.has_api_key(p):
            raise HTTPException(400, '请先填写 API Key')
        secret = settings.qwen_api_key if p == 'qwen' else settings.deepseek_api_key
        key = secret.get_secret_value()
    probe = Settings(_env_file=None, LLM_PROVIDER=p, **{f'{p.upper()}_BASE_URL': url, f'{p.upper()}_API_KEY': key})
    try:
        return {'provider': p, 'models': await create_provider(probe, p).list_models()}
    except ModelOutputError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f'无法连接模型服务（{type(exc).__name__}）') from exc


@app.post('/api/v1/llm/health')
async def llm_health(provider: str | None = Query(default=None)):
    try:
        selected = settings.assert_allowed_provider(provider)
        return {'provider': selected, **await create_provider(settings, selected).health()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------- 数据集与任务 ----------------
def _save_upload(f: UploadFile, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open('wb') as out:
        shutil.copyfileobj(f.file, out, length=4 * 1024 * 1024)
    return dest


@app.post('/api/v1/datasets')
def upload_dataset(html_zip: UploadFile | None = File(default=None), attachment_zip: UploadFile | None = File(default=None),
                   html_file: UploadFile | None = File(default=None), attachment_file: UploadFile | None = File(default=None)):
    """接收批量 ZIP，或接收单篇 HTML + 对应附件 ZIP。"""
    dataset_id = ingestion.new_id('ds')
    folder = ingestion.DATASETS_DIR / dataset_id
    if html_file and html_zip:
        raise HTTPException(400, '请在批量 ZIP 和单篇 HTML 中选择一种输入类型')
    if html_file:
        html_name = Path(html_file.filename or 'announcement.html').name
        html_path = _save_upload(html_file, folder / html_name)
        att_upload = attachment_file or attachment_zip
        att_name = Path(att_upload.filename or 'attachment.zip').name if att_upload and att_upload.filename else 'attachment.zip'
        att_path = _save_upload(att_upload, folder / att_name) if att_upload and att_upload.filename else None
        try:
            return ingestion.register_paths(dataset_id, 'upload', html_path, att_path, 'single')
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if not html_zip or not html_zip.filename:
        raise HTTPException(400, '请提供公告 HTML ZIP，或改用单篇 HTML 输入')
    html_path = _save_upload(html_zip, folder / 'html.zip')
    att_path = _save_upload(attachment_zip, folder / 'attachments.zip') if attachment_zip and attachment_zip.filename else None
    try:
        return ingestion.register_paths(dataset_id, 'upload', html_path, att_path, 'batch')
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class LocalDataset(BaseModel):
    html_path: str
    attachment_path: str | None = None
    kind: Literal['batch', 'single'] = 'batch'


@app.get('/api/v1/datasets/local-defaults')
def local_defaults():
    return ingestion.local_defaults()


@app.post('/api/v1/datasets/local')
def local_dataset(body: LocalDataset):
    """引用本机已有的 ZIP（不复制），适合数 GB 的附件包。"""
    html = Path(body.html_path.strip().strip('"'))
    att = Path(body.attachment_path.strip().strip('"')) if body.attachment_path and body.attachment_path.strip() else None
    expected_html = ('.html', '.htm') if body.kind == 'single' else ('.zip',)
    if not html.is_file() or html.suffix.lower() not in expected_html:
        suffix_hint = '.html/.htm' if body.kind == 'single' else '.zip'
        raise HTTPException(400, f'公告包路径不存在或不是 {suffix_hint} 文件：{html}')
    if att is not None and (not att.is_file() or att.suffix.lower() != '.zip'):
        raise HTTPException(400, f'附件包路径不存在或不是 .zip 文件：{att}')
    try:
        return ingestion.register_paths(ingestion.new_id('ds'), 'local', html.resolve(), att.resolve() if att else None, body.kind)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get('/api/v1/datasets/{dataset_id}/inventory')
def dataset_inventory(dataset_id: str):
    inv = ingestion.inventory(dataset_id)
    if not inv:
        raise HTTPException(404, '数据集不存在')
    return inv


class JobCreate(BaseModel):
    announcement_ids: list[str] | None = None
    limit: int | None = Field(default=None, ge=1)
    bypass_cache: bool = False


@app.post('/api/v1/datasets/{dataset_id}/jobs', status_code=202)
def start_job(dataset_id: str, body: JobCreate | None = None):
    if not ingestion.get_dataset(dataset_id):
        raise HTTPException(404, '数据集不存在')
    if not (settings.has_api_key() and settings.model_for()):
        raise HTTPException(409, '尚未配置模型 API：请先到“模型诊断”页填写 API Key 和模型名称')
    try:
        ids = body.announcement_ids if body else None
        return {'job_id': worker.create_job(dataset_id, ids, body.limit if body else None,
                                            body.bypass_cache if body else False)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get('/api/v1/jobs')
def jobs():
    return worker.list_jobs()


@app.delete('/api/v1/records')
def clear_records():
    """清除任务历史、抽取结果、评测和模型缓存；保留数据集登记及源文件。"""
    try:
        cleared = storage.clear_task_records()
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'cleared': cleared, 'datasets_preserved': True, 'source_files_preserved': True}


@app.delete('/api/v1/jobs/{job_id}')
def clear_job(job_id: str):
    try:
        return storage.clear_job_record(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/v1/jobs/{job_id}')
def job(job_id: str):
    v = worker.job_view(job_id)
    if not v:
        raise HTTPException(404, '任务不存在')
    return v


class RetryBody(BaseModel):
    announcement_ids: list[str]
    reprocess: bool = False


@app.post('/api/v1/jobs/{job_id}/retry')
def retry_job(job_id: str, body: RetryBody):
    if not worker.job_view(job_id):
        raise HTTPException(404, '任务不存在')
    worker.retry(job_id, body.announcement_ids, include_succeeded=body.reprocess)
    return worker.job_view(job_id)


# ---------------- 任务一总览 ----------------
@app.get('/api/v1/task1/overview')
def task1_overview(refresh: bool = False, job_id: str | None = None):
    """任务一四项评分指标；可按单个抽取任务筛选。"""
    from . import task1
    if job_id and not storage.one('SELECT id FROM jobs WHERE id=?', (job_id,)):
        raise HTTPException(404, '任务不存在')
    return task1.overview(refresh, job_id)


# ---------------- 准确率评测 ----------------
@app.get('/api/v1/evaluation')
def evaluation_report(job_id: str | None = None):
    """与人工复核答案对比；不传 job_id 时评测库中全部已抽取公告。"""
    if job_id and not storage.one('SELECT id FROM jobs WHERE id=?', (job_id,)):
        raise HTTPException(404, '任务不存在')
    try:
        return evaluation.evaluate(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


# ---------------- 检索与详情 ----------------
@app.get('/api/v1/stats')
def stats():
    s = queries.store()
    return {'announcements': len(s.anns), 'items': len(s.items), 'organizations': len(s.orgs), 'revision': s.revision}


def _item_query(q, buyer, supplier, brand, date_from, date_to, sort, announcement_id, job_id) -> dict:
    return {'q': q, 'buyer': buyer, 'supplier': supplier, 'brand': brand, 'date_from': date_from, 'date_to': date_to,
            'sort': sort, 'announcement_id': announcement_id, 'job_id': job_id}


@app.get('/api/v1/items')
def items(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500), q: str | None = None,
          buyer: str | None = None, supplier: str | None = None, brand: str | None = None,
          date_from: str | None = None, date_to: str | None = None, sort: str | None = None,
          announcement_id: str | None = None, job_id: str | None = None):
    rows = queries.filter_items(_item_query(q, buyer, supplier, brand, date_from, date_to, sort, announcement_id, job_id))
    start = (page - 1) * page_size
    return {'total': len(rows), 'page': page, 'page_size': page_size,
            'rows': [queries.item_out(r) for r in rows[start:start + page_size]]}


@app.get('/api/v1/items/export')
def items_export(q: str | None = None, buyer: str | None = None, supplier: str | None = None, brand: str | None = None,
                 date_from: str | None = None, date_to: str | None = None, sort: str | None = None,
                 format: str = 'json', page: int | None = None, page_size: int | None = None,
                 announcement_id: str | None = None, job_id: str | None = None):
    rows = queries.filter_items(_item_query(q, buyer, supplier, brand, date_from, date_to, sort, announcement_id, job_id))
    return [queries.item_out(r) for r in rows]


@app.get('/api/v1/export/standard')
def export_standard(job_id: str | None = None, dataset_id: str | None = None, announcement_id: str | None = None):
    """按赛题《输出标准样例表.xlsx》下载提取结果：总表 / 标的表 / 投标人表三个工作表。

    不带参数导出全部已入库公告；job_id 只导出该任务成功的公告；dataset_id 导出该数据集已处理的公告。
    """
    from urllib.parse import quote
    from fastapi.responses import Response
    from . import standard_export
    ids = None
    if job_id and not storage.one('SELECT id FROM jobs WHERE id=?', (job_id,)):
        raise HTTPException(404, '任务不存在')
    if dataset_id:
        ids = [r['announcement_id'] for r in storage.rows('SELECT announcement_id FROM announcements WHERE dataset_id=?', (dataset_id,))]
    if announcement_id:
        ids = [announcement_id]
    if ids is not None and not ids:
        raise HTTPException(404, '该范围内还没有已入库的提取结果')
    try:
        data, counts = standard_export.export(job_id, ids)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    scope = next((j['display_name']+'_'+job_id for j in worker.list_jobs() if j['job_id']==job_id), job_id) if job_id else dataset_id or announcement_id or '全部已入库数据'
    name = f'提取结果_标准样例表_{scope}_{storage.now()[:10]}.xlsx'
    return Response(content=data, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(name)}",
                             'X-Row-Counts': f"{counts['总表']},{counts['标的表']},{counts['投标人表']}"})


@app.get('/api/v1/announcements')
def announcements(q: str = '', limit: int = Query(50, ge=1, le=500), job_id: str | None = None):
    _check_result_job(job_id)
    return queries.announcements(q, limit, job_id)


@app.get('/api/v1/announcements/{aid}')
def announcement(aid: str, job_id: str | None = None):
    _check_result_job(job_id)
    if job_id and aid not in queries.store(job_id).ann_by_id:
        raise HTTPException(404, '该公告不在所选任务的已入库结果中')
    a = queries.announcement(aid)
    if not a:
        raise HTTPException(404, f'未找到公告 {aid}（可能尚未处理或处理失败）')
    return a


@app.get('/api/v1/organizations')
def organizations(q: str = '', role: Literal['buyer', 'winner', 'bidder'] = 'buyer', limit: int = Query(30, ge=1, le=200), job_id: str | None = None):
    _check_result_job(job_id)
    return queries.organizations(q, role, limit, job_id)


def _check_result_job(job_id: str | None):
    if job_id and not storage.one('SELECT id FROM jobs WHERE id=?', (job_id,)):
        raise HTTPException(404, '任务不存在，请重新选择任务范围')


class ProfileBody(BaseModel):
    job_id: str | None = None
    org: str
    date_range: list[str | None] | None = None
    scope: Literal['project', 'package'] = 'project'


@app.get('/api/v1/task2/overview')
def task2_overview(job_id: str | None = None):
    """任务二总览：关系模型规模 + 赛题各项要求的示例查询结果摘要。"""
    _check_result_job(job_id)
    return task2.overview(job_id)


@app.post('/api/v1/profiles/{kind}')
def profile(kind: Literal['buyer', 'supplier'], body: ProfileBody):
    """用户画像：kind=buyer 采购单位画像；kind=supplier 供应商/投标主体画像。"""
    _check_result_job(body.job_id)
    if not body.org.strip():
        raise HTTPException(400, '请选择画像对象')
    try:
        return profiles.run(kind, body.model_dump())
    except LookupError as exc:
        raise HTTPException(404, str(exc).strip("'\"")) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class ScenarioBody(BaseModel):
    job_id: str | None = None
    dataset_id: str | None = None
    buyer_id: str | None = None
    supplier_ids: list[str] | None = None
    date_range: list[str | None] | None = None
    scope: Literal['project', 'package'] = 'project'
    top_k: int = Field(5, ge=1, le=50)
    participant_only: bool | None = None


@app.post('/api/v1/scenarios/{sid}/query')
def scenario(sid: int, body: ScenarioBody):
    _check_result_job(body.job_id)
    if sid not in scenarios.HANDLERS:
        raise HTTPException(404, '场景编号只能是 1–5')
    if sid in (1, 2) and not body.buyer_id:
        raise HTTPException(400, '请选择采购单位')
    if sid == 3 and not body.supplier_ids:
        raise HTTPException(400, '请选择中标供应商')
    if sid in (4, 5) and len(body.supplier_ids or []) < 2:
        raise HTTPException(400, '请至少选择两家供应商')
    try:
        return scenarios.run(sid, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# 静态资源挂载放在所有 API 路由之后，保证 /api、/readyz 等优先匹配。
# 前端使用 hash 路由，因此无需 SPA 回退。
if FRONTEND_DIST.is_dir():
    app.mount('/', StaticFiles(directory=FRONTEND_DIST, html=True), name='frontend')
