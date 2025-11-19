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
        b64 = base64.b64encode(image_bytes).decode()
        data_url = f"data:image/jpeg;base64,{b64}"

        resp = await self.client.responses.create(
            model="gpt-4.1",
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": instruction},
                        {"type": "input_image", "image_url": data_url},
                    ],
                }
            ]
        )

        for msg in resp.output:
            if msg["type"] == "message":
                for c in msg["content"]:
                    if c["type"] == "output_image":
                        return base64.b64decode(c["image"]["data"]), None

        return None, "API не вернул изображение"

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
