# services/images.py

import base64
from openai import AsyncOpenAI
from .providers import ProviderService


class ImageService:
    """
    Отвечает только за работу с изображениями.
    Не знает о Telegram или БД.
    """

    def __init__(self):
        self.client = AsyncOpenAI()

    async def edit(self, image_bytes: bytes, instruction: str):
        try:
            resp = await self.client.images.edits(
                model="gpt-image-1",
                prompt=instruction,
                image=image_bytes,
                size="1024x1024",
                response_format="b64_json",
            )

            return base64.b64decode(resp.data[0].b64_json), None

        except Exception as e:
            return None, f"OpenAI editing error: {e}"

    async def generate(self, prompt: str, provider="openai"):
        """Генерация нового изображения через Images API"""
        try:
            resp = await ProviderService.images_async(
                provider,
                model="gpt-image-1",
                prompt=prompt,
                size="1024x1024",
                response_format="b64_json",
            )

            return base64.b64decode(resp.data[0].b64_json), None

        except Exception as e:
            return None, f"Provider Image Generation Error: {e}"
