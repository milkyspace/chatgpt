from openai import AsyncOpenAI
import base64
import asyncio

class OpenAIVisionEditProvider:
    def __init__(self, model="gpt-4.1"):
        self.client = AsyncOpenAI()
        self.model = model

    async def edit_image(self, image_bytes: bytes, instruction: str) -> bytes:
        img_b64 = base64.b64encode(image_bytes).decode()

        response = await self.client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": f"data:image/jpeg;base64,{img_b64}"
                        },
                        {
                            "type": "text",
                            "text": instruction
                        }
                    ]
                }
            ]
        )

        # Извлекаем изображение
        for out in response.output:
            if out.type == "output_image":
                return base64.b64decode(out.data)

        raise RuntimeError("Vision: модель не вернула изображение")
