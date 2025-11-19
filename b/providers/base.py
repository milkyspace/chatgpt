from __future__ import annotations
from typing import Protocol, Any, Sequence, AsyncGenerator, Optional


class ChatProvider(Protocol):
    """Интерфейс чат-провайдера."""

    async def stream_chat(
            self,
            messages: Sequence[dict[str, Any]],
            max_tokens: int,
            temperature: float = 0.7
    ) -> AsyncGenerator[str, None]:
        ...

    async def chat_with_tools(
            self,
            messages: Sequence[dict[str, Any]],
            tools: list[dict],
            max_tokens: int = 800,
            temperature: float = 0.7
    ) -> dict[str, Any]:
        ...


class ImageProvider(Protocol):
    """Интерфейс провайдера изображений."""

    async def generate(self, prompt: str, aspect_ratio: str = "1:1") -> bytes:
        ...

    async def edit_image(self, image_bytes: bytes, instruction: str) -> bytes:
        ...

    async def analyze_image(self, image_bytes: bytes, question: str) -> str:
        ...

    async def add_people(self, image_bytes: bytes, description: str) -> bytes:
        ...

    async def celebrity_selfie(self, image_bytes: bytes, celebrity_name: str, style: str = None) -> bytes:
        ...