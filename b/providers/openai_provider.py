from __future__ import annotations
from typing import Sequence, AsyncGenerator
from openai import AsyncOpenAI
from config import cfg
import httpx


class OpenAIChatProvider:
    """OpenAI GPT с поддержкой потокового вывода."""
    def __init__(self, model: str = "gpt-4o"):
        self.model = model
        self.http_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self.client = AsyncOpenAI(
            api_key=cfg.openai_api_key,
            base_url=cfg.openai_api_base,
            http_client=self.http_client,  # <-- ключевой фикс
        )

    async def stream_chat(
        self, messages: Sequence[dict[str, str]], max_tokens: int = 800, temperature: float = 0.7
    ) -> AsyncGenerator[str, None]:
        stream = await self.client.chat.completions.stream(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if delta:
                yield delta


class OpenAIImageProvider:
    """Провайдер изображений через gpt-image-1 с кастомным httpx-клиентом."""
    def __init__(self, model: str = "gpt-image-1"):
        self.model = model
        self.http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
        self.client = AsyncOpenAI(
            api_key=cfg.openai_api_key,
            base_url=cfg.openai_api_base,
            http_client=self.http_client,  # <-- фикс
        )

    async def generate(self, prompt: str) -> bytes:
        import base64
        img = await self.client.images.generate(model=self.model, prompt=prompt, size="1024x1024", n=1)
        b64 = img.data[0].b64_json
        return base64.b64decode(b64)

    async def edit(self, image_bytes: bytes, instruction: str) -> bytes:
        prompt = f"Отредактируй изображение согласно инструкции: {instruction}"
        return await self.generate(prompt)

    async def add_people(self, image_bytes: bytes, description: str) -> bytes:
        prompt = f"На основе исходного фото, добавь людей: {description}. Сохрани стиль и реалистичность."
        return await self.generate(prompt)

    async def celebrity_selfie(self, image_bytes: bytes, celebrity_name: str, style: str | None = None) -> bytes:
        prompt = f"Создай изображение двух людей в формате селфи. Один — исходный человек, второй — {celebrity_name}. {style or ''}".strip()
        return await self.generate(prompt)
