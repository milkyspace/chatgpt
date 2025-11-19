from __future__ import annotations
from typing import Sequence, AsyncGenerator
from openai import AsyncOpenAI, OpenAI
from config import cfg
import httpx


class OpenAIChatProvider:
    """
    Провайдер для GPT с поддержкой потоковой передачи данных.
    Использует новый OpenAI SDK (v1.x), в котором используется client.chat.completions.create(stream=True)
    """

    def __init__(self, model: str = "gpt-4o"):
        self.model = model

        # кастомный httpx клиент — корректно
        self.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0)
        )

        self.client = AsyncOpenAI(
            api_key=cfg.openai_api_key,
            base_url=cfg.openai_api_base,
            http_client=self.http_client
        )

    async def stream_chat(
        self,
        messages: Sequence[dict[str, str]],
        max_tokens: int = 800,
        temperature: float = 0.7
    ) -> AsyncGenerator[str, None]:
        """
        Потоковое получение текста от модели GPT.
        Использует современный метод create(stream=True).
        """

        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,             # ← ВАЖНО!
        )

        async for chunk in stream:
            # chunk.choices[0].delta.content — текстовая часть стрима
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


class OpenAIImageProvider:
    """Провайдер изображений через DALL-E с кастомным httpx-клиентом."""

    def __init__(self, model: str = "dall-e-3"):
        self.model = model
        self.http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
        self.client = AsyncOpenAI(
            api_key=cfg.openai_api_key,
            base_url=cfg.openai_api_base,
            http_client=self.http_client,
        )
        self.clientSync = OpenAI()

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

    async def edit_image(self, image_bytes: bytes, instruction: str) -> bytes:
        import base64
        prompt = f"Отредактируй изображение согласно инструкции: {instruction}"
        try:
            response = self.clientSync.images.edit(
                model="gpt-image-1",
                image=image_bytes,
                mask=None,  # type: ignore[arg-type]
                prompt=prompt,
                size="auto",
                n=1,
            )

            for item in response.data:
                if getattr(item, "b64_json", None):
                    b64 = "data:image/png;base64," + item.b64_json  # type: ignore[attr-defined]
                    return base64.b64decode(b64)
                else:
                    raise RuntimeError("No image URLs returned from API.")

            return base64.b64decode("")
        except Exception as e:
            print(f"OpenAI API Error: {e}")
            raise

    async def add_people(self, image_bytes: bytes, description: str) -> bytes:
        prompt = f"На основе исходного фото, добавь людей: {description}. Сохрани стиль и реалистичность."
        return await self.generate(prompt)

    async def celebrity_selfie(self, image_bytes: bytes, celebrity_name: str, style: str | None = None) -> bytes:
        # Для DALL-E нам нужно создать новый промпт, так как он не принимает исходное изображение
        prompt = f"Реалистичное селфи двух людей. Один выглядит как {celebrity_name}, второй - обычный человек. {style or ''} Фотография должна выглядеть как настоящее селфи, естественное освещение, высокое качество."
        return await self.generate(prompt)