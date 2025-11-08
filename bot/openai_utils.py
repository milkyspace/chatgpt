from typing import Optional

from openai import AsyncOpenAI
import base64
from io import BytesIO

# Backward‑compatible OpenAI utils
# Новая реализация + старые методы-обёртки, чтобы не ломать бота

class OpenAIUtils:
    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(api_key=api_key)

    # ---- Новый универсальный метод ----
    async def generate_image(self, prompt: str, image: Optional[BytesIO] = None, size: str = "1024x1024") -> str:
        """
        Генерация реалистичных изображений (селфи, сцены, фотореал).
        При необходимости учитывает изображение пользователя.
        Полностью заменяет create_variation и edit_image.
        """
        image_b64 = None
        if image:
            image_b64 = base64.b64encode(image.read()).decode()

        # Используем gpt-image-1 (аналог качества ChatGPT)
        response = await self.client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size=size,
            image=image_b64,
            quality="high",
            n=1,
        )
        return response.data[0].url

    # ---- Старые методы (оставлены для совместимости) ----

    async def create_variation(self, image: BytesIO, size: str = "1024x1024") -> str:
        """
        Backward-compatible метод.
        Раньше создавал вариации через DALL·E 2.
        Теперь вызывает новый generate_image.
        """
        return await self.generate_image(
            prompt="Создай улучшенную реалистичную вариацию этого изображения",
            image=image,
            size=size,
        )

    async def edit_image(self, image: BytesIO, prompt: str, size: str = "1024x1024") -> str:
        """
        Backward-compatible метод.
        Раньше применял edit через DALL·E 2.
        Теперь просто генерирует новое изображение по промпту.
        """
        return await self.generate_image(
            prompt=prompt,
            image=image,
            size=size,
        )

__all__ = ["OpenAIUtils"]
