from typing import Any, Protocol


class LLMProvider(Protocol):
    name: str

    async def extract(self, messages: list[dict[str, Any]], response_schema: dict[str, Any]) -> dict[str, Any]:
        ...

    async def health(self) -> dict[str, Any]:
        ...
