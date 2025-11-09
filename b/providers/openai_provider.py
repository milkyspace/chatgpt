from __future__ import annotations
from openai import AsyncOpenAI
from typing import AsyncGenerator, Sequence
from b.config import cfg
import base64


class OpenAIChatProvider:
    """OpenAI GPT с поддержкой потокового вывода."""
    def __init__(self, model: str = "gpt-4o"):
        self.client = AsyncOpenAI(api_key=cfg.openai_api_key, base_url=cfg.openai_api_base)
        self.model = model

    async def chat(self, messages: Sequence[dict[str, str]], max_tokens: int, temperature: float = 0.7) -> str:
        # Простой вызов Chat Completions (соблюдаем лимиты)
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    async def stream_chat(
            self, messages: Sequence[dict[str, str]], max_tokens: int = 800, temperature: float = 0.7
    ) -> AsyncGenerator[str, None]:
        """Асинхронно стримит ответ по мере генерации."""
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
    """Провайдер изображений через gpt-image-1 (text-to-image и basic editing).

    ВАЖНО: Соблюдаем политики — часть операций (напр., deepfake со знаменитостью) может быть запрещена.
    Реализуем через фасад services.safety.SafetyGuard.
    """

    def __init__(self, model: str = "gpt-image-1"):
        self.client = AsyncOpenAI(api_key=cfg.openai_api_key, base_url=cfg.openai_api_base)
        self.model = model

    async def generate(self, prompt: str) -> bytes:
        img = await self.client.images.generate(model=self.model, prompt=prompt, size="1024x1024", n=1)
        b64 = img.data[0].b64_json
        return base64.b64decode(b64)

    async def edit(self, image_bytes: bytes, instruction: str) -> bytes:
        # Для простоты используем text-guided edit как новую генерацию с описанием.
        # В реальном проде можно перейти на images.edits (если провайдер позволит) с масками.
        prompt = f"Отредактируй изображение согласно инструкции: {instruction}"
        return await self.generate(prompt)

    async def add_people(self, image_bytes: bytes, description: str) -> bytes:
        prompt = f"На основе исходного фото, добавь людей: {description}. Сохрани стиль и реалистичность."
        return await self.generate(prompt)

    async def celebrity_selfie(self, image_bytes: bytes, celebrity_name: str, style: str | None = None) -> bytes:
        # Мягкая реализация — вернём отказ, если safety не пропустит (см. services.safety)
        prompt = f"Создай изображение двух людей в формате селфи. Один — исходный человек, второй — {celebrity_name}. {style or ''}".strip()
        return await self.generate(prompt)
