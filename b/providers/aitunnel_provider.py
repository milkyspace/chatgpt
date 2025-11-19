from __future__ import annotations
from typing import Sequence, AsyncGenerator, Optional, List, Dict, Any
from openai import AsyncOpenAI
import httpx
import base64
import logging
import json
from typing import Any, Optional
from config import cfg

logger = logging.getLogger(__name__)

def extract_image_from_choices(choices: list[Any]) -> Optional[bytes]:
    """
    Универсальный парсер изображений из ответа AITunnel.

    Args:
        choices: response["choices"]

    Returns:
        Байты изображения или None
    """
    if not choices:
        return None

    msg = choices[0].get("message")
    if not msg:
        return None

    images = msg.get("images")
    if not images:
        return None

    img = images[0]

    # 1) image_url: {"url": "data:image/..."} (как у OpenAI)
    if isinstance(img, dict):
        # Вариант: {"image_url": {"url": "..."}}
        if "image_url" in img and isinstance(img["image_url"], dict):
            url = img["image_url"].get("url")
            if url and url.startswith("data:image"):
                return base64.b64decode(url.split(",", 1)[1])

        # Вариант: {"url": "data:image/..."}
        if "url" in img:
            url = img["url"]
            if url and url.startswith("data:image"):
                return base64.b64decode(url.split(",", 1)[1])

        # Вариант: {"data": "<base64>"}
        if "data" in img:
            try:
                return base64.b64decode(img["data"])
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
        """
        Генерация изображения через AITunnel с универсальным разбором ответа.
        """
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                modalities=["image", "text"]
            )

            # !!! AITunnel возвращает обычный dict
            data = response  # уже dict

            img_bytes = extract_image_from_choices(data.get("choices", []))
            if img_bytes:
                return img_bytes

            raise RuntimeError("AITunnel не вернул изображение")

        except Exception as e:
            logger.error(f"Ошибка обработки generate: {e}")
            raise

    async def edit_image(self, image_bytes: bytes, instruction: str) -> bytes:
        """
        Редактирование изображения через AITunnel.
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

            data = response
            img_bytes = extract_image_from_choices(data.get("choices", []))
            if img_bytes:
                return img_bytes

            raise RuntimeError("AITunnel не вернул отредактированное изображение")

        except Exception as e:
            logger.error(f"Ошибка обработки edit_image: {e}")
            raise

    async def analyze_image(self, image_bytes: bytes, question: str) -> str:
        """
        Анализ изображения с задаванием вопросов.

        Args:
            image_bytes: Байты изображения для анализа
            question: Вопрос об изображении

        Returns:
            Текстовый ответ модели
        """
        try:
            # Кодируем изображение в base64
            base64_image = base64.b64encode(image_bytes).decode('utf-8')
            data_url = f"data:image/jpeg;base64,{base64_image}"

            # Создаем мультимодальное сообщение
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "high"}
                        }
                    ]
                }
            ]

            response = await self.client.chat.completions.create(
                model=cfg.chat_model,  # Используем чатовую модель для анализа
                messages=messages
            )

            return response.choices[0].message.content

        except Exception as e:
            print(f"AITUNNEL Image Analysis Error: {e}")
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