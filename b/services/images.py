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
                "Transform the photo into a vivid, detailed Studio Ghibli style illustration: "
                "bold cel-shading, strong outlines, saturated pastel colors, dramatic lighting, "
                "distinct anime facial proportions, expressive large eyes, hand-painted watercolor textures. "
                "Highly stylized look. Keep identity."
            ),

            "pixar": (
                "Transform this photo into a fully stylized Pixar 3D render: "
                "recreate the person, their clothing, and the entire background from scratch "
                "as a cohesive Pixar-style CGI scene. "
                "Replace all real clothing with Pixar-style 3D simplified shapes, fabric, and textures. "
                "Do NOT preserve real-life clothing details — fully restyle the outfit in a cartoon 3D manner. "
                "Also rebuild the background entirely in a Pixar cartoon style: smooth geometry, soft global illumination, "
                "rounded forms, warm ambient light, and a colorful cinematic environment. "
                "Do not keep any real photo elements. "
                "Apply full 3D character shading: glossy stylized eyes, clean subsurface scattering, "
                "soft facial topology, and exaggerated Pixar proportions. "
                "The entire image must look like a frame from a Pixar movie."
            ),

            "comic": (
                "Convert this photo into a fully illustrated comic book panel: "
                "redraw the person, their clothing, and the entire background in a hand-drawn style. "
                "Do NOT preserve real clothing textures — restylize all clothing with bold comic fabric shading, "
                "solid color blocks, and illustrated contour lines. "
                "Rebuild the background entirely as a comic-style environment with no photographic elements. "
                "Use heavy black ink outlines, thick contour strokes, halftone patterns, dramatic shadows, "
                "dynamic highlights, and ultra-stylized action-comic rendering. "
                "The result must look fully illustrated — not a filtered photo. "
                "Maintain the person's identity but stylize all features in comic form."
            ),

            "anime": (
                "Transform this photo into a clean and bold anime portrait: "
                "sharp line art, bright vibrant colors, strong cel shading, "
                "glossy oversized eyes, crisp highlights, defined contours, "
                "refined anime facial proportions. Highly stylized. Preserve identity."
            ),

            "watercolor": (
                "Convert the photo into a detailed storybook watercolor painting: "
                "rich pigment flow, defined brush strokes, textured paper, "
                "controlled bleeding edges, deep shadows, vivid natural tones. "
                "Stronger stylization but keep the original face recognizable."
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
