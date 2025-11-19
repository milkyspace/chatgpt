from __future__ import annotations
from typing import Sequence, AsyncGenerator, Optional, List, Dict, Any
from openai import AsyncOpenAI
import httpx
import base64
import logging
from typing import Any, Optional
from config import cfg

logger = logging.getLogger(__name__)

def extract_image_from_message(message: Any) -> Optional[bytes]:
    """
    Универсальное извлечение изображения из ChatCompletionMessage.
    Работает с любым форматом AITunnel / OpenAI multimodal.
    """

    logger.debug("message: %s", message)

    if message is None:
        return None

    content = getattr(message, "content", None)
    logger.debug("content: %s", content)


    # --- Вариант 1: модель вернула строку: "data:image/png;base64,AAAA..."
    if isinstance(content, str) and content.startswith("data:image"):
        return base64.b64decode(content.split(",", 1)[1])

    # --- Вариант 2: content = [{"type": "...", ...}, ...]
    if isinstance(content, list):
        for part in content:

            # 2.1 {"type": "output_image", "image_url": {"url": "data:image/..."}}
            if isinstance(part, dict):
                img_url = (
                    part.get("image_url", {}) or
                    part.get("url") or
                    None
                )

                # {"image_url": {"url": "..."}}
                if isinstance(img_url, dict) and "url" in img_url:
                    url = img_url["url"]
                    if url.startswith("data:image"):
                        return base64.b64decode(url.split(",", 1)[1])

                # {"url": "data:image/png;base64,..."}
                if isinstance(img_url, str) and img_url.startswith("data:image"):
                    return base64.b64decode(img_url.split(",", 1)[1])

                # {"data": "<base64>"}
                if "data" in part:
                    try:
                        return base64.b64decode(part["data"])
                    except Exception:
                        pass

    # Ничего не нашли
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
        """
        Генерация изображения (универсальный формат ответа).
        """
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                modalities=["image", "text"]
            )

            msg = response.choices[0].message
            img_bytes = extract_image_from_message(msg)

            if img_bytes:
                return img_bytes

            raise RuntimeError(
                "Модель не вернула изображение. Проверь формат ответа AITunnel."
            )

        except Exception as e:
            logger.error(f"Ошибка обработки generate: {e}")
            raise

    async def edit_image(self, image_bytes: bytes, instruction: str) -> bytes:
        """
        Редактирование изображения по текстовой инструкции.
        """
        try:
            base64_image = base64.b64encode(image_bytes).decode("utf-8")
            img_url = f"data:image/jpeg;base64,{base64_image}"

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {"type": "image_url", "image_url": {"url": img_url}}
                    ]
                }
            ]

            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                modalities=["image", "text"]
            )

            msg = response.choices[0].message
            img_bytes = extract_image_from_message(msg)

            if img_bytes:
                return img_bytes

            raise RuntimeError("Модель не вернула отредактированное изображение")

        except Exception as e:
            logger.error(f"Ошибка обработки edit_image: {e}")
            raise

    async def analyze_image(self, image_bytes: bytes, question: str) -> str:
        """
        Анализ изображения возвращает ТОЛЬКО текст.
        """
        try:
            base64_image = base64.b64encode(image_bytes).decode("utf-8")
            img_url = f"data:image/jpeg;base64,{base64_image}"

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": img_url}}
                    ]
                }
            ]

            response = await self.client.chat.completions.create(
                model=cfg.chat_model,
                messages=messages
            )

            return response.choices[0].message.content

        except Exception as e:
            logger.error(f"Ошибка анализа изображения: {e}")
            raise

    async def add_people(self, image_bytes: bytes, description: str) -> bytes:
        """
        Добавление людей на изображение.

        Args:
            image_bytes: Байты исходного изображения
            description: Описание добавляемых людей

        Returns:
            Байты нового изображения
        """
        prompt = f"На основе исходного фото, добавь людей: {description}. Сохрани стиль и реалистичность."
        return await self.edit_image(image_bytes, prompt)

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
            f"Один выглядит как {celebrity_name}, второй - обычный человек. "
            f"{style or ''} Фотография должна выглядеть как настоящее селфи, "
            f"естественное освещение, высокое качество."
        )
        return await self.edit_image(image_bytes, instruction)