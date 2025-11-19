from __future__ import annotations
from typing import Sequence, AsyncGenerator, Optional, List, Dict, Any
from openai import AsyncOpenAI
import httpx
import base64
import logging
from typing import Any, Optional
from config import cfg

logger = logging.getLogger(__name__)

def extract_image_from_ai_tunnel(message: Any) -> Optional[bytes]:
    """
    Универсальный извлекатель изображения из AITunnel ChatCompletionMessage.

    Поддерживает два формата:
    1) message.content[...]  (OpenAI style)
    2) message.images[...]   (AITunnel style)
    """

    if not message:
        return None

    # ======= 1) AITunnel уникальный формат: message.images =======
    images = getattr(message, "images", None)
    if isinstance(images, list) and images:
        img = images[0]

        # Формат: {"type": "...", "image_url": {"url": "data:image/..."}}
        if isinstance(img, dict):
            iu = img.get("image_url")
            if isinstance(iu, dict):
                url = iu.get("url")
                if isinstance(url, str) and url.startswith("data:image"):
                    return base64.b64decode(url.split(",", 1)[1])

    # ======= 2) OpenAI формат: message.content =======
    content = getattr(message, "content", None)

    # content == string?
    if isinstance(content, str) and content.startswith("data:image"):
        return base64.b64decode(content.split(",", 1)[1])

    # content == list?
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue

            # {"image_url": {"url": "..."}}
            if "image_url" in part and isinstance(part["image_url"], dict):
                url = part["image_url"].get("url")
                if isinstance(url, str) and url.startswith("data:image"):
                    return base64.b64decode(url.split(",", 1)[1])

            # {"url": "data:image..."}
            if "url" in part and isinstance(part["url"], str) and part["url"].startswith("data:image"):
                return base64.b64decode(part["url"].split(",", 1)[1])

            # {"data": "<base64>"}
            if "data" in part and isinstance(part["data"], str):
                try:
                    return base64.b64decode(part["data"])
                except Exception:
                    pass

    return None

class AITunnelChatProvider:
    """
    Провайдер для чата через AITUNNEL API.
    Поддерживает потоковую передачу, инструменты и мультимодальные запросы.
    """

    def __init__(self, model: str = None):
        self.model = model or cfg.chat_model

        # Создаем кастомный HTTP клиент с увеличенными таймаутами
        self.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0)
        )

        # Инициализируем клиент AITUNNEL
        self.client = AsyncOpenAI(
            api_key=cfg.aitunnel_api_key,
            base_url=cfg.aitunnel_api_base,
            http_client=self.http_client
        )

    async def stream_chat(
            self,
            messages: Sequence[Dict[str, Any]],
            max_tokens: int = 800,
            temperature: float = 0.7,
            tools: Optional[List[Dict]] = None
    ) -> AsyncGenerator[str, None]:
        """
        Потоковое получение текста от модели через AITUNNEL.

        Args:
            messages: История сообщений
            max_tokens: Максимальное количество токенов
            temperature: Температура генерации
            tools: Список инструментов для вызова

        Yields:
            Текстовые фрагменты ответа
        """
        try:
            # Подготавливаем параметры запроса
            request_params = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True
            }

            # Добавляем инструменты, если они предоставлены
            if tools:
                request_params["tools"] = tools

            # Выполняем потоковый запрос
            stream = await self.client.chat.completions.create(**request_params)

            # Обрабатываем потоковые chunk'и
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

        except Exception as e:
            print(f"AITUNNEL Chat API Error: {e}")
            raise

    async def chat_with_tools(
            self,
            messages: Sequence[Dict[str, Any]],
            tools: List[Dict],
            max_tokens: int = 800,
            temperature: float = 0.7
    ) -> Dict[str, Any]:
        """
        Чат с поддержкой вызова инструментов.

        Args:
            messages: История сообщений
            tools: Список доступных инструментов
            max_tokens: Максимальное количество токенов
            temperature: Температура генерации

        Returns:
            Словарь с результатом выполнения
        """
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens
            )

            return {
                "content": response.choices[0].message.content,
                "tool_calls": getattr(response.choices[0].message, 'tool_calls', None),
                "usage": getattr(response, 'usage', {})
            }
        except Exception as e:
            print(f"AITUNNEL Tools API Error: {e}")
            raise


class AITunnelImageProvider:
    """
    Провайдер для работы с изображениями через AITUNNEL.
    Поддерживает генерацию, редактирование и анализ изображений.
    """

    def __init__(self, model: str = None):
        self.model = model or cfg.image_model

        # Создаем кастомный HTTP клиент
        self.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0)
        )

        # Инициализируем клиент AITUNNEL
        self.client = AsyncOpenAI(
            api_key=cfg.aitunnel_api_key,
            base_url=cfg.aitunnel_api_base,
            http_client=self.http_client
        )

    async def generate(self, prompt: str, aspect_ratio: str = "1:1") -> bytes:
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                modalities=["image", "text"]
            )

            msg = response.choices[0].message

            img_bytes = extract_image_from_ai_tunnel(msg)
            if img_bytes:
                return img_bytes

            raise RuntimeError("Модель не вернула изображение")

        except Exception as e:
            logger.error(f"Ошибка обработки generate: {e}")
            raise

    async def edit_image(self, image_bytes: bytes, instruction: str) -> bytes:
        """
        Редактирование изображения по текстовой инструкции.
        """
        try:
            self.model = cfg.edit_model

            response = await self.client.images.edit(
                model=self.model,
                image=("image.png", image_bytes, "image/png"),
                prompt=instruction,
                timeout=30
            )

            msg = response.choices[0].message
            img_bytes = extract_image_from_ai_tunnel(msg)

            if img_bytes:
                return img_bytes

            raise RuntimeError("Модель не вернула отредактированное изображение")

        except Exception as e:
            logger.error(f"Ошибка обработки edit_image: {e}")
            raise

    async def celebrity_selfie(self, image_bytes: bytes, celebrity_name: str, style: str = None) -> bytes:
        """
        Создание селфи со знаменитостью.

        Args:
            image_bytes: Байты исходного изображения
            celebrity_name: Имя знаменитости
            style: Стиль изображения

        Returns:
            Байты нового изображения
        """
        instruction = (
            f"Создай реалистичное селфи двух людей. "
            f"Один - {celebrity_name}, второй - человек с фото. "
            f"{style or ''} Фотография должна выглядеть как настоящее селфи, "
            f"естественное освещение, высокое качество."
        )

        logger.debug("instruction: %s", instruction)
        return await self.edit_image(image_bytes, instruction)