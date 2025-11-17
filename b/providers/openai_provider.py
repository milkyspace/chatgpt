from __future__ import annotations
from typing import Sequence, AsyncGenerator
from openai import AsyncOpenAI
from config import cfg
import httpx


class OpenAIChatProvider:
    """OpenAI GPT с поддержкой потокового вывода и кастомным httpx-клиентом."""

    def __init__(self, model: str = "gpt-4o"):
        self.model = model
        self.http_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self.client = AsyncOpenAI(
            api_key=cfg.openai_api_key,
            base_url=cfg.openai_api_base,
            http_client=self.http_client
        )

    async def stream_chat(
            self, messages: Sequence[dict[str, str]], max_tokens: int = 800, temperature: float = 0.7
    ) -> AsyncGenerator[str, None]:
        """Асинхронный стриминг OpenAI GPT-4o."""
        async with self.client.chat.completions.stream(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
        ) as stream:
            async for event in stream:
                if event.type == "message.delta":
                    delta = event.delta.content or ""
                    if delta:
                        yield delta


class OpenAIImageProvider:
    """Провайдер изображений через DALL-E с кастомным httpx-клиентом."""

    def __init__(self, model: str = "dall-e-3"):  # ← ИЗМЕНИТЕ ЗДЕСЬ
        self.model = model
        self.http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
        self.client = AsyncOpenAI(
            api_key=cfg.openai_api_key,
            base_url=cfg.openai_api_base,
            http_client=self.http_client,
        )

    async def generate(self, prompt: str) -> bytes:
        import base64
        try:
            response = await self.client.images.generate(
                model=self.model,
                prompt=prompt,
                size="1024x1024",
                n=1,
                response_format="b64_json"  # ← ДОБАВЬТЕ ЭТО
            )
            b64 = response.data[0].b64_json
            return base64.b64decode(b64)
        except Exception as e:
            print(f"OpenAI API Error: {e}")
            raise

    async def edit(self, image_bytes: bytes, instruction: str) -> bytes:
        prompt = f"Отредактируй изображение согласно инструкции: {instruction}"
        return await self.generate(prompt)

    async def add_people(self, image_bytes: bytes, description: str) -> bytes:
        prompt = f"На основе исходного фото, добавь людей: {description}. Сохрани стиль и реалистичность."
        return await self.generate(prompt)

    async def celebrity_selfie(self, image_bytes: bytes, celebrity_name: str, style: str | None = None) -> bytes:
        # Для DALL-E нам нужно создать новый промпт, так как он не принимает исходное изображение
        prompt = f"Реалистичное селфи двух людей. Один выглядит как {celebrity_name}, второй - обычный человек. {style or ''} Фотография должна выглядеть как настоящее селфи, естественное освещение, высокое качество."
        return await self.generate(prompt)