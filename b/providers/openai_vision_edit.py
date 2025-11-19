from __future__ import annotations
import asyncio
import base64
import io
from openai import AsyncOpenAI
from config import cfg


class OpenAIVisionEditProvider:
    def __init__(self, model: str = "gpt-4.1"):
        self.model = model
        self.client = AsyncOpenAI(api_key=cfg.openai_api_key)

    async def _upload_image(self, image_bytes: bytes):
        buf = io.BytesIO(image_bytes)
        buf.name = "image.jpg"               # обязательно!
        return await self.client.files.create(
            file=buf,
            purpose="input"
        )

    async def edit_image(self, image_bytes: bytes, instruction: str) -> bytes:
        # 1) upload
        image_file = await self._upload_image(image_bytes)

        # 2) thread
        thread = await self.client.threads.create()

        # 3) message
        await self.client.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=[
                {"type": "input_image", "image_file_id": image_file.id},
                {"type": "text", "text": instruction}
            ]
        )

        # 4) run
        run = await self.client.threads.runs.create(
            thread_id=thread.id,
            model=self.model
        )

        # 5) wait
        while True:
            status = await self.client.threads.runs.retrieve(
                thread_id=thread.id,
                run_id=run.id
            )
            if status.status in ("completed", "failed"):
                break
            await asyncio.sleep(0.4)

        if status.status == "failed":
            raise RuntimeError("Vision editing failed")

        # 6) extract output image
        messages = await self.client.threads.messages.list(thread_id=thread.id)

        for msg in messages.data:
            for item in msg.content:
                if item.type == "output_image":
                    return base64.b64decode(item.data)

        raise RuntimeError("No output_image returned")
