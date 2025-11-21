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
            "ghibli": (
                "Transform this photo into a magical Studio Ghibli–inspired illustration: "
                "soft pastel colors, gentle cel-shading, warm lighting, expressive anime-style eyes, "
                "hand-painted watercolor textures, dreamy atmosphere, delicate linework. "
                "Keep the original person’s identity and main composition."
            ),

            "pixar": (
                "Turn this photo into a high-quality Pixar-style 3D character render: "
                "smooth stylized skin, big expressive eyes, realistic soft shadows, "
                "subsurface scattering, cinematic lighting, vibrant colors, "
                "3D cartoon proportions while preserving the original likeness and pose."
            ),

            "comic": (
                "Convert this photo into a dynamic comic-book illustration: "
                "bold ink outlines, halftone shading, dramatic highlights, bright contrasting colors, "
                "heroic aesthetics, expressive contour lines, stylized shadows. "
                "Maintain the person’s identity and facial features."
            ),

            "anime": (
                "Transform this photo into a polished anime-style portrait: "
                "clean line art, vivid colors, glossy eyes, subtle soft shading, "
                "smooth gradients, sharp highlights, refined facial proportions. "
                "Preserve the original appearance while applying high-quality anime rendering."
            ),

            "watercolor": (
                "Reimagine this photo as a gentle watercolor storybook illustration: "
                "soft flowing pigment, textured paper effect, subtle outlines, "
                "warm natural tones, hand-painted brush strokes, light dreamy atmosphere. "
                "Keep the character’s recognizable features and overall pose."
            ),
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