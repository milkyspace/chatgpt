from typing import Optional
from providers.openai_vision_edit import OpenAIVisionEditProvider
from services.safety import SafetyGuard, SafetyDecision


class ImageService:

    def __init__(self, provider: OpenAIVisionEditProvider | None = None):
        self.provider = provider or OpenAIVisionEditProvider()

    async def generate(self, prompt: str) -> tuple[Optional[bytes], Optional[str]]:
        try:
            img = await self.provider.generate(prompt)
            return img, None
        except Exception as e:
            return None, f"Ошибка генерации: {str(e)}"

    async def edit(self, image_bytes: bytes, instruction: str):
        try:
            img = await self.provider.edit_image(image_bytes, instruction)
            return img, None
        except Exception as e:
            return None, f"Ошибка редактирования: {str(e)}"

    async def celebrity_selfie(self, image_bytes: bytes, celebrity_name: str, style: Optional[str] = None) -> tuple[
        Optional[bytes], Optional[str]]:
        # SafetyGate: блокируем deepfake со знаменитостями
        dec: SafetyDecision = SafetyGuard.check_celebrity_selfie(celebrity_name)
        if not dec.allowed:
            return None, dec.reason or "Операция запрещена политиками."

        try:
            img = await self.provider.celebrity_selfie(image_bytes, celebrity_name, style)
            return img, None
        except Exception as e:
            return None, f"Ошибка создания селфи: {str(e)}"