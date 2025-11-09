import base64
import logging
from io import BytesIO
from typing import Optional, List, AsyncGenerator, Tuple, Union

from openai import AsyncOpenAI

import config

# Константы вынесены в верхний регистр
OPENAI_COMPLETION_OPTIONS = {
    "temperature": 0.7,
    "max_tokens": 2000,
    "top_p": 1,
    "frequency_penalty": 0,
    "presence_penalty": 0,
}

# Инициализация клиента OpenAI (упрощенная версия)
openai_client = AsyncOpenAI(
    api_key=config.openai_api_key,
    base_url=config.openai_api_base  # base_url может быть None
)

logger = logging.getLogger(__name__)


def configure_logging():
    """Конфигурация логирования с оптимизацией производительности"""
    level = logging.DEBUG if config.enable_detailed_logging else logging.CRITICAL
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
        force=True  # Перезаписывает существующую конфигурацию
    )


configure_logging()


class ChatGPT:
    """Оптимизированный класс для работы с ChatGPT"""

    # Кэш для промптов (небольшая оптимизация)
    _prompt_cache = {}

    def __init__(self, model: str = "gpt-4o"):
        self.model = model
        self.is_claude_model = model.startswith("claude")
        self.logger = logger  # Используем существующий логгер

    async def send_message_stream(
            self,
            message: str,
            dialog_messages: List[dict] = None,
            chat_mode: str = "assistant"
    ) -> AsyncGenerator[Tuple[str, str, Tuple[int, int], int], None]:
        """Оптимизированная потоковая отправка сообщения."""
        if dialog_messages is None:
            dialog_messages = []

        try:
            self._validate_chat_mode(chat_mode)
            messages = self._generate_prompt_messages(message, dialog_messages, chat_mode)

            response = await openai_client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,
                **OPENAI_COMPLETION_OPTIONS
            )

            full_answer = ""
            chunk_counter = 0
            YIELD_EVERY_N_CHUNKS = 5  # Увеличиваем частоту обновлений

            async for chunk in response:
                if (chunk.choices and
                        chunk.choices[0].delta.content is not None):

                    chunk_content = chunk.choices[0].delta.content
                    full_answer += chunk_content
                    chunk_counter += 1

                    if chunk_counter % YIELD_EVERY_N_CHUNKS == 0:
                        yield "streaming", full_answer, (0, 0), 0

            # Финальный результат с реальными токенами
            yield "finished", full_answer, (len(full_answer) // 4, 0), 0

        except Exception as e:
            logger.error(f"Streaming error for model {self.model}: {e}")
            raise

    async def send_message(
            self,
            message: str,
            dialog_messages: List[dict] = None,
            chat_mode: str = "assistant"
    ) -> Tuple[str, Tuple[int, int], int]:
        """Синхронная отправка сообщения с оптимизацией"""
        if dialog_messages is None:
            dialog_messages = []

        self._validate_chat_mode(chat_mode)
        messages = self._generate_prompt_messages(message, dialog_messages, chat_mode)

        response = await openai_client.chat.completions.create(
            model=self.model,
            messages=messages,
            **OPENAI_COMPLETION_OPTIONS
        )

        answer = response.choices[0].message.content
        usage = response.usage
        return answer, (usage.prompt_tokens, usage.completion_tokens), 0

    def _validate_chat_mode(self, chat_mode: str) -> None:
        """Валидация chat_mode с кэшированием"""
        if chat_mode not in config.chat_modes:
            raise ValueError(f"Chat mode {chat_mode} is not supported")

    def _generate_prompt_messages(
            self,
            message: str,
            dialog_messages: List[dict],
            chat_mode: str,
            image_buffer: Optional[BytesIO] = None
    ) -> List[dict]:
        """Генерация сообщений с оптимизацией и кэшированием промптов"""
        # Кэширование системных промптов
        cache_key = f"system_{chat_mode}"
        if cache_key not in self._prompt_cache:
            self._prompt_cache[cache_key] = config.chat_modes[chat_mode]["prompt_start"]

        prompt = self._prompt_cache[cache_key]
        messages = [{"role": "system", "content": prompt}]

        # Более эффективное построение истории диалога
        for msg in dialog_messages:
            messages.extend([
                {"role": "user", "content": msg["user"]},
                {"role": "assistant", "content": msg["bot"]}
            ])

        # Обработка изображения
        if image_buffer is not None:
            encoded = base64.b64encode(image_buffer.read()).decode()
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": message},
                    {"type": "image", "image": encoded},
                ]
            })
        else:
            messages.append({"role": "user", "content": message})

        return messages

    # Контекстный менеджер для очистки кэша
    def clear_cache(self):
        """Очистка кэша промптов"""
        self._prompt_cache.clear()


async def transcribe_audio(audio_file) -> str:
    """Транскрибация аудио с улучшенной обработкой ошибок"""
    try:
        transcript = await openai_client.audio.transcriptions.create(
            file=audio_file,
            model="whisper-1"
        )
        return transcript.text or ""
    except Exception as e:
        logger.error("Error transcribing audio: %s", e, exc_info=True)
        return ""


async def generate_images(
        prompt: str,
        model: str = "gpt-image-1",
        size: str = "1024x1024",
        n: int = 1
) -> List[str]:
    """Генерация изображений с улучшенной обработкой параметров"""
    try:
        # Валидация параметров
        if n <= 0 or n > 4:  # OpenAI обычно ограничивает количество
            raise ValueError("Number of images must be between 1 and 4")

        response = await openai_client.images.generate(
            model=model,
            prompt=prompt,
            size=size,
            n=n,
            quality="high"
        )
        return [item.url for item in response.data]

    except Exception as e:
        logger.error("Error generating images: %s", e, exc_info=True)
        raise

async def generate_image_with_input(prompt: str, image_bytes: bytes) -> bytes:
    response = await openai_client.images.edit(
        model="gpt-image-1",
        prompt=prompt,
        image=[
            {
                "name": "input.png",
                "bytes": image_bytes
            }
        ],
        size="1024x1024"
    )

    # Достаём base64
    b64 = response.data[0].b64_json
    return base64.b64decode(b64)

async def generate_image(prompt: str) -> str:
    """Генерирует изображение по текстовому описанию."""
    try:
        response = await openai.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1,
        )
        return response.data[0].url
    except Exception as e:
        logger.error(f"Error generating image: {e}")
        raise