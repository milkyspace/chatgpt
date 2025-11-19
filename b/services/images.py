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
        """
        Image Editing через /responses + gpt-image-1
        """
        try:
            b64_image = base64.b64encode(image_bytes).decode()
            data_url = f"data:image/jpeg;base64,{b64_image}"

            resp = await self.client.responses.create(
                model="gpt-image-1",  # <-- правильная модель
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": instruction},
                            {"type": "input_image", "image_url": data_url},
                        ],
                    }
                ],
            )

            # ---- парсинг ----
            for block in resp.output:
                if block.type != "message":
                    continue

                for item in block.content:
                    if item.type == "output_image":
                        return base64.b64decode(item.image.data), None

            return None, "output_image не найден"

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
