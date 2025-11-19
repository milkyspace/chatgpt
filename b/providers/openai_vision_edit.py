from __future__ import annotations
import asyncio
import base64
from openai import AsyncOpenAI
from config import cfg


class OpenAIVisionEditProvider:
    def __init__(self, model: str = "gpt-4.1"):
        self.client = AsyncOpenAI(api_key=cfg.openai_api_key)
        self.model = model

    async def edit_image(self, image_bytes: bytes, instruction: str) -> bytes:
        # Base64 encode image for direct Vision API input
        img_b64 = base64.b64encode(image_bytes).decode()

        # Create thread
        thread = await self.client.threads.create()

        # Send message with embedded image (NOT via /files)
        await self.client.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=[
                {
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{img_b64}"
                },
                {
                    "type": "text",
                    "text": instruction
                }
            ]
        )

        # Run model
        run = await self.client.threads.runs.create(
            thread_id=thread.id,
            model=self.model
        )

        # Wait for completion
        while True:
            status = await self.client.threads.runs.retrieve(
                thread_id=thread.id, run_id=run.id
            )
            if status.status in ("completed", "failed"):
                break
            await asyncio.sleep(0.3)

        if status.status == "failed":
            raise RuntimeError("Vision editing failed")

        # Extract output image
        messages = await self.client.threads.messages.list(thread_id=thread.id)

        for msg in messages.data:
            for part in msg.content:
                if part.type == "output_image":
                    return base64.b64decode(part.data)

        raise RuntimeError("No edited image returned")
