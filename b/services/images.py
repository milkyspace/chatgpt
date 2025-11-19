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

    async def analyze(
            self,
            image_bytes: bytes,
            question: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Анализ изображения с задаванием вопроса.

        Args:
            image_bytes: Изображение для анализа
            question: Вопрос об изображении

        Returns:
            Кортеж (текстовый ответ, ошибка)
        """
        try:
            analysis = await self.provider.analyze_image(image_bytes, question)
            return analysis, None
        except Exception as e:
            return None, f"Ошибка анализа изображения: {str(e)}"

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

    async def add_people(
            self,
            image_bytes: bytes,
            description: str
    ) -> Tuple[Optional[bytes], Optional[str]]:
        """
        Добавление людей на изображение.

        Args:
            image_bytes: Исходное изображение
            description: Описание добавляемых людей

        Returns:
            Кортеж (байты изображения, ошибка)
        """
        try:
            img_bytes = await self.provider.add_people(image_bytes, description)
            return img_bytes, None
        except Exception as e:
            return None, f"Ошибка добавления людей: {str(e)}"