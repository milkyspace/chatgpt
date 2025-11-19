# services/images.py

import base64
from openai import AsyncOpenAI


class ImageService:
    def __init__(self):
        self.client = AsyncOpenAI()

    async def edit(self, image_bytes: bytes, instruction: str):
        """
        Редактирование картинки через images.generate (универсальный метод).
        Если передаём image=..., то это EDIT, а не генерация.
        """
        try:
            resp = await self.client.images.generate(
                model="gpt-image-1",
                prompt=instruction,
                image=image_bytes,          # <-- это включает РЕЖИМ EDIT
                size="1024x1024",
                response_format="b64_json"
            )

            return base64.b64decode(resp.data[0].b64_json), None

        except Exception as e:
            return None, f"OpenAI editing error: {e}"

    async def generate(self, prompt: str):
        """
        Генерация нового изображения (без входного image).
        """
        try:
            resp = await self.client.images.generate(
                model="gpt-image-1",
                prompt=prompt,
                size="1024x1024",
                response_format="b64_json"
            )

            return base64.b64decode(resp.data[0].b64_json), None

        except Exception as e:
            return None, f"Image generation error: {e}"
