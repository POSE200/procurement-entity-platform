"""适配DeepSeek的图片格式和尺寸，保留原始附件和OCR。"""
import base64
import io
import json
import math


REQUEST_TARGET = 44 * 1024 ** 2


def _message_bytes(messages):
    return len(json.dumps(messages, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def _compact_request(messages):
    """大请求按模型视觉像素预算生成传输副本，保留全部页面和OCR文本。"""
    before = _message_bytes(messages)
    if before <= REQUEST_TARGET:
        return messages, []
    from PIL import Image, ImageOps

    for quality in (90, 80, 70, 65, 60):
        result, changes, index = [], [], 0
        for message in messages:
            if not isinstance(message.get('content'), list):
                result.append(message)
                continue
            parts = []
            for part in message['content']:
                if part.get('type') != 'image_url':
                    parts.append(part)
                    continue
                image_index, index = index, index + 1
                info = part.get('image_url') or {}
                url = info.get('url', '')
                if not url.startswith('data:') or ';base64,' not in url:
                    parts.append(part)
                    continue
                with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))) as image:
                    if getattr(image, 'n_frames', 1) > 1:
                        raise ValueError('大请求包含多帧图片，需逐帧解析后传入')
                    normalized = ImageOps.exif_transpose(image)
                    original_size = normalized.size
                    factor = min(1.0, math.sqrt(1690000 / (normalized.width * normalized.height)))
                    size = tuple(max(1, int(side * factor)) for side in normalized.size)
                    if size != normalized.size:
                        normalized = normalized.resize(size, Image.Resampling.LANCZOS)
                    if 'A' in normalized.getbands() or 'transparency' in normalized.info:
                        rgba = normalized.convert('RGBA')
                        rgb = Image.new('RGB', rgba.size, 'white')
                        rgb.paste(rgba, mask=rgba.getchannel('A'))
                    else:
                        rgb = normalized.convert('RGB')
                    buffer = io.BytesIO()
                    rgb.save(buffer, format='JPEG', quality=quality, optimize=True, subsampling=0)
                parts.append({**part, 'image_url': {**info, 'url': 'data:image/jpeg;base64,' +
                                                   base64.b64encode(buffer.getvalue()).decode('ascii')}})
                changes.append({'image_index': image_index, 'reason': 'request_body_limit',
                                'original_size': list(original_size), 'request_size': list(size),
                                'request_format': 'JPEG', 'jpeg_quality': quality})
            result.append({**message, 'content': parts})
        after = _message_bytes(result)
        if after <= REQUEST_TARGET:
            changes.append({'reason': 'request_body_limit', 'messages_bytes_before': before,
                            'messages_bytes_after': after, 'images_preserved': index})
            return result, changes
    raise ValueError(f'保留全部页面的请求仍超过44MiB传输预算：{after}字节，需分批处理附件')


def prepare_images(messages):
    from PIL import Image, ImageOps

    count = sum(1 for message in messages if isinstance(message.get('content'), list)
                for part in message['content'] if part.get('type') == 'image_url')
    if count > 600:
        raise ValueError(f'图片共{count}张，超过接口单次600张上限，需分批处理附件')
    limit = 4096 if count >= 15 else 8192
    result, changes = [], []
    image_index = 0
    for message in messages:
        content = message.get('content')
        if not isinstance(content, list):
            result.append(message)
            continue
        parts = []
        for part in content:
            if part.get('type') != 'image_url':
                parts.append(part)
                continue
            index = image_index
            image_index += 1
            info = part.get('image_url') or {}
            url = info.get('url', '')
            if not url.startswith('data:'):
                parts.append(part)
                continue
            header, encoded = url.split(',', 1)
            if ';base64' not in header:
                parts.append(part)
                continue
            raw = base64.b64decode(encoded, validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                before = image.size
                original_format = image.format
                needs_format_conversion = original_format not in {'JPEG', 'PNG', 'GIF', 'WEBP'}
                if max(before) <= limit and not needs_format_conversion:
                    parts.append(part)
                    continue
                if original_format == 'WMF':
                    image.load(dpi=144)
                elif getattr(image, 'n_frames', 1) > 1:
                    raise ValueError('多帧图片需逐帧解析后传入，不能只保留首帧')
                normalized = ImageOps.exif_transpose(image)
                normalized.thumbnail((limit, limit), Image.Resampling.LANCZOS)
                buffer = io.BytesIO()
                if image.format == 'JPEG':
                    normalized.convert('RGB').save(buffer, format='JPEG', quality=95)
                    mime = 'image/jpeg'
                else:
                    normalized.save(buffer, format='PNG')
                    mime = 'image/png'
                adapted = {**info, 'url': f'data:{mime};base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')}
                parts.append({**part, 'image_url': adapted})
                changes.append({'image_index': index, 'original_size': list(before),
                                'request_size': list(normalized.size), 'max_dimension': limit,
                                'original_format': original_format,
                                'request_format': 'JPEG' if mime == 'image/jpeg' else 'PNG'})
        result.append({**message, 'content': parts})
    result, compact_changes = _compact_request(result)
    return result, changes + compact_changes
