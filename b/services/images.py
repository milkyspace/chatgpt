from typing import Optional, Tuple
from providers.aitunnel_provider import AITunnelImageProvider
from services.safety import SafetyGuard, SafetyDecision


class ImageService:
    """
    Сервис для работы с изображениями через AITUNNEL.
    """

    def __init__(self, provider: AITunnelImageProvider = None):
        """
        Инициализация сервиса изображений.

        Args:
            provider: Провайдер изображений (по умолчанию AITunnelImageProvider)
        """
        self.provider = provider or AITunnelImageProvider()

    async def generate(
            self,
            prompt: str,
            aspect_ratio: str = "1:1"
    ) -> Tuple[Optional[bytes], Optional[str]]:
        """
        Генерация изображения по текстовому описанию.

        Args:
            prompt: Текстовое описание
            aspect_ratio: Соотношение сторон

        Returns:
            Кортеж (байты изображения, ошибка)
        """
        try:
            img_bytes = await self.provider.generate(prompt, aspect_ratio)
            return img_bytes, None
        except Exception as e:
            return None, f"Ошибка генерации изображения: {str(e)}"

    async def edit(
            self,
            image_bytes: bytes,
            instruction: str
    ) -> Tuple[Optional[bytes], Optional[str]]:
        """
        Редактирование изображения по инструкции.

        Args:
            image_bytes: Исходное изображение
            instruction: Инструкция для редактирования

        Returns:
            Кортеж (байты изображения, ошибка)
        """
        try:
            img_bytes = await self.provider.edit_image(image_bytes, instruction)
            return img_bytes, None
        except Exception as e:
            return None, f"Ошибка редактирования изображения: {str(e)}"


    async def celebrity_selfie(
            self,
            image_bytes: bytes,
            celebrity_name: str,
            style: Optional[str] = None
    ) -> Tuple[Optional[bytes], Optional[str]]:
        """
        Создание селфи со знаменитостью с проверкой безопасности.

        Args:
            image_bytes: Исходное изображение
            celebrity_name: Имя знаменитости
            style: Стиль изображения

        Returns:
            Кортеж (байты изображения, ошибка)
        """
        # Проверка безопасности
        decision: SafetyDecision = SafetyGuard.check_celebrity_selfie(celebrity_name)
        if not decision.allowed:
            return None, decision.reason or "Операция запрещена политиками безопасности."

        try:
            img_bytes = await self.provider.celebrity_selfie(
                image_bytes, celebrity_name, style
            )
            return img_bytes, None
        except Exception as e:
            return None, f"Ошибка создания селфи: {str(e)}"

    async def creative_edit(
            self,
            image_bytes: bytes,
            style: str,
            instruction: str
    ) -> Tuple[Optional[bytes], Optional[str]]:
        """
        Редактирование изображения в творческой стилистике.
        Args:
            image_bytes: исходное изображение (байты)
            style: код стиля (ghibli, pixar, comic, anime, watercolor)
            instruction: дополнительная инструкция от пользователя
        Returns:
            Tuple[bytes_image, error_str]
        """
        # формируем промпт в зависимости от стиля
        prompt_map = {
            "ghibli": "Let this photo become a dreamy Studio Ghibli-style watercolor illustration.",
            "pixar": "Let this photo become a colorful 3D cartoon character in Pixar animation style.",
            "comic": "Let this photo become a bold comic book-style superhero illustration.",
            "anime": "Let this photo become a cinematic anime‐style character with expressive eyes.",
            "watercolor": "Let this photo become a soft watercolor storybook-style illustration.",
        }
        base_prompt = prompt_map.get(style, "")
        if not base_prompt:
            return None, "Неизвестный стиль творческого редактора."

        full_prompt = f"{base_prompt} {instruction}"

        try:
            img_bytes = await self.provider.edit_image(
                image_bytes=image_bytes,
                instruction=full_prompt
            )
            return img_bytes, None
        except Exception as e:
            return None, f"Ошибка творческого редактирования: {str(e)}"