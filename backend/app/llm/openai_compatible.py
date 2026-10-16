import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import Settings


_clients: dict[tuple[int, float], httpx.AsyncClient] = {}


def _shared_client(timeout: float) -> httpx.AsyncClient:
    """每个事件循环复用一个连接池：省去每次请求的 TCP/TLS 握手（约 0.3–1 s）。"""
    key = (id(asyncio.get_running_loop()), timeout)
    client = _clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
        _clients[key] = client
    return client


class ModelOutputError(RuntimeError):
    """模型返回不可用（截断、空内容等），带错误码供任务失败明细展示。"""

    def __init__(self, code: str, message: str, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class OpenAICompatibleProvider:
    def __init__(self, settings: Settings, name: str):
        self.settings = settings
        self.name = settings.assert_allowed_provider(name)
        self.transport = None

    @property
    def model(self) -> str:
        return self.settings.model_for(self.name)

    @property
    def base_url(self) -> str:
        return self.settings.base_url_for(self.name).rstrip("/")

    def _api_key(self) -> str | None:
        secret = self.settings.qwen_api_key if self.name == "qwen" else self.settings.deepseek_api_key
        return secret.get_secret_value() if secret else None

    def _headers(self) -> dict[str, str]:
        key = self._api_key()
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"} if key else {}

    async def _post(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        if self.transport is not None:  # 测试注入的传输层
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds, transport=self.transport) as client:
                return await client.post(url, headers=self._headers(), json=payload)
        return await _shared_client(self.settings.llm_timeout_seconds).post(url, headers=self._headers(), json=payload)

    def _response_error(self, response: httpx.Response, data: Any, code: str) -> ModelOutputError:
        from ..storage import DATA_DIR

        error = data.get('error') if isinstance(data, dict) else None
        message = error.get('message') if isinstance(error, dict) else error
        if not message and isinstance(data, dict):
            message = data.get('message') or data.get('detail')
        message = str(message or '服务返回内容缺少合法choices/message结构')[:2000]
        secret = self._api_key()
        if secret:
            message = message.replace(secret, '[REDACTED]')
        request_id = response.headers.get('x-request-id') or (data.get('id') if isinstance(data, dict) else None)
        record = {'time': datetime.now(timezone.utc).isoformat(), 'provider': self.name,
                  'model': self.model, 'http_status': response.status_code, 'code': code,
                  'message': message, 'request_id': request_id,
                  'response_keys': list(data) if isinstance(data, dict) else type(data).__name__}
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with (DATA_DIR / 'llm_errors.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        except OSError:
            pass
        suffix = f'（request_id: {request_id}）' if request_id else ''
        return ModelOutputError(code, f'{code}: {message}{suffix}；未自动重试',
                                retryable=response.status_code == 429 or response.status_code >= 500)

    async def list_models(self) -> list[str]:
        """调用 OpenAI 兼容的 GET {base_url}/models，返回可用模型 ID。"""
        if not self._api_key():
            raise ModelOutputError("api_key_missing", f"{self.name} 未配置 API Key", retryable=False)
        async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
            r = await client.get(f"{self.base_url}/models", headers=self._headers())
        if r.status_code >= 400:
            raise ModelOutputError(f"http_{r.status_code}", "获取模型列表失败（检查 Base URL 与 API Key）", retryable=False)
        data = r.json()
        items = data.get("data") if isinstance(data, dict) else data
        ids = [m.get("id") for m in items or [] if isinstance(m, dict) and m.get("id")]
        return sorted(set(ids))

    async def chat(self, messages: list[dict[str, Any]], max_tokens: int | None = None,
                   json_mode: bool = True) -> dict[str, Any]:
        """每次调用只发一个非流式 chat/completions 请求；失败交由任务状态记录，不自动重试。"""
        if not self._api_key():
            raise ModelOutputError("api_key_missing", f"{self.name} 未配置 API Key", retryable=False)
        if not self.model:
            raise ModelOutputError("model_missing", f"{self.name} 未配置模型名称", retryable=False)
        image_adaptations = []
        if self.name == 'deepseek':
            from .image_limits import prepare_images
            try:
                messages, image_adaptations = await asyncio.to_thread(prepare_images, messages)
            except (ValueError, OSError) as error:
                raise ModelOutputError('image_input_invalid',
                                       f'图片请求适配失败：{type(error).__name__}: {str(error)[:160]}', retryable=False) from error
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0}
        # DeepSeek's thinking mode is enabled by default on current chat models. Its
        # reasoning tokens share the completion budget with the JSON we need, which
        # can truncate otherwise valid structured responses. Keep extraction and
        # evidence review in direct-answer mode; do not send this vendor-specific
        # parameter to Qwen.
        if self.name == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if self.name == 'deepseek' and len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) >= 48 * 1024 ** 2:
            raise ModelOutputError('request_body_limit', '请求超过48MiB，需分批处理附件', retryable=False)
        try:
            response = await self._post(f"{self.base_url}/chat/completions", payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ModelOutputError(type(exc).__name__, "无法连接模型服务或响应超时；未自动重试") from exc
        try:
            data = response.json()
        except ValueError:
            raise self._response_error(response, {'error': {'message': response.text[:2000]}}, f'http_{response.status_code}'
                                       if response.status_code >= 400 else 'invalid_model_response')
        if response.status_code >= 400:
            raise self._response_error(response, data, f'http_{response.status_code}')
        choices = data.get('choices') if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise self._response_error(response, data, 'invalid_model_response')
        choice = choices[0]
        if not isinstance(choice.get('message'), dict):
            raise self._response_error(response, data, 'invalid_model_response')
        if choice['message'].get('content') is not None and not isinstance(choice['message']['content'], str):
            raise self._response_error(response, data, 'invalid_model_response')
        return {"content": choice["message"].get("content") or "", "finish_reason": choice.get("finish_reason"),
                "usage": data.get("usage"), "model": data.get("model"), "request_id": data.get("id"), "raw": data,
                "image_adaptations": image_adaptations}

    async def extract(self, messages: list[dict[str, Any]], response_schema: dict[str, Any]) -> dict[str, Any]:
        result = await self.chat(messages)
        return {"content": result["content"], "raw": result["raw"], "schema": response_schema}

    async def health(self) -> dict[str, Any]:
        if not self._api_key():
            return {"ok": False, "configured": False, "reason": "api_key_missing"}
        if not self.model:
            return {"ok": False, "configured": False, "reason": "model_missing"}
        try:
            result = await self.extract(
                [{"role": "user", "content": 'Return exactly this JSON object: {"ok": true}'}],
                {"type": "object", "properties": {"ok": {"type": "boolean"}}},
            )
            return {"ok": True, "configured": True, "response": result["content"]}
        except ModelOutputError as exc:
            return {"ok": False, "configured": True, "reason": exc.code, "detail": str(exc)}
        except Exception as exc:
            return {"ok": False, "configured": True, "reason": type(exc).__name__, "detail": "模型请求失败，请检查网络、额度、模型名称及服务端状态；不回传原始异常以保护凭据。"}
