from __future__ import annotations
import base64
from openai import AsyncOpenAI
from config import cfg
import httpx


class OpenAIVisionEditProvider:
    """
    Provider для настоящего редактирования изображений через Assistants API.
    Использует модель gpt-4.1 (или gpt-4o) для vision editing.
    """

    def __init__(self, model: str = "gpt-4.1"):
        self.model = model
        self.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0)
        )
        self.client = AsyncOpenAI(
            api_key=cfg.openai_api_key,
            base_url=cfg.openai_api_base,
            http_client=self.http_client,
        )

    async def edit_image(self, image_bytes: bytes, instruction: str) -> bytes:
        """
        Редактирует переданное изображение согласно инструкции.
        Возвращает байты отредактированного изображения.
        """

        # 1) Загружаем изображение как file в ассистента
        file = await self.client.files.create(
            file=image_bytes,
            purpose="input"
        )

        # 2) Создаём thread
        thread = await self.client.threads.create()

        # 3) Добавляем в thread изображение + инструкцию
        await self.client.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=[
                {
                    "type": "input_image",
                    "image_file_id": file.id
                },
                {
                    "type": "text",
                    "text": instruction
                }
            ]
        )

        # 4) Запускаем ассистента
        run = await self.client.threads.runs.create(
            thread_id=thread.id,
            model=self.model
        )

        # 5) Ждём завершения
        while True:
            run_status = await self.client.threads.runs.retrieve(
                thread_id=thread.id, run_id=run.id
            )
            if run_status.status in ("completed", "failed"):
                break
            await asyncio.sleep(0.4)

        if run_status.status == "failed":
            raise RuntimeError("OpenAI failed to edit image")

        # 6) Получаем результат
        messages = await self.client.threads.messages.list(thread_id=thread.id)

        for message in messages.data:
            for item in message.content:
                if item.type == "output_image":
                    b64 = item.data
                    return base64.b64decode(b64)

        raise RuntimeError("No output_image returned by OpenAI")
