from __future__ import annotations
from typing import Sequence, AsyncGenerator
from openai import AsyncOpenAI
from config import cfg
import os
import httpx
import base64
from io import BytesIO
from openai import OpenAI
from PIL import Image # For image manipulation if handling b64_json

# Recommended: Use response_format='b64_json' for direct handling
# Helper function (optional) to process b64_json data:
def process_b64_json(b64_json_data, output_path):
    try:
        image_bytes = base64.b64decode(b64_json_data)
        image = Image.open(BytesIO(image_bytes))
        # Optional: Resize or other processing
        # image = image.resize((512, 512), Image.LANCZOS)
        image.save(output_path)  # Saves in format inferred from extension
        print(f"Image saved to {output_path}")
    except Exception as e:
        print(f"Error processing image: {e}")

# Create output directory
os.makedirs("generated_images", exist_ok=True)
output_dir = "generated_images"

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
    """Провайдер изображений через GPT-Image-1 (4o-based)."""

    def __init__(self, model: str = "gpt-image-1"):
        self.model = model

        # Настройки из статьи — обычный httpx клиент
        self.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=15.0)
        )

        self.client = AsyncOpenAI(
            api_key=cfg.openai_api_key,
            base_url=cfg.openai_api_base,
            http_client=self.http_client,
        )

    # -----------------------------
    # 1. Генерация изображения
    # -----------------------------
    async def generate(self, prompt: str, size: str = "1024x1024") -> bytes:
        """
        Генерация изображения по текстовому описанию (GPT-Image-1).
        """

        try:
            response = await self.client.images.generate(
                model=self.model,
                prompt=prompt,
                size=size,
                n=1,
                response_format="b64_json",
            )

            b64 = response.data[0].b64_json
            return base64.b64decode(b64)

        except Exception as e:
            print(f"[OpenAIImageProvider] Generate error: {e}")
            raise

    # -----------------------------
    # 2. Редактирование (inpainting)
    # -----------------------------
    async def edit(self, image_bytes: bytes, mask_bytes: bytes, prompt: str, size: str = "1024x1024") -> bytes:
        """
        Редактирование изображения через GPT-Image-1.
        Требует mask (область, которую нужно изменить).
        """

        try:
            response = await self.client.images.edit(
                model=self.model,
                image=image_bytes,
                mask=mask_bytes,
                prompt=prompt,
                size=size,
                n=1,
                response_format="b64_json",
            )

            b64 = response.data[0].b64_json
            return base64.b64decode(b64)

        except Exception as e:
            print(f"[OpenAIImageProvider] Edit error: {e}")
            raise

    # -----------------------------
    # 3. "Селфи со знаменитостью"
    # -----------------------------
    async def celebrity_selfie(
        self,
        celebrity_name: str,
        style: str | None = None,
        size: str = "1024x1024"
    ) -> bytes:
        """
        GPT-Image-1 НЕ принимает исходное фото → создаём новый промпт.
        """

        prompt = (
            f"Realistic selfie of two people. One looks like {celebrity_name}, "
            f"the other looks like an ordinary person. Natural lighting, "
            f"high detail, ultra realistic. {style or ''}"
        )

        return await self.generate(prompt, size=size)