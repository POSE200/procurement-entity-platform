from pathlib import Path
import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["qwen", "deepseek"]
ALLOWED_PROVIDERS = frozenset({"qwen", "deepseek"})


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: ProviderName = Field(default="qwen", validation_alias="LLM_PROVIDER")
    qwen_api_key: SecretStr | None = Field(default=None, validation_alias="QWEN_API_KEY")
    qwen_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias="QWEN_BASE_URL",
    )
    qwen_model: str = Field(default="", validation_alias="QWEN_MODEL")
    deepseek_api_key: SecretStr | None = Field(default=None, validation_alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", validation_alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field(default="", validation_alias="DEEPSEEK_MODEL")
    llm_timeout_seconds: float = Field(default=60.0, validation_alias="LLM_TIMEOUT_SECONDS")
    llm_max_concurrency: int = Field(default=16, validation_alias="LLM_MAX_CONCURRENCY", ge=1, le=64)

    def provider_name(self) -> str:
        return self.llm_provider.strip().lower()

    def assert_allowed_provider(self, provider: str | None = None) -> str:
        name = (provider or self.provider_name()).strip().lower()
        if name not in ALLOWED_PROVIDERS:
            raise ValueError(f"Unsupported LLM provider: {name!r}. Allowed: qwen, deepseek")
        return name

    def model_for(self, provider: str | None = None) -> str:
        name = self.assert_allowed_provider(provider)
        return self.qwen_model if name == "qwen" else self.deepseek_model

    def base_url_for(self, provider: str | None = None) -> str:
        name = self.assert_allowed_provider(provider)
        return self.qwen_base_url if name == "qwen" else self.deepseek_base_url

    def has_api_key(self, provider: str | None = None) -> bool:
        name = self.assert_allowed_provider(provider)
        key = self.qwen_api_key if name == "qwen" else self.deepseek_api_key
        return bool(key and key.get_secret_value().strip())


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=None) if os.environ.get('ICT_CLOUD_MODE') == '1' else Settings()


settings = get_settings()

ENV_PATH = PROJECT_ROOT / ".env"

# 网页可修改的配置项：字段名 → .env 变量名
EDITABLE = {
    "llm_provider": "LLM_PROVIDER",
    "qwen_api_key": "QWEN_API_KEY", "qwen_base_url": "QWEN_BASE_URL", "qwen_model": "QWEN_MODEL",
    "deepseek_api_key": "DEEPSEEK_API_KEY", "deepseek_base_url": "DEEPSEEK_BASE_URL", "deepseek_model": "DEEPSEEK_MODEL",
    "llm_max_concurrency": "LLM_MAX_CONCURRENCY", "llm_timeout_seconds": "LLM_TIMEOUT_SECONDS",
}


def _env_value(v: object) -> str:
    s = str(v)
    if any(ch in s for ch in "\r\n"):
        raise ValueError("配置值不能包含换行")
    return s


def update_settings(changes: dict[str, object]) -> None:
    """校验后写入项目根目录 .env（保留其他行与注释），并就地更新运行中的配置，无需重启。"""
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise ValueError(f"不可修改的配置项：{', '.join(sorted(unknown))}")
    # 先用 Settings 校验整体合法性（白名单、取值范围）
    merged = {EDITABLE[k]: (getattr(settings, k).get_secret_value() if isinstance(getattr(settings, k), SecretStr)
                            else getattr(settings, k)) for k in EDITABLE if getattr(settings, k) is not None}
    merged.update({EDITABLE[k]: v for k, v in changes.items()})
    validated = Settings(_env_file=None, **merged)

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.is_file() else []
    pending = {EDITABLE[k]: _env_value(v) for k, v in changes.items()}
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in pending and not line.lstrip().startswith("#"):
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in pending.items()]
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")

    for k in changes:
        setattr(settings, k, getattr(validated, k))
