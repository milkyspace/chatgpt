import base64
from .providers import ProviderService
from openai import OpenAI

client = OpenAI()

class ImageService:

    async def edit(self, image_bytes: bytes, instruction: str):
        """
        Vision Editing via /responses — OpenAI >=1.59
        """

        try:
            b64_image = base64.b64encode(image_bytes).decode("utf-8")
            data_url = f"data:image/jpeg;base64,{b64_image}"

            resp = client.responses.create(
                model="gpt-4.1",
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": instruction
                            },
                            {
                                "type": "input_image",
                                "image_url": data_url
                            }
                        ]
                    }
                ]
            )

            # ищем output_image
            for item in resp.output:
                if item["type"] == "output_image":
                    out_b64 = item["image"]["data"]
                    return base64.b64decode(out_b64), None

            return None, "API не вернул изображение"

        except Exception as e:
            return None, f"OpenAI editing error: {e}"


    async def generate(self, prompt: str, provider="openai"):
        """
        Генерация нового изображения — через Images API.
        """
        try:
            resp = ProviderService.images(
                provider=provider,
                model="gpt-image-1",
                prompt=prompt,
                size="1024x1024",
                response_format="b64_json"
            )

            return base64.b64decode(resp.data[0].b64_json), None

        except Exception as e:
            return None, f"Provider Image Generation Error: {e}"