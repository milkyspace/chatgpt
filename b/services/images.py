# services/images.py

import base64
from openai import AsyncOpenAI


class ImageService:
    def __init__(self):
        self.client = AsyncOpenAI()

    async def edit(self, image_bytes: bytes, instruction: str):
        try:
            b64 = base64.b64encode(image_bytes).decode()
            data_url = f"data:image/jpeg;base64,{b64}"

            resp = await self.client.responses.create(
                model="gpt-4o",
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

            # парсим output
            for block in resp.output:
                if block.type != "message":
                    continue

                for item in block.content:
                    if item.type == "output_image":
                        return base64.b64decode(item.image.data), None

            return None, "output_image не найден"

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
