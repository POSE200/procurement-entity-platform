from ..config import Settings
from .base import LLMProvider
from .openai_compatible import OpenAICompatibleProvider


def create_provider(settings: Settings, provider: str | None = None) -> LLMProvider:
    name = settings.assert_allowed_provider(provider)
    return OpenAICompatibleProvider(settings, name)
