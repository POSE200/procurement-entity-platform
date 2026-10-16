"""公告 HTML 与附件解析：安全读取 ZIP、按内容签名识别格式、抽取文本证据。

- 不按后缀判断格式，先看文件头魔数。
- ZIP 内文件名只作标签，从不作为磁盘路径使用（无路径穿越风险）；全部在内存中读取。
- 支持旧Word/Excel、RAR及扫描页OCR；解析异常保留来源和原因。
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from bs4 import BeautifulSoup

logging.getLogger('pypdf').setLevel(logging.ERROR)  # 公告附件常见轻微格式问题，警告会刷屏

ANN_ID_RE = re.compile(r'(t\d{8}_\d+)', re.I)
MAX_MEMBER_BYTES = 200 * 1024 ** 2   # 单个附件成员上限
MAX_NESTED_DEPTH = 3                 # 嵌套 ZIP 深度上限
MAX_TOTAL_UNCOMPRESSED = 1024 ** 3   # 单篇附件展开总量上限

PROJECT_NO_RE = re.compile(r'项目编号[：:]\s*([^\s（(，,。；;]{3,60})')
PROJECT_NO_STOP = re.compile(
    r'(二、|三、|四、|采购计划编号|项目名称|招标文件编号|标段|\d{1,2}、采购|、采购|[)）]|中标公告|成交公告|结果公告)')
TIME_RE = re.compile(r'(\d{4})年(\d{2})月(\d{2})日\s*(\d{2}):(\d{2})')


def announcement_id_of(name: str) -> str | None:
    """从 ZIP 成员名取公告 ID（允许目录前缀），只认文件名本身。"""
    stem = PurePosixPath(name.replace('\\', '/')).name
    m = ANN_ID_RE.fullmatch(stem.rsplit('.', 1)[0]) if '.' in stem else None
    return m.group(1).lower() if m else None


def zip_member_name(info: zipfile.ZipInfo) -> str:
    """修正未设 UTF-8 标志的中文文件名（Windows 压缩常见 GBK）。"""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode('cp437').decode('gbk')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def clean_lines(text: str) -> str:
    lines = [re.sub(r'[ \s]+', ' ', ln).strip() for ln in text.splitlines()]
    return '\n'.join(ln for ln in lines if ln)


def _table_to_text(table) -> str:
    """表格逐行转成 | 分隔文本，保留行列结构供模型阅读。"""
    out = []
    for tr in table.find_all('tr'):
        cells = [re.sub(r'\s+', ' ', td.get_text(' ', strip=True)) for td in tr.find_all(['td', 'th'], recursive=False)]
        if any(cells):
            out.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(out)


def parse_html(raw: bytes) -> dict:
    soup = BeautifulSoup(raw.decode('utf-8', errors='ignore'), 'html.parser')
    title_el = soup.select_one('div.vF_detail_header h2') or soup.find('title')
    time_el = soup.select_one('#pubTime')
    summary_el = soup.select_one('div.table')
    body_el = soup.select_one('div.vF_detail_content') or soup.body or soup
    summary = clean_lines(summary_el.get_text('\n')) if summary_el else ''
    body_display = clean_lines(body_el.get_text('\n'))

    # 内容表保留逐行文本，外层排版表只去标签，避免再次压平内层行界。
    body_copy = BeautifulSoup(str(body_el), 'html.parser')
    leaves = [t for t in body_copy.find_all('table') if not t.find('table')]
    for t in leaves:
        t.replace_with(body_copy.new_string('\n' + _table_to_text(t) + '\n'))
    for t in body_copy.find_all('table'):
        t.unwrap()
    body_for_model = clean_lines(body_copy.get_text('\n'))

    # 页面导航栏目（中央/地方 × 中标/成交），用于标准样例表复原公告源地址 URL
    crumbs = soup.select_one('div.vF_deail_currentloc')
    channel = None
    if crumbs:
        names = [a.get('title') for a in crumbs.find_all('a') if a.get('title')]
        level = next((x for x in names if x in ('中央公告', '地方公告')), None)
        kind = next((x for x in names if x in ('中标公告', '成交公告')), None)
        channel = {('中央公告', '中标公告'): 'zygg/zbgg', ('中央公告', '成交公告'): 'zygg/cjgg',
                   ('地方公告', '中标公告'): 'dfgg/zbgg', ('地方公告', '成交公告'): 'dfgg/cjgg'}.get((level, kind))

    flat = re.sub(r'\s+', '', body_display)
    m = PROJECT_NO_RE.search(flat)
    publish = None
    if time_el and (t := TIME_RE.search(time_el.get_text())):
        publish = f'{t[1]}-{t[2]}-{t[3]}T{t[4]}:{t[5]}:00+08:00'
    return {
        'title': title_el.get_text(strip=True) if title_el else '',
        'publish_time': publish,
        'project_no': (PROJECT_NO_STOP.split(m[1])[0].strip('：:、') or None) if m else None,
        'summary_text': summary,
        'channel': channel,
        'body_text': body_display,
        'model_text': body_for_model,
    }


# ---------------- 附件 ----------------
@dataclass
class AttachmentDoc:
    name: str
    kind: str          # pdf / docx / xlsx / image / text / unsupported:<原因>
    text: str = ''
    pages: int | None = None
    note: str | None = None
    images: list[dict] = field(default_factory=list)  # {name, mime, data: bytes}


@dataclass
class AttachmentBundle:
    docs: list[AttachmentDoc] = field(default_factory=list)
    error: str | None = None


def detect(data: bytes) -> str:
    if data[:6] == b'7z\xbc\xaf\x27\x1c':
        return '7z'
    if data[:5] == b'%PDF-':
        return 'pdf'
    if data[:4] == b'PK\x03\x04':
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except zipfile.BadZipFile:
            return 'bad_zip'
        if '[Content_Types].xml' in names:
            if any(n.startswith('word/') for n in names):
                return 'docx'
            if any(n.startswith('xl/') for n in names):
                return 'xlsx'
            return 'ooxml_other'
        return 'zip'
    if data[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        return 'ole'
    if data[:4] == b'Rar!':
        return 'rar'
    if data[:8] == b'\x89PNG\r\n\x1a\n' or data[:3] == b'\xff\xd8\xff':
        return 'image'
    head = data[:512].lstrip().lower()
    if head.startswith(b'<!doctype html') or head.startswith(b'<html'):
        return 'html'
    if head.startswith(b'{') or head.startswith(b'['):
        return 'json'
    return 'unknown'


def _pdf_text(data: bytes) -> tuple[str, int]:
    """优先用 pypdfium2（PDFium 引擎，约为 pypdf 的 10 倍速度，版面顺序也更好）；未安装时回退 pypdf。"""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return _pdf_text_pypdf(data)
    doc = pdfium.PdfDocument(data)
    try:
        parts = []
        for i in range(len(doc)):
            page = doc[i]
            try:
                tp = page.get_textpage()
                t = tp.get_text_bounded() or ''
                tp.close()
            except Exception:  # 个别页面损坏时跳过该页
                t = ''
            finally:
                page.close()
            if t.strip():
                parts.append(f'[第{i + 1}页]\n{t}')
        return clean_lines('\n'.join(parts)), len(doc)
    finally:
        doc.close()


def _pdf_text_pypdf(data: bytes) -> tuple[str, int]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ''
        except Exception:  # 个别页面损坏时跳过该页
            t = ''
        if t.strip():
            parts.append(f'[第{i + 1}页]\n{t}')
    return clean_lines('\n'.join(parts)), len(reader.pages)


def _docx_text(data: bytes) -> str:
    from xml.etree import ElementTree as ET
    ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    root = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read('word/document.xml'))
    out: list[str] = []

    def walk(el):
        for child in el:
            if child.tag == ns + 'p':
                out.append(''.join(t.text or '' for t in child.iter(ns + 't')))
            elif child.tag == ns + 'tbl':
                for tr in child.iter(ns + 'tr'):
                    cells = [''.join(t.text or '' for t in tc.iter(ns + 't')).strip() for tc in tr.findall(ns + 'tc')]
                    if any(cells):
                        out.append('| ' + ' | '.join(cells) + ' |')
            else:
                walk(child)

    walk(root)
    return clean_lines('\n'.join(out))


def _xlsx_text(data: bytes) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f'[工作表 {ws.title}]')
        for n, row in enumerate(ws.iter_rows(values_only=True)):
            if n > 2000:
                out.append('…（行数过多已截断）')
                break
            cells = ['' if v is None else str(v).strip() for v in row]
            if any(cells):
                out.append('| ' + ' | '.join(cells) + ' |')
    return clean_lines('\n'.join(out))


def _pdf_scan_pages(data: bytes) -> list[int]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(data)
    selected = []
    try:
        for index in range(len(doc)):
            page = doc[index]
            try:
                tp = page.get_textpage()
                text = tp.get_text_bounded() or ''
                tp.close()
                chars = len(re.sub(r'\s+', '', text))
                width, height = page.get_size()
                scan_image = False
                if chars < 300:
                    image_area = 0.0
                    for obj in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE], max_depth=3):
                        left, bottom, right, top = obj.get_pos()
                        area = max(0, min(right, width) - max(left, 0)) * max(0, min(top, height) - max(bottom, 0))
                        if area >= width * height * 0.015:
                            image_area += area
                        if area >= width * height * 0.3:
                            scan_image = True
                    scan_image = scan_image or image_area >= width * height * 0.20
                # 扫描合同可能另带标题、水印等少量文字，不能按整份PDF是否有文本判断。
                if not text.strip() or scan_image:
                    selected.append(index)
            finally:
                page.close()
        return selected
    finally:
        doc.close()


UNSUPPORTED = {
    'image': '图片需 OCR，暂未接入',
    'bad_zip': '损坏的 ZIP',
    'json': '内容是 JSON 错误响应而非文档',
    'html': '内容是网页而非文档（可能是下载错误页）',
    'unknown': '无法识别的格式',
    'ooxml_other': '不支持的 Office 文档类型',
}


def _collect_7z(data: bytes, prefix: str, depth: int, out: list,
                bundle: AttachmentBundle, budget: list[int]) -> None:
    """7z 成员写入有大小上限的内存流；文件名仅作来源标签。"""
    try:
        import py7zr
        from py7zr.io import Py7zIO, WriterFactory
    except ImportError:
        bundle.docs.append(AttachmentDoc(prefix.rstrip('/'), 'unsupported', note='7z解析依赖py7zr未安装'))
        return

    class Buffer(Py7zIO):
        def __init__(self, limit):
            self.stream, self.limit = io.BytesIO(), limit

        def write(self, value):
            if self.stream.tell() + len(value) > self.limit:
                raise ValueError('7z成员实际大小超过声明大小或上限')
            return self.stream.write(value)

        def read(self, size=None):
            return self.stream.read(-1 if size is None else size)

        def seek(self, offset, whence=0):
            return self.stream.seek(offset, whence)

        def flush(self):
            self.stream.flush()

        def size(self):
            return len(self.stream.getbuffer())

    class Factory(WriterFactory):
        def __init__(self, sizes):
            self.sizes, self.buffers = sizes, {}

        def create(self, filename):
            # py7zr 将成员名交给 factory，不在文件系统创建成员。
            key = str(filename).replace('\\', '/')
            if key not in self.sizes:
                raise ValueError('7z解包返回未登记的成员')
            value = Buffer(self.sizes[key])
            self.buffers[key] = value
            return value

    try:
        with py7zr.SevenZipFile(io.BytesIO(data), mode='r') as archive:
            if archive.needs_password():
                raise ValueError('7z压缩包需要密码')
            sizes = {}
            for info in archive.list():
                name = info.filename.replace('\\', '/')
                if info.is_directory:
                    continue
                if not info.is_file or info.is_symlink:
                    bundle.docs.append(AttachmentDoc(prefix + name, 'unsupported', note='7z成员不是普通文件'))
                    continue
                if name in sizes:
                    raise ValueError('7z成员名称重复')
                if info.uncompressed > MAX_MEMBER_BYTES:
                    bundle.docs.append(AttachmentDoc(prefix + name, 'unsupported', note='文件过大，跳过'))
                    continue
                if info.uncompressed > budget[0]:
                    bundle.docs.append(AttachmentDoc(prefix + name, 'unsupported', note='附件展开总量超限'))
                    continue
                budget[0] -= info.uncompressed
                sizes[name] = info.uncompressed
            if not sizes:
                return
            factory = Factory(sizes)
            archive.extract(targets=list(sizes), factory=factory)
            for name, buf in factory.buffers.items():
                member = buf.stream.getvalue()
                _collect_member(member, prefix + name, depth, out, bundle, budget)
    except Exception as exc:
        bundle.docs.append(AttachmentDoc(prefix.rstrip('/'), 'unsupported',
                                         note=f'7z解析失败：{type(exc).__name__}: {str(exc)[:100]}'))


def _collect_member(member: bytes, name: str, depth: int, out: list,
                    bundle: AttachmentBundle, budget: list[int]) -> None:
    kind = detect(member)
    if kind in ('zip', '7z', 'rar'):
        if depth >= MAX_NESTED_DEPTH:
            bundle.docs.append(AttachmentDoc(name, 'unsupported', note='嵌套层数超限'))
        else:
            try:
                _collect(member, name + '/', depth + 1, out, bundle, budget)
            except zipfile.BadZipFile:
                bundle.docs.append(AttachmentDoc(name, 'unsupported', note=UNSUPPORTED['bad_zip']))
    else:
        out.append((name, member, kind))


def _collect_rar(data: bytes, prefix: str, depth: int, out: list,
                 bundle: AttachmentBundle, budget: list[int]) -> None:
    try:
        import rarfile

        candidates = [os.environ.get('ICT_UNRAR_PATH'), shutil.which('unrar'),
                      str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'WinRAR/UnRAR.exe')]
        tool = next((path for path in candidates if path and os.path.isfile(path)), None)
        if tool:
            rarfile.UNRAR_TOOL = tool
        with rarfile.RarFile(io.BytesIO(data), errors='strict') as archive:
            if archive.needs_password():
                raise ValueError('RAR压缩包需要密码')
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = prefix + info.filename.replace('\\', '/')
                if not info.is_file() or info.is_symlink():
                    bundle.docs.append(AttachmentDoc(name, 'unsupported', note='RAR成员不是普通文件'))
                    continue
                if info.file_size > MAX_MEMBER_BYTES or info.file_size > budget[0]:
                    bundle.docs.append(AttachmentDoc(name, 'unsupported', note='RAR成员超过大小上限'))
                    continue
                with archive.open(info) as stream:
                    member = stream.read(min(info.file_size, MAX_MEMBER_BYTES) + 1)
                if len(member) != info.file_size:
                    raise ValueError('RAR成员实际大小与声明不符')
                budget[0] -= len(member)
                _collect_member(member, name, depth, out, bundle, budget)
    except Exception as exc:
        bundle.docs.append(AttachmentDoc(prefix.rstrip('/'), 'unsupported',
                                         note=f'RAR解析失败：{type(exc).__name__}: {str(exc)[:100]}'))


def _collect(data: bytes, prefix: str, depth: int, out: list, bundle: AttachmentBundle, budget: list[int]) -> None:
    """展开（嵌套）ZIP，收集 (名称, 字节, 格式)。只在内存中处理，成员名不落盘。"""
    if detect(data) == '7z':
        _collect_7z(data, prefix, depth, out, bundle, budget)
        return
    if detect(data) == 'rar':
        _collect_rar(data, prefix, depth, out, bundle, budget)
        return
    zf = zipfile.ZipFile(io.BytesIO(data))
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = prefix + zip_member_name(info)
        if info.file_size > MAX_MEMBER_BYTES:
            bundle.docs.append(AttachmentDoc(name, 'unsupported', note='文件过大，跳过'))
            continue
        budget[0] -= info.file_size
        if budget[0] < 0:
            bundle.docs.append(AttachmentDoc(name, 'unsupported', note='附件展开总量超限，后续文件未解析'))
            return
        member = zf.read(info)
        _collect_member(member, name, depth, out, bundle, budget)


def _ocr_image_bytes(data: bytes) -> str:
    """OCR一张图片，OCR失败时保留原图给Vision，不让异常阻断整篇公告。"""
    global _OCR_ENGINE, _OCR_UNAVAILABLE
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return ''
        if _OCR_UNAVAILABLE:
            return ''
        if _OCR_ENGINE is None:
            with _OCR_LOCK:
                if _OCR_ENGINE is None and not _OCR_UNAVAILABLE:
                    try:
                        from rapidocr_onnxruntime import RapidOCR
                        _OCR_ENGINE = RapidOCR()
                    except Exception:
                        _OCR_UNAVAILABLE = True
                        return ''
        # RapidOCR 的单次推理放在锁内，兼容线程回退路径；进程池仍可并行处理不同公告。
        with _OCR_LOCK:
            result, _ = _OCR_ENGINE(img)
        if not result:
            return ''
        return '\n'.join(str(row[1]) for row in result if len(row) > 1 and str(row[1]).strip())
    except Exception:  # OCR是辅助通道，Vision仍可直接读取原图
        return ''


def _docx_media(data: bytes, prefix: str) -> tuple[str, list[dict]]:
    """提取DOCX内嵌图片，同时生成OCR文本和Vision图片载荷。"""
    text_parts, images = [], []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = sorted(n for n in zf.namelist()
                           if n.startswith('word/media/') and not n.endswith('/'))
            for n in names:
                raw = zf.read(n)
                ext = PurePosixPath(n).suffix.lower()
                mime = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                        'webp': 'image/webp'}.get(ext.lstrip('.'), 'image/png')
                images.append({'name': f'{prefix}/{PurePosixPath(n).name}', 'mime': mime, 'data': raw})
                ocr = _ocr_image_bytes(raw)
                if ocr:
                    text_parts.append(f'【DOCX内嵌图片：{PurePosixPath(n).name}】\n{ocr}')
    except (zipfile.BadZipFile, OSError):
        pass
    return '\n'.join(text_parts), images


def _ole_media(data: bytes, prefix: str) -> tuple[str, list[dict]]:
    import olefile
    from PIL import Image

    images, texts, seen = [], [], set()
    with olefile.OleFileIO(io.BytesIO(data)) as container:
        for stream in container.listdir():
            if stream[-1] != 'Data':
                continue
            raw = container.openstream(stream).read()
            for marker in (b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n'):
                offset = 0
                while True:
                    index = raw.find(marker, offset)
                    if index < 0:
                        break
                    offset = index + len(marker)
                    try:
                        picture = Image.open(io.BytesIO(raw[index:]))
                        if picture.width < 80 or picture.height < 60:
                            continue
                        picture.load()
                        buffer = io.BytesIO()
                        picture.convert('RGB').save(buffer, format='PNG')
                        payload = buffer.getvalue()
                        if payload in seen:
                            continue
                        seen.add(payload)
                        name = f'{prefix}/embedded-{len(images) + 1}.png'
                        images.append({'name': name, 'mime': 'image/png', 'data': payload})
                        recognized = _ocr_image_bytes(payload)
                        if recognized.strip():
                            texts.append(f'【内嵌图片OCR：{name}】\n{recognized}')
                    except Exception:
                        continue
    return '\n'.join(texts), images


_OCR_ENGINE = None
_OCR_UNAVAILABLE = False
_OCR_LOCK = threading.Lock()


def _pdf_media(data: bytes, name: str, pages: list[int] | None = None) -> list[dict]:
    """渲染指定扫描页面，同时保留原始页码。"""
    images = []
    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(data)
        for i in (range(len(doc)) if pages is None else pages):
            page = doc[i]
            bitmap = page.render(scale=2.0)
            pil = bitmap.to_pil()
            buf = io.BytesIO()
            pil.save(buf, format='PNG')
            images.append({'name': f'{name}#page-{i + 1}.png', 'mime': 'image/png', 'data': buf.getvalue()})
            page.close()
        doc.close()
    except Exception:
        pass
    return images


def extract_document(data: bytes, name: str, kind: str) -> AttachmentDoc:
    try:
        if kind == 'pdf':
            text, pages = _pdf_text(data)
            scan_pages = _pdf_scan_pages(data)
            images = _pdf_media(data, name, scan_pages) if scan_pages else []
            ocr_parts = []
            for image in images:
                recognized = _ocr_image_bytes(image['data']) if image.get('data') else ''
                if recognized.strip():
                    ocr_parts.append(f"【扫描页OCR：{image['name']}】\n{recognized}")
            ocr = '\n'.join(ocr_parts)
            return AttachmentDoc(name, 'pdf', '\n'.join(x for x in (text, ocr) if x), pages,
                                 None if text or images else '扫描件无文本且无法渲染', images)
        if kind == 'docx':
            text = _docx_text(data)
            media_text, images = _docx_media(data, name)
            return AttachmentDoc(name, 'docx', '\n'.join(x for x in (text, media_text) if x),
                                 None, None if text or images else 'DOCX无文本且无内嵌图片', images)
        if kind == 'xlsx':
            return AttachmentDoc(name, 'xlsx', _xlsx_text(data))
        if kind == 'ole':
            from .legacy_office import read_ole
            text, actual_kind = read_ole(data)
            media_text, images = _ole_media(data, name) if actual_kind == 'doc' else ('', [])
            return AttachmentDoc(name, actual_kind, clean_lines('\n'.join((text, media_text))),
                                 note=None if text.strip() or images else '旧Office正文为空', images=images)
        if kind == 'image':
            import cv2
            import numpy as np
            if cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR) is None:
                return AttachmentDoc(name, 'unsupported', note='图片解码失败，OCR/Vision不可用')
            mime = 'image/png' if data.startswith(b'\x89PNG') else 'image/jpeg'
            return AttachmentDoc(name, 'image', _ocr_image_bytes(data), 1, None,
                                 [{'name': name, 'mime': mime, 'data': data}])
        return AttachmentDoc(name, 'unsupported', note=UNSUPPORTED.get(kind, kind))
    except Exception as exc:  # 单个文件解析失败不影响其他文件
        return AttachmentDoc(name, 'unsupported', note=f'解析失败：{type(exc).__name__}')


def parse_attachment_zip(data: bytes) -> AttachmentBundle:
    """按证据优先级解析附件；不因累计文本字符数而跳过后续文件。"""
    bundle = AttachmentBundle()
    members: list = []
    try:
        _collect(data, '', 1, members, bundle, [MAX_TOTAL_UNCOMPRESSED])
    except zipfile.BadZipFile:
        bundle.error = '附件包不是有效的 ZIP'
        return bundle
    for name, raw, kind in sorted(members, key=lambda m: (doc_priority(m[0]), len(m[1]))):
        doc = extract_document(raw, name, kind)
        bundle.docs.append(doc)
    return bundle


# 附件选取优先级：成交/报价/明细类优先，招标/采购文件（需求而非成交）靠后
PRIORITY = [
    (re.compile(r'报价|明细|清单|开标|评审|评标|中标|成交|结果|分项'), 0),
    (re.compile(r'响应|投标文件|声明'), 1),
    (re.compile(r'招标文件|采购文件|磋商文件|谈判文件|需求|文件集'), 3),
]


def doc_priority(name: str) -> int:
    for rx, p in PRIORITY:
        if rx.search(name):
            return p
    return 2
