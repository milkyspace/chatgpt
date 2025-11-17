from __future__ import annotations
from providers.openai_provider import OpenAIImageProvider
from services.safety import SafetyGuard, SafetyDecision


class ImageService:
    """Фасад для изображений. Легко заменить провайдер."""

    def __init__(self, provider: OpenAIImageProvider | None = None):
        self.provider = provider or OpenAIImageProvider()

    async def generate(self, prompt: str) -> tuple[bytes | None, str | None]:
        try:
            img = await self.provider.generate(prompt)
            return img, None
        except Exception as e:
            return None, f"Ошибка генерации: {str(e)}"

    async def edit(self, image_bytes: bytes, instruction: str) -> tuple[bytes | None, str | None]:
        try:
            img = await self.provider.edit(image_bytes, instruction)
            return img, None
        except Exception as e:
            return None, f"Ошибка редактирования: {str(e)}"

    async def add_people(self, image_bytes: bytes, description: str) -> tuple[bytes | None, str | None]:
        try:
            img = await self.provider.add_people(image_bytes, description)
            return img, None
        except Exception as e:
            return None, f"Ошибка добавления людей: {str(e)}"

    async def celebrity_selfie(self, image_bytes: bytes, celebrity_name: str, style: str | None = None) -> tuple[
        bytes | None, str | None]:
        # SafetyGate: блокируем deepfake со знаменитостями
        dec: SafetyDecision = SafetyGuard.check_celebrity_selfie(celebrity_name)
        if not dec.allowed:
            return None, dec.reason or "Операция запрещена политиками."

        try:
            img = await self.provider.celebrity_selfie(image_bytes, celebrity_name, style)
            return img, None
        except Exception as e:
            return None, f"Ошибка создания селфи: {str(e)}"