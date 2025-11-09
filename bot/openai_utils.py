from typing import List

import openai
from openai import OpenAI
import logging
import config

logger = logging.getLogger(__name__)

# Инициализация клиента OpenAI
client = OpenAI(api_key=config.openai_api_key)


def generate_images(prompt: str, model: str = "dall-e-3", size: str = "1024x1024") -> List[str]:
    """Генерирует изображения по текстовому описанию."""
    try:
        # DALL-E 3 поддерживает только 1 изображение за запрос
        if model == "dall-e-3":
            # Проверяем допустимые размеры для DALL-E 3
            if size not in ["1024x1024", "1792x1024", "1024x1792"]:
                size = "1024x1024"

            response = client.images.generate(  # УБРАТЬ await
                model=model,
                prompt=prompt,
                size=size,
                quality="standard",
                n=1,
            )
            return [response.data[0].url]

        # Для DALL-E 2
        elif model == "dall-e-2":
            if size not in ["256x256", "512x512", "1024x1024"]:
                size = "1024x1024"

            response = client.images.generate(  # УБРАТЬ await
                model=model,
                prompt=prompt,
                size=size,
                n=1,
            )
            return [img.url for img in response.data]

        else:
            raise ValueError(f"Unsupported model: {model}")

    except Exception as e:
        logger.error(f"Error generating images: {e}")
        raise


def generate_image_with_input(prompt: str, image_bytes: bytes) -> str:
    """Генерирует изображение на основе входного изображения и промпта."""
    try:
        response = client.images.edit(  # УБРАТЬ await
            model="dall-e-2",  # DALL-E 2 поддерживает редактирование
            image=image_bytes,
            prompt=prompt,
            size="1024x1024",
            n=1,
        )
        return response.data[0].url
    except Exception as e:
        logger.error(f"Error generating image with input: {e}")
        raise


def transcribe_audio(audio_buffer) -> str:
    """Транскрибирует аудио сообщение."""
    try:
        audio_buffer.name = "audio.oga"  # Важно установить имя файла
        response = client.audio.transcriptions.create(  # УБРАТЬ await
            model="whisper-1",
            file=audio_buffer,
            language="ru"  # Опционально: указываем язык
        )
        return response.text
    except Exception as e:
        logger.error(f"Error transcribing audio: {e}")
        raise