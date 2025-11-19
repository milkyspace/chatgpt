import base64
from openai import AsyncOpenAI

class OpenAIVisionEditProvider:

    def __init__(self, model="gpt-4.1"):
        self.client = AsyncOpenAI()
        self.model = model

    async def edit_image(self, image_bytes: bytes, instruction: str) -> bytes:
        # конвертируем в data-url
        img_b64 = base64.b64encode(image_bytes).decode()
        data_url = f"data:image/jpeg;base64,{img_b64}"

        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": data_url},
                        {"type": "text", "text": instruction}
                    ]
                }
            ]
        )

        text = resp.choices[0].message.content

        # Ищем data:image/...;base64,BLABLA
        import re
        match = re.search(r"data:image\/[^;]+;base64,([A-Za-z0-9+/=]+)", text)
        if not match:
            raise RuntimeError("Модель не вернула редактированное изображение")

        return base64.b64decode(match.group(1))
