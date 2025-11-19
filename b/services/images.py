# services/images.py

import base64
from openai import AsyncOpenAI
from PIL import Image
import io


class ImageService:
    def __init__(self):
        self.client = AsyncOpenAI()

    # ------------------------------------------------
    # Генерация полной маски (если пользователь маску не дал)
    # ------------------------------------------------
    def _create_full_mask(self, image_bytes: bytes) -> bytes:
        """
        GPT-Image-1 edit требует mask.
        Если маска не предоставлена — создаём белую маску
        (весь кадр можно редактировать).
        """
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        w, h = img.size

        # полностью белая маска (255 = всё редактируем)
        mask = Image.new("L", (w, h), 255)

        buf = io.BytesIO()
        mask.save(buf, format="PNG")
        return buf.getvalue()

    # ------------------------------------------------
    # 1. EDIT via GPT-IMAGE-1
    # ------------------------------------------------
    async def edit(self, image_bytes: bytes, instruction: str):
        """
        Правка изображения с помощью GPT-Image-1.
        Требует: image + mask + prompt.
        """

        mask_bytes = self._create_full_mask(image_bytes)

        try:
            resp = await self.client.images.edit(
                model="gpt-image-1",
                image=image_bytes,
                mask=mask_bytes,
                prompt=instruction,
                size="1024x1024",
                n=1,
                response_format="b64_json",
            )

            b64 = resp.data[0].b64_json
            img_bytes = base64.b64decode(b64)
            return img_bytes, None

        except Exception as e:
            return None, f"Image editing error: {e}"

    # ------------------------------------------------
    # 2. GENERATE via GPT-IMAGE-1
    # ------------------------------------------------
    async def generate(self, prompt: str):
        """
        Генерация нового изображения по тексту.
        GPT-Image-1 (статья).
        """
        try:
            resp = await self.client.images.generate(
                model="gpt-image-1",
                prompt=prompt,
                size="1024x1024",
                n=1,
                response_format="b64_json",
            )

            img_bytes = base64.b64decode(resp.data[0].b64_json)
            return img_bytes, None

        except Exception as e:
            return None, f"Image generation error: {e}"
